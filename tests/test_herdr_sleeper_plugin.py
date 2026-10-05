"""Unit tests for the herdr-sleeper plugin: pure decision logic plus the sleep/wake state
machine, the watcher tick, the focus hook, and the in-pane stub — against a fake `herdr`
(no Herdr, no Claude needed)."""

from __future__ import annotations

import json
import os
import signal
import time
import types
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "herdr-sleeper"
sleeper = types.ModuleType("herdr_sleeper")
sleeper.__file__ = str(SCRIPT)  # a real interpreter sets __file__; exec() does not
exec(compile(SCRIPT.read_text(), str(SCRIPT), "exec"), sleeper.__dict__)

UUID = "20603f77-21a6-4a16-9f2b-77a87efbc665"
REAL_UUID_LIVE = sleeper.uuid_live_elsewhere  # captured before any fixture stubs it


def agent(
    pane: str = "w1:p1",
    uuid: str | None = UUID,
    status: str = "idle",
    focused: bool = False,
    kind: str = "claude",
    name: str | None = "a",
    seq: int = 5,
    terminal: str = "term-1",
    cwd: str = "/tmp/proj",
) -> dict[str, Any]:
    a: dict[str, Any] = {"pane_id": pane, "agent_status": status, "focused": focused, "agent": kind,
                         "state_change_seq": seq, "cwd": cwd, "terminal_title_stripped": "Title",
                         "terminal_id": terminal, "workspace_id": "w1", "tab_id": "w1:t1"}
    if name:
        a["name"] = name
    if uuid:
        a["agent_session"] = {"value": uuid}
    return a


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated state dir + transcript root, with a transcript for UUID on disk."""
    d = tmp_path / "state"
    for name in ("STATE_DIR", "JOURNAL", "SNAPSHOT", "EVENTS", "LOGFILE", "LOCK", "WATCHER_PID"):
        base = getattr(sleeper, name)
        monkeypatch.setattr(sleeper, name, d / base.name if name != "STATE_DIR" else d)
    monkeypatch.setattr(sleeper, "LEGACY_STATE_DIR", tmp_path / "legacy")
    monkeypatch.setattr(sleeper, "CONFIG_FILE", tmp_path / "absent.toml")
    root = tmp_path / "projects"
    (root / "proj").mkdir(parents=True)
    monkeypatch.setattr(sleeper, "TRANSCRIPTS", root)
    return tmp_path


def transcript(tmp_path: Path, uuid: str, age_hours: float, proj: str = "proj") -> Path:
    p = tmp_path / "projects" / proj / f"{uuid}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{}\n")
    stamp = time.time() - age_hours * 3600
    os.utime(p, (stamp, stamp))
    return p


EMPTY_SCREEN = "⏺ pong\n────────\n❯ \n────────\n  ◤ graft · 40 nodes\n"


class FakeHerdr:
    """Scripted `herdr` CLI: agents list, per-pane info, screen text, claims, and a call log."""

    def __init__(self, agents: list[dict[str, Any]], screen: str = EMPTY_SCREEN,
                 argv: list[str] | None = None) -> None:
        self.agents = agents
        self.screen: str | None = screen
        self.argv = ["--dangerously-skip-permissions"] if argv is None else argv
        self.calls: list[tuple[str, ...]] = []
        self.exit_leaves_agent = True      # a SIGTERM/prompt removes the agent from its pane
        self.start_uuid: str | None = None  # what agent start reports; None → the requested --resume uuid
        self.process_info_broken = False    # process-info returns an error envelope
        self.process_override: bool | None = None  # force process presence regardless of agent registration
        self.pidless = False              # report the agent process without a pid (argv still readable)
        self.stub_panes: set[str] = set()   # panes whose foreground is our wake stub
        self.signals: list[tuple[int, int]] = []
        self.execvps: list[tuple[str, list[str]]] = []

    def kill(self, pid: int, sig: int) -> None:
        self.signals.append((pid, sig))
        if self.exit_leaves_agent and sig == signal.SIGTERM:
            for a in self.agents:
                if a["pane_id"] in {p for p in self.stub_panes} or True:
                    if a.get("agent") and a.get("pid_hint") == pid:
                        a.pop("agent", None)
                        a.pop("agent_session", None)
            # the fake's processes carry pid 1 for the agent; drop any pane whose agent had no explicit pid
            for a in self.agents:
                if a.get("agent") and a.get("pid_hint", 1) == pid:
                    a.pop("agent", None)
                    a.pop("agent_session", None)

    def _procs(self, pane: str, a: dict[str, Any] | None) -> list[dict[str, Any]]:
        if pane in self.stub_panes:
            return [{"argv0": "python3", "name": "python3",
                     "argv": ["python3", str(SCRIPT), "stub", pane, "u"], "pid": 99}]
        present = (a is not None and bool(a.get("agent"))) if self.process_override is None else self.process_override
        if not present:
            return []
        kind = (a or {}).get("agent") or "claude"
        proc = {"argv0": kind, "name": kind, "argv": [kind, *self.argv]}
        if not self.pidless:
            proc["pid"] = a.get("pid_hint", 1)
        return [proc]

    def __call__(self, *args: str, check: bool = True, timeout: float = 0) -> dict[str, Any]:
        self.calls.append(args)
        by_pane = {a["pane_id"]: a for a in self.agents}
        if args[:2] == ("agent", "list"):
            return {"agents": self.agents}
        if args[:2] == ("pane", "list"):
            return {"panes": [{"pane_id": a["pane_id"], "terminal_id": a.get("terminal_id"),
                               "cwd": a.get("cwd"), "label": a.get("label")} for a in self.agents]}
        if args[:2] == ("pane", "get"):
            a = by_pane.get(args[2])
            if a is None:
                if check:
                    raise sleeper.SleeperError("no such pane")
                return {}
            pane = {"pane_id": args[2], "label": a.get("label"), "cwd": a.get("cwd"),
                    "terminal_id": a.get("terminal_id")}
            if a.get("agent"):
                pane["agent"] = a["agent"]
                pane["agent_session"] = a.get("agent_session")
            return {"pane": pane}
        if args[:2] == ("pane", "process-info"):
            pane = args[args.index("--pane") + 1]
            if self.process_info_broken:
                return {}
            return {"process_info": {"foreground_processes": self._procs(pane, by_pane.get(pane))}}
        if args[:2] == ("agent", "start"):
            pane = args[args.index("--pane") + 1]
            uuid = self.start_uuid or args[args.index("--resume") + 1]
            kind = args[args.index("--kind") + 1]
            a = by_pane[pane]
            a.update(agent=kind, agent_session={"value": uuid})
            self.stub_panes.discard(pane)
            return {"agent": {"agent_session": {"value": uuid}}}
        if args[:2] == ("pane", "report-agent"):
            by_pane[args[2]]["agent"] = sleeper.CLAIM_AGENT
            return {}
        if args[:2] == ("pane", "release-agent"):
            a = by_pane[args[2]]
            if a.get("agent") == sleeper.CLAIM_AGENT:
                a.pop("agent", None)
            return {}
        if args[:2] in (("pane", "run"), ("pane", "rename"), ("pane", "report-metadata"),
                        ("pane", "send-keys"), ("notification", "show"), ("agent", "prompt")):
            if args[:2] == ("pane", "rename"):
                assert len(args) > 3, "bare `pane rename <pane>` is a usage error in real herdr"
                by_pane[args[2]]["label"] = None if args[3] == "--clear" else args[3]
            return {}
        raise AssertionError(f"unexpected herdr call {args}")


@pytest.fixture
def fake(state: Path, monkeypatch: pytest.MonkeyPatch) -> FakeHerdr:
    f = FakeHerdr([agent()])
    monkeypatch.setattr(sleeper, "herdr", f)
    monkeypatch.setattr(sleeper, "herdr_screen", lambda pane: f.screen)
    monkeypatch.setattr(sleeper, "uuid_live_elsewhere", lambda uuid, except_pane=None, cwd=None: None)
    monkeypatch.setattr(sleeper.time, "sleep", lambda s: None)
    monkeypatch.setattr(sleeper, "EXIT_WAIT_SECONDS", 2)
    monkeypatch.setattr(sleeper.os, "kill", f.kill)
    transcript(state, UUID, 30)
    return f


def journal() -> dict[str, Any]:
    return sleeper.read_json(sleeper.JOURNAL)


def events() -> list[str]:
    p = sleeper.EVENTS
    return [json.loads(l)["event"] for l in p.read_text().splitlines()] if p.exists() else []


def stub_run(fake: FakeHerdr) -> str:
    return next(c[3] for c in fake.calls if c[:2] == ("pane", "run"))


# ------------------------------------------------------------- pure helpers

def test_replayable_argv_strips_session_selectors() -> None:
    ok = sleeper.replayable_argv
    assert ok(["--dangerously-skip-permissions", "--resume", "abc"]) == (["--dangerously-skip-permissions"], None)
    assert ok(["-r", "abc", "--model", "opus"]) == (["--model", "opus"], None)
    assert ok(["--resume=abc", "-c", "--continue"]) == ([], None)
    assert ok(["--resume", "--model", "opus"]) == (["--model", "opus"], None)  # bare --resume opens a picker
    assert ok([]) == ([], None)
    assert ok(["--add-dir", "/x", "--verbose"]) == (["--add-dir", "/x", "--verbose"], None)
    assert ok(["--unknown-flag", "value"])[0] is None and "unrecognised" in ok(["--unknown-flag", "value"])[1]
    assert ok(["--model"])[0] is None  # value option missing its value


@pytest.mark.parametrize(
    "args, needle",
    [
        (["--fork-session"], "--fork-session"),
        (["-p", "hi"], "-p"),
        (["--session-id", "x"], "--session-id"),
        (["--verbose", "publish the release"], "positional"),
        (["--dangerously-skip-permissions", "--", "x"], "`--`"),
    ],
)
def test_replayable_argv_refuses_dangerous(args: list[str], needle: str) -> None:
    argv, why = sleeper.replayable_argv(args)
    assert argv is None and why and needle in why


def test_parse_duration() -> None:
    assert sleeper.parse_duration("12h") == 12
    assert sleeper.parse_duration("90m") == 1.5
    assert sleeper.parse_duration("1d") == 24
    assert sleeper.parse_duration("3600s") == 1
    assert sleeper.parse_duration("2") == 2
    with pytest.raises(sleeper.ConfigError):
        sleeper.parse_duration("soon")


CLAUDE_SCREEN = """
❯ earlier question
⏺ pong
✻ Worked for 1s · done 5:23 PM
────────
❯ {composer}
────────
  ◤ graft · 40 nodes / 63 edges · ✓ synced
  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents
"""


def test_composer_state_fails_closed() -> None:
    cs = sleeper.composer_state
    assert cs(CLAUDE_SCREEN.format(composer=""))[0] == "empty"
    assert cs(CLAUDE_SCREEN.format(composer="half-typed thought")) == ("draft", "half-typed thought")
    multiline = CLAUDE_SCREEN.format(composer="") .replace("────────\n  ◤", "  second line of a draft\n────────\n  ◤", 1)
    assert cs(multiline)[0] == "draft"
    assert cs("❯ \n  ?\n────────\n")[0] == "draft"          # one-character draft is still a draft
    assert cs("❯ \n")[0] == "unknown"                    # bare glyph, nothing rendered below it
    assert cs(None)[0] == "unknown"                       # unreadable screen
    assert cs("djbclark@mac:~$ \n")[0] == "unknown"       # bare shell: no composer to vouch for
    assert cs("⏺ done\n✻ Worked\n")[0] == "unknown"       # transcript visible, composer scrolled off
    assert cs("⏺ done\n> quoted line\n")[0] == "unknown"  # a bare ">" is not the composer
    assert cs("❯ \n─── a rule I am drafting\n────────\n  ⏵⏵ bypass\n")[0] == "draft"


def test_manual_command_is_shell_safe() -> None:
    cmd = sleeper.manual_command({"cwd": "/tmp/my dir", "argv": ["--model", "it's"], "uuid": "u", "kind": "claude"})
    assert cmd == "cd '/tmp/my dir' && claude --model 'it'\"'\"'s' --resume u"


def test_manual_command_never_prints_refused_argv() -> None:
    cmd = sleeper.manual_command({"cwd": "/p", "argv": ["--fork-session", "--model", "x"], "uuid": "u", "kind": "claude"})
    assert cmd == "cd /p && claude --resume u"


def test_wake_name_falls_back_to_pane() -> None:
    assert sleeper.wake_name({"name": None, "pane_id": "w26:p2"}) == "wake-w26-p2"
    assert sleeper.wake_name({"name": "lichess", "pane_id": "w24:p1"}) == "lichess"


# ---------------------------------------------------------------- config

def test_load_config_precedence(state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = state / "config.toml"
    monkeypatch.setattr(sleeper, "CONFIG_FILE", cfg)
    for var in ("HERDR_SLEEPER_IDLE_HOURS", "HERDR_SLEEPER_IDLE", "HERDR_SLEEPER_EXCLUDE",
                "HERDR_SLEEPER_AGENTS", "HERDR_SLEEPER_POLL_SECONDS"):
        monkeypatch.delenv(var, raising=False)
    values, source = sleeper.load_config()
    assert values == {"idle_hours": 12.0, "agents": ["claude", "opencode"], "exclude": [],
                      "poll_seconds": 60, "notify": True, "wake_on_focus": True}
    assert set(source.values()) == {"default"}

    cfg.write_text('idle = "90m"\nexclude = "orc"\npoll_seconds = 30\nnotify = false\n')
    values, source = sleeper.load_config()
    assert values["idle_hours"] == 1.5 and values["exclude"] == ["orc"]   # a string is one name, not letters
    assert values["poll_seconds"] == 30 and values["notify"] is False
    assert source["idle_hours"] == str(cfg)

    monkeypatch.setenv("HERDR_SLEEPER_IDLE", "2d")
    monkeypatch.setenv("HERDR_SLEEPER_EXCLUDE", "a b")
    values, source = sleeper.load_config()
    assert values["idle_hours"] == 48 and values["exclude"] == ["a", "b"]
    assert source["idle_hours"] == "$HERDR_SLEEPER_IDLE"


@pytest.mark.parametrize(
    "text",
    ["idle_hours = = 3\n", "idle_hours = nan\n", "idle_hours = -1\n", "exclude = [1, 2]\n", "poll_seconds = 0\n",
     "bogus = 1\n", "idle_hours = false\n", "poll_seconds = true\n", 'agents = ["codex"]\n', "notify = 1\n"],
)
def test_load_config_rejects_bad_values(state: Path, monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    cfg = state / "config.toml"
    cfg.write_text(text)
    monkeypatch.setattr(sleeper, "CONFIG_FILE", cfg)
    with pytest.raises(sleeper.ConfigError):
        sleeper.load_config()


def test_env_alias_conflict_and_units(state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sleeper, "CONFIG_FILE", state / "absent.toml")
    monkeypatch.setenv("HERDR_SLEEPER_IDLE_HOURS", "90m")
    monkeypatch.delenv("HERDR_SLEEPER_IDLE", raising=False)
    assert sleeper.load_config()[0]["idle_hours"] == 1.5
    monkeypatch.setenv("HERDR_SLEEPER_IDLE", "2h")
    with pytest.raises(sleeper.ConfigError):
        sleeper.load_config()


def test_cli_exclude_adds_to_config_exclude(fake: FakeHerdr, state: Path,
                                            monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = state / "config.toml"
    cfg.write_text('exclude = ["a"]\nidle_hours = 0.0\n')
    monkeypatch.setattr(sleeper, "CONFIG_FILE", cfg)
    monkeypatch.setattr(sleeper, "list_agents", lambda: fake.agents)
    assert sleeper.main(["scan", "--exclude", "zzz"]) == 0
    assert "excluded" in capsys.readouterr().out and journal() == {}


def test_scan_refuses_with_broken_config(state: Path, monkeypatch: pytest.MonkeyPatch,
                                         capsys: pytest.CaptureFixture[str]) -> None:
    cfg = state / "config.toml"
    cfg.write_text("idle_hours = = 3\n")
    monkeypatch.setattr(sleeper, "CONFIG_FILE", cfg)
    assert sleeper.main(["scan"]) == 2
    assert "broken config" in capsys.readouterr().err


# ------------------------------------------------------------- assessment

def test_assess_sleeps_when_window_elapsed(state: Path) -> None:
    transcript(state, UUID, 30)
    since = time.monotonic() - 13 * 3600
    ok, _reason, idle = sleeper.assess(agent(), [agent()], 12, {}, idle_since=since)
    assert ok and idle is not None and idle > 12


def test_transcript_for_picks_newest_copy(state: Path) -> None:
    old = transcript(state, UUID, 40, proj="older")
    new = transcript(state, UUID, 0.1, proj="newer")
    assert sleeper.transcript_for(UUID) == new and sleeper.transcript_for(UUID) != old


def test_assess_requires_transcript_for_claude(state: Path) -> None:
    ok, reason, _ = sleeper.assess(agent(), [agent()], 12, {}, idle_since=time.monotonic() - 100 * 3600)
    assert not ok and "no transcript" in reason


def test_assess_opencode_needs_no_transcript(state: Path) -> None:
    since = time.monotonic() - 13 * 3600
    a = agent(kind="opencode", uuid="sess-1")
    ok, _reason, _ = sleeper.assess(a, [a], 12, {}, idle_since=since)
    assert ok


@pytest.mark.parametrize(
    "kw, needle",
    [
        ({"status": "working"}, "status working"),
        ({"status": "blocked"}, "status blocked"),
        ({"focused": True}, "focused"),
        ({"focused": None}, "focus unknown"),
        ({"uuid": None}, "no session uuid"),
        ({"kind": "codex"}, "not supported"),
    ],
)
def test_assess_rejections(state: Path, kw: dict[str, Any], needle: str) -> None:
    transcript(state, UUID, 30)
    a = agent(**kw)
    ok, reason, _ = sleeper.assess(a, [a], 12, {}, idle_since=time.monotonic() - 100 * 3600)
    assert not ok and needle in reason


def test_assess_scan_rows_and_gates(state: Path) -> None:
    transcript(state, UUID, 30)
    a, b = agent(pane="w1:p1"), agent(pane="w2:p1", status="working")
    assert "w2:p1" in sleeper.assess(a, [a, b], 12, {})[1]
    assert sleeper.assess(a, [a], 12, {}, {"a"})[1] == "excluded"
    assert sleeper.assess(a, [a], 12, {}, {"w1:p1"})[1] == "excluded"
    assert "journal" in sleeper.assess(a, [a], 12, {"w1:p1": {"phase": "asleep"}})[1]
    assert "no transcript" in sleeper.assess(agent(uuid="other"), [agent(uuid="other")], 12, {})[1]


# ------------------------------------------------------ sleep state machine

def test_sleep_happy_path_signals_and_leaves_stub(fake: FakeHerdr) -> None:
    slept, outcome = sleeper.sleep_agent(agent(), 12, set(), dry_run=False)
    assert slept and outcome == "slept"
    assert fake.signals == [(1, signal.SIGTERM)]                     # the exit is a signal, not a typed command
    j = journal()["w1:p1"]
    assert j["uuid"] == UUID and j["kind"] == "claude" and j["phase"] == "asleep"
    assert j["argv"] == ["--dangerously-skip-permissions"] and j["terminal_id"] == "term-1"
    assert ("agent", "prompt", "w1:p1", "/exit") not in fake.calls
    run = stub_run(fake)
    assert str(SCRIPT) in run and "stub" in run and "w1:p1" in run and UUID in run
    assert "exec " not in run                                        # the pane's shell must survive beneath the stub
    assert any(c[:2] == ("pane", "rename") and c[3].startswith("💤") for c in fake.calls)
    assert any(c[:2] == ("pane", "report-agent") for c in fake.calls)  # sidebar claim
    assert fake.agents[0]["agent"] == sleeper.CLAIM_AGENT
    assert any(c[:2] == ("notification", "show") for c in fake.calls)
    assert events() == ["slept"]


def test_sleep_discards_draft_via_signal(fake: FakeHerdr) -> None:
    """A draft no longer blocks sleeping: SIGTERM discards it instead of the old
    /exit-appends-to-draft hazard. (The composer check still guards prompt-exit kinds.)"""
    fake.screen = CLAUDE_SCREEN.format(composer="unsent")
    slept, outcome = sleeper.sleep_agent(agent(), 12, set(), dry_run=False)
    assert slept and outcome == "slept" and journal()["w1:p1"]["phase"] == "asleep"


def test_sleep_refuses_when_state_moved_since_scan(fake: FakeHerdr) -> None:
    fake.agents[0]["state_change_seq"] = 6  # scan saw 5
    slept, outcome = sleeper.sleep_agent(agent(seq=5), 12, set(), dry_run=False)
    assert not slept and "state changed" in outcome
    assert not fake.signals and journal() == {}


def test_sleep_refuses_unreplayable_argv(fake: FakeHerdr) -> None:
    fake.argv = ["--fork-session"]
    slept, outcome = sleeper.sleep_agent(agent(), 12, set(), dry_run=False)
    assert not slept and "--fork-session" in outcome and journal() == {} and not fake.signals


def test_sleep_refuses_when_process_unidentifiable(fake: FakeHerdr) -> None:
    fake.pidless = True  # process-info answers, argv is readable, but no pid to signal
    slept, outcome = sleeper.sleep_agent(agent(), 12, set(), dry_run=False)
    assert not slept and "not identifiable" in outcome and journal() == {} and not fake.signals


def test_dry_run_runs_every_check_but_acts_on_nothing(fake: FakeHerdr) -> None:
    slept, outcome = sleeper.sleep_agent(agent(), 12, set(), dry_run=True)
    assert not slept and outcome.startswith("would sleep")
    assert not fake.signals and journal() == {}
    fake.agents[0]["state_change_seq"] = 6
    assert "state changed" in sleeper.sleep_agent(agent(seq=5), 12, set(), dry_run=True)[1]


def test_exit_timeout_keeps_handle_as_exit_requested(fake: FakeHerdr) -> None:
    fake.exit_leaves_agent = False  # SIGTERM delivered but the agent lingers
    slept, outcome = sleeper.sleep_agent(agent(), 12, set(), dry_run=False)
    assert not slept and "uncertain" in outcome
    assert journal()["w1:p1"]["phase"] == "exit-requested"
    assert events() == ["exit-uncertain"]


def test_exit_with_unreadable_process_info_stays_uncertain(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch) -> None:
    # process-info works until the signal is sent, then becomes unreadable: Herdr drops the agent
    # but we cannot positively see the process go -> exit-requested, no pane run, handle kept
    real_kill = fake.kill

    def flaky(pid: int, sig: int) -> None:
        real_kill(pid, sig)
        fake.process_info_broken = True

    monkeypatch.setattr(sleeper.os, "kill", flaky)
    slept, outcome = sleeper.sleep_agent(agent(), 12, set(), dry_run=False)
    assert not slept and "uncertain" in outcome
    assert journal()["w1:p1"]["phase"] == "exit-requested"
    assert not any(c[:2] == ("pane", "run") for c in fake.calls)


def test_reconcile_settles_exit_requested(fake: FakeHerdr) -> None:
    fake.exit_leaves_agent = False
    sleeper.sleep_agent(agent(), 12, set(), dry_run=False)
    # case 1: the agent is still there next tick → first sighting only counts, second drops
    sleeper.reconcile(fake.agents)
    assert journal()["w1:p1"]["seen_running"] == 1
    sleeper.reconcile(fake.agents)
    assert journal() == {}
    # case 2: it did leave after the timeout → becomes asleep
    fake.exit_leaves_agent = False
    fake.agents = [agent()]
    sleeper.sleep_agent(agent(), 12, set(), dry_run=False)
    fake.agents[0].pop("agent"); fake.agents[0].pop("agent_session")
    sleeper.reconcile(fake.agents)
    assert journal()["w1:p1"]["phase"] == "asleep"


def test_reconcile_drops_entry_when_pane_runs_another_session(fake: FakeHerdr) -> None:
    sleeper.sleep_agent(agent(), 12, set(), dry_run=False)
    fake.agents[0].update(agent="claude", agent_session={"value": "other"})
    sleeper.reconcile(fake.agents)
    assert journal() == {} and events()[-1] == "reconciled"


# ------------------------------------------------------- wake state machine

def slept_entry(fake: FakeHerdr) -> dict[str, Any]:
    sleeper.sleep_agent(agent(), 12, set(), dry_run=False)
    return journal()["w1:p1"]


def test_wake_happy_path_restores_label(fake: FakeHerdr) -> None:
    fake.agents[0]["label"] = "mylabel"
    entry = slept_entry(fake)
    assert fake.agents[0]["label"] == "💤 mylabel"
    assert sleeper.wake_entry(entry)
    start = next(c for c in fake.calls if c[:2] == ("agent", "start"))
    assert start[-2:] == ("--resume", UUID) and "--dangerously-skip-permissions" in start
    assert any(c[:2] == ("pane", "release-agent") for c in fake.calls)   # claim dropped before the start
    assert fake.agents[0]["label"] == "mylabel" and fake.agents[0]["agent"] == "claude"
    assert journal() == {} and events()[-1] == "woke"


def test_wake_clears_label_that_did_not_exist_before_sleep(fake: FakeHerdr) -> None:
    entry = slept_entry(fake)
    assert fake.agents[0]["label"] == "💤 Title" and entry["label"] is None
    assert sleeper.wake_entry(entry)
    assert fake.agents[0]["label"] is None


def test_wake_filters_snapshot_argv(fake: FakeHerdr) -> None:
    entry = slept_entry(fake)
    entry["argv"] = ["--fork-session"]  # a raw snapshot argv that sleep never vetted
    assert not sleeper.wake_entry(entry) and "w1:p1" in journal()
    assert not any(c[:2] == ("agent", "start") for c in fake.calls)


def test_read_json_rejects_non_object_root(state: Path) -> None:
    sleeper.STATE_DIR.mkdir(parents=True)
    sleeper.JOURNAL.write_text("[]\n")
    with pytest.raises(sleeper.DamagedState):
        sleeper.read_json(sleeper.JOURNAL)


def test_wake_quarantines_damaged_journal_before_snapshot_recovery(fake: FakeHerdr, capsys: pytest.CaptureFixture[str]) -> None:
    sleeper.merge_snapshot(fake.agents)
    fake.agents[0].pop("agent"); fake.agents[0].pop("agent_session")
    sleeper.JOURNAL.write_text("[]\n")
    assert sleeper.cmd_wake(types.SimpleNamespace(target="a", all=False)) == 0
    damaged = list(sleeper.STATE_DIR.glob("sleeping.json.damaged-*"))
    assert len(damaged) == 1 and damaged[0].read_text() == "[]\n"
    assert "damaged journal moved to" in capsys.readouterr().err


def test_reconcile_needs_a_real_process_before_dropping(fake: FakeHerdr) -> None:
    fake.exit_leaves_agent = False
    sleeper.sleep_agent(agent(), 12, set(), dry_run=False)
    fake.process_override = False  # agent list says present, process-info says no process
    sleeper.reconcile(fake.agents); sleeper.reconcile(fake.agents); sleeper.reconcile(fake.agents)
    assert journal()["w1:p1"]["phase"] == "exit-requested" and "seen_running" not in journal()["w1:p1"]


def test_reconcile_sightings_must_be_consecutive(fake: FakeHerdr) -> None:
    fake.exit_leaves_agent = False
    sleeper.sleep_agent(agent(), 12, set(), dry_run=False)
    sleeper.reconcile(fake.agents)                      # True
    fake.process_override = None; fake.process_info_broken = True
    sleeper.reconcile(fake.agents)                      # unknown -> streak resets
    fake.process_info_broken = False
    sleeper.reconcile(fake.agents)                      # True again: only one in a row
    assert "w1:p1" in journal()


def test_displaced_session_survives_reconcile(fake: FakeHerdr) -> None:
    entry = slept_entry(fake)
    fake.start_uuid = "fresh-uuid"
    assert not sleeper.wake_entry(entry)                # pane now runs fresh-uuid; entry kept with wake_got
    sleeper.merge_snapshot(fake.agents)
    sleeper.reconcile(fake.agents)
    j = journal()["w1:p1"]
    assert j["uuid"] == UUID and j["phase"] == "displaced" and j["argv"] == ["--dangerously-skip-permissions"]
    assert not sleeper.wake_entry(j)                    # manual-only from here


def test_wake_refuses_recycled_pane_by_terminal_id(fake: FakeHerdr) -> None:
    entry = slept_entry(fake)
    fake.agents[0]["terminal_id"] = "term-OTHER"  # pane id lives on, terminal does not: recycled
    assert not sleeper.wake_entry(entry) and "w1:p1" in journal()
    assert not any(c[:2] == ("agent", "start") for c in fake.calls)


def test_wake_refuses_recycled_pane_by_cwd(fake: FakeHerdr) -> None:
    entry = slept_entry(fake)
    fake.agents[0]["cwd"] = "/somewhere/else"
    assert not sleeper.wake_entry(entry) and "w1:p1" in journal()
    assert not any(c[:2] == ("agent", "start") for c in fake.calls)


def test_wake_refuses_when_pane_gone_and_keeps_entry(fake: FakeHerdr) -> None:
    entry = slept_entry(fake)
    fake.agents = []
    assert not sleeper.wake_entry(entry) and "w1:p1" in journal()


def test_wake_refuses_when_argv_unknown(fake: FakeHerdr) -> None:
    entry = slept_entry(fake)
    entry["argv"] = None
    assert not sleeper.wake_entry(entry)
    assert not any(c[:2] == ("agent", "start") for c in fake.calls)


def test_wake_refuses_when_session_live_elsewhere(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch) -> None:
    entry = slept_entry(fake)
    monkeypatch.setattr(sleeper, "uuid_live_elsewhere", lambda uuid, except_pane=None, cwd=None: "pid 42")
    assert not sleeper.wake_entry(entry) and "w1:p1" in journal()


def test_wake_refuses_when_liveness_unknown(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch) -> None:
    entry = slept_entry(fake)

    def boom(uuid: str, except_pane: str | None = None, cwd: str | None = None) -> None:
        raise sleeper.SleeperError("ps failed")

    monkeypatch.setattr(sleeper, "uuid_live_elsewhere", boom)
    with pytest.raises(sleeper.SleeperError):
        sleeper.wake_entry(entry)
    assert "w1:p1" in journal()


def test_wake_clears_entry_when_same_session_already_running(fake: FakeHerdr) -> None:
    entry = slept_entry(fake)
    fake.agents[0].update(agent="claude", agent_session={"value": UUID})  # came back by other means
    assert sleeper.wake_entry(entry) and journal() == {}


def test_wake_keeps_entry_when_agent_shown_but_no_process(fake: FakeHerdr) -> None:
    entry = slept_entry(fake)
    fake.agents[0].update(agent="claude", agent_session={"value": UUID})
    fake.process_info_broken = True
    assert not sleeper.wake_entry(entry) and "w1:p1" in journal()


def test_wake_keeps_entry_on_session_mismatch(fake: FakeHerdr) -> None:
    entry = slept_entry(fake)
    fake.start_uuid = "fresh-uuid"
    assert not sleeper.wake_entry(entry)
    assert journal()["w1:p1"]["wake_got"] == "fresh-uuid" and events()[-1] == "wake-mismatch"


def test_wake_with_stub_running_sends_enter_instead_of_starting(fake: FakeHerdr) -> None:
    entry = slept_entry(fake)
    fake.stub_panes.add("w1:p1")
    assert sleeper.wake_entry(entry)
    assert any(c[:3] == ("pane", "send-keys", "w1:p1") and "enter" in c for c in fake.calls)
    assert not any(c[:2] == ("agent", "start") for c in fake.calls)   # the stub execs the agent itself


def test_wake_from_snapshot_persists_before_trying(fake: FakeHerdr, capsys: pytest.CaptureFixture[str]) -> None:
    sleeper.merge_snapshot(fake.agents)
    fake.agents[0].pop("agent"); fake.agents[0].pop("agent_session")   # asleep, but no journal (crash)
    assert sleeper.cmd_wake(types.SimpleNamespace(target="a", all=False)) == 0
    assert (fake.agents[0].get("agent_session") or {}).get("value") == UUID
    assert "recovering" in capsys.readouterr().out


def test_snapshot_keeps_sleeping_panes(fake: FakeHerdr) -> None:
    sleeper.merge_snapshot(fake.agents)
    fake.agents[0].pop("agent"); fake.agents[0].pop("agent_session")   # now asleep: not in agent list
    sleeper.merge_snapshot([])
    assert "w1:p1" in sleeper.read_json(sleeper.SNAPSHOT)
    fake.agents[0].update(agent="claude", agent_session={"value": "different"})
    sleeper.merge_snapshot(fake.agents)
    assert sleeper.read_json(sleeper.SNAPSHOT)["w1:p1"]["uuid"] == "different"


# ------------------------------------------------------------------ stub

def test_stub_wakes_by_exec_after_enter(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch,
                                        capsys: pytest.CaptureFixture[str]) -> None:
    entry = slept_entry(fake)
    fake.stub_panes.add("w1:p1")
    fake.agents[0].pop("agent", None)  # claimed placeholder is not a real agent for this path
    monkeypatch.setattr("builtins.input", lambda _prompt="": "")
    monkeypatch.setattr(sleeper.os, "chdir", lambda _p: None)  # cwd check compares realpaths; entry cwd is /tmp/proj
    monkeypatch.setattr(sleeper.os, "getcwd", lambda: entry["cwd"])
    execvps: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(sleeper.os, "execvp", lambda b, a: execvps.append((b, a)))
    assert sleeper.cmd_stub("w1:p1", UUID) == 0
    assert execvps == [("claude", ["claude", "--dangerously-skip-permissions", "--resume", UUID])]
    assert journal() == {} and events()[-1] == "woke"
    assert any(c[:2] == ("pane", "release-agent") for c in fake.calls)


def test_stub_leaves_shell_on_interrupt(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch,
                                        capsys: pytest.CaptureFixture[str]) -> None:
    entry = slept_entry(fake)
    monkeypatch.setattr("builtins.input", lambda _prompt="": (_ for _ in ()).throw(KeyboardInterrupt()))
    assert sleeper.cmd_stub("w1:p1", UUID) == 0
    assert "w1:p1" in journal()  # entry kept; the printed manual command still points at it
    assert "resume with:" in capsys.readouterr().out


def test_stub_refuses_when_session_live_elsewhere(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch,
                                                  capsys: pytest.CaptureFixture[str]) -> None:
    entry = slept_entry(fake)
    monkeypatch.setattr("builtins.input", lambda _prompt="": "")
    monkeypatch.setattr(sleeper, "uuid_live_elsewhere", lambda uuid, except_pane=None, cwd=None: "pane w9:p9")
    assert sleeper.cmd_stub("w1:p1", UUID) == 0
    assert "w9:p9" in capsys.readouterr().out and "w1:p1" in journal()


# ------------------------------------------------------------- focus hook

def focus_env(pane: str) -> None:
    os.environ["HERDR_PLUGIN_EVENT_JSON"] = json.dumps(
        {"event": "pane_focused", "data": {"type": "pane_focused", "pane_id": pane, "workspace_id": "w1"}})


def test_on_focus_wakes_sleeping_pane(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch) -> None:
    slept_entry(fake)
    focus_env("w1:p1")
    assert sleeper.cmd_on_focus() == 0
    assert any(c[:2] == ("agent", "start") for c in fake.calls) and journal() == {}


def test_on_focus_ignores_ordinary_panes(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch) -> None:
    focus_env("w9:p9")  # nothing of ours there: no wake, no lock contention, fast exit
    assert sleeper.cmd_on_focus() == 0
    assert not any(c[:2] == ("agent", "start") for c in fake.calls)


def test_on_focus_debounces_one_click(fake: FakeHerdr, monkeypatch: pytest.MonkeyPatch) -> None:
    slept_entry(fake)
    focus_env("w1:p1")
    lock = sleeper.STATE_DIR / ".w1_p1.wakelock"  # pane.replace(":", "_") inside the leading dot
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("held")  # a sibling hook from the same click holds the per-pane lock
    assert sleeper.cmd_on_focus() == 0
    assert not any(c[:2] == ("agent", "start") for c in fake.calls)


def test_on_focus_respects_config_optout(fake: FakeHerdr, state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    slept_entry(fake)
    cfg = state / "config.toml"
    cfg.write_text("wake_on_focus = false\n")
    monkeypatch.setattr(sleeper, "CONFIG_FILE", cfg)
    focus_env("w1:p1")
    assert sleeper.cmd_on_focus() == 0
    assert not any(c[:2] == ("agent", "start") for c in fake.calls)


# ---------------------------------------------------------------- watcher

def test_watch_tick_sleeps_only_after_window(fake: FakeHerdr, state: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = state / "config.toml"
    cfg.write_text('idle = "1h"\n')
    monkeypatch.setattr(sleeper, "CONFIG_FILE", cfg)
    idle_since: dict[str, float] = {}
    last_seq: dict[str, object] = {}
    clock = {"t": 1000.0}
    monkeypatch.setattr(sleeper.time, "monotonic", lambda: clock["t"])
    config, _src = sleeper.load_config()

    sleeper.watch_tick(idle_since, last_seq, config)
    assert journal() == {} and idle_since == {"w1:p1": 1000.0}   # clock starts, nothing sleeps yet

    clock["t"] += 1800
    sleeper.watch_tick(idle_since, last_seq, config)
    assert journal() == {}                                       # half an hour is not an hour

    fake.agents[0]["state_change_seq"] = 6                        # activity between polls restarts the clock
    clock["t"] += 1800
    sleeper.watch_tick(idle_since, last_seq, config)
    assert journal() == {} and idle_since == {"w1:p1": 4600.0}

    clock["t"] += 3700
    sleeper.watch_tick(idle_since, last_seq, config)
    assert journal()["w1:p1"]["phase"] == "asleep"                # now a full quiet hour has passed


def test_watch_tick_honors_excludes_and_kinds(fake: FakeHerdr, state: Path,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = state / "config.toml"
    cfg.write_text('idle = "0s"\nexclude = ["a"]\n')
    monkeypatch.setattr(sleeper, "CONFIG_FILE", cfg)
    config, _src = sleeper.load_config()
    idle_since: dict[str, float] = {}
    last_seq: dict[str, object] = {}
    sleeper.watch_tick(idle_since, last_seq, config)
    assert journal() == {} and idle_since == {}                   # excluded before the clock even starts

    fake.agents[0].pop("name")
    sleeper.watch_tick(idle_since, last_seq, config)
    assert journal()["w1:p1"]["phase"] == "asleep"


# -------------------------------------------------------------- migration

def test_migration_adopts_legacy_journal(state: Path) -> None:
    legacy = state / "legacy"
    (legacy).mkdir(parents=True, exist_ok=True)
    (legacy / "sleeping.json").write_text(json.dumps({
        "w9:p1": {"pane_id": "w9:p1", "uuid": "legacy-uuid", "cwd": "/x",
                  "argv": ["--dangerously-skip-permissions"], "phase": "asleep"},
        "bad": "not an entry",
    }))
    sleeper.migrate_legacy()
    j = journal()
    assert j["w9:p1"]["uuid"] == "legacy-uuid" and j["w9:p1"]["kind"] == "claude"
    assert "bad" not in j and events() == ["migrated"]


def test_migration_is_once_and_opt_in(fake: FakeHerdr) -> None:
    (sleeper.STATE_DIR).mkdir(parents=True, exist_ok=True)
    sleeper.JOURNAL.write_text(json.dumps({"w1:p1": {"pane_id": "w1:p1", "uuid": "u", "kind": "claude"}}))
    sleeper.migrate_legacy()  # journal already exists → no touch
    assert journal()["w1:p1"]["uuid"] == "u" and events() == []
