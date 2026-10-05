# herdr-sleeper — sleep idle resumable agents, wake them later

A Herdr plugin that sleeps idle agent panes after a user-chosen window and
wakes them — on focus, by pressing Enter in the pane, or by command. It is
the standalone [`scripts/herdr-sleeper`](https://github.com/djbclark/herdr/tree/herdr-sleeper/scripts/herdr-sleeper)
reference implementation reborn as a plugin: the fail-closed safety rules
are unchanged, and the plugin adds what only a plugin can do (an in-process
idle watcher, wake-on-focus, a sidebar placeholder for slept panes,
notifications).

Grown from a study of the two existing community plugins — good ideas
adopted from both (see *Credits* at the end); the safety state machine,
argv replay filter, session-live checks, and config validation are ours and
carried over from the standalone script's three review rounds plus its
live-deployment iteration.

## Install

```bash
herdr plugin install djbclark/herdr-sleeper
```

Requires Herdr ≥ 0.9.1 on macOS or Linux, and Python 3.9 or newer on the
PATH the Herdr server sees (stdlib only — no packages, no venv; config
parsing falls back to a built-in TOML subset below Python 3.11).

For local development:

```bash
git clone https://github.com/djbclark/herdr-sleeper
herdr plugin link <checkout>
```

## What it does

After an agent has been idle for a user-chosen window (`idle = "12h"` —
default 12 h), it exits the agent process (SIGTERM for claude and opencode:
both shut down cleanly and a composer draft is discarded, never submitted),
keeps the pane, reports a `sleeper` placeholder so the sidebar keeps its
row, leaves a wake stub in the pane ("press Enter to resume"), prefixes the
pane label with 💤, and posts a notification with the freed MB.

Wake paths:

1. **Focus the pane** — the `pane.focused` hook wakes it (disable with
   `wake_on_focus = false`).
2. **Press Enter in the pane** — the wake stub execs the agent back into
   its session after the same fail-closed checks.
3. **Command** — `herdr plugin action invoke djbclark.herdr-sleeper.wake`
   with the pane focused, or `wake-all` / `list` / `log` from anywhere.

The idle clock is `state_change_seq` movement from the watcher's polling of
`agent list` (default 60 s) — no transcript mtime, no launchd, and the
clock restarts on any state change between polls. `done` (idle-but-unviewed)
accrues like `idle`.

Supported kinds: **claude** (original argv replayed on wake) and
**opencode** (`-s <session>`). Codex is deliberately absent — see the
comment on `PROFILES` in the script.

## Use

```bash
herdr plugin action invoke djbclark.herdr-sleeper.list
herdr plugin action invoke djbclark.herdr-sleeper.sleep-pane   # context pane
herdr plugin action invoke djbclark.herdr-sleeper.wake         # context pane
herdr plugin action invoke djbclark.herdr-sleeper.wake-all
herdr plugin action invoke djbclark.herdr-sleeper.log
```

Config: `<plugin config dir>/config.toml` — `idle` (duration), `agents`
(list), `exclude` (panes or names), `poll_seconds`, `notify`,
`wake_on_focus`; overridable by `HERDR_SLEEPER_*` env, lowest to highest
precedence. State: `<plugin state dir>/` per Herdr session — `sleeping.json`
journal, `panes.json` crash-recovery snapshot, `events.jsonl`, `sleeper.log`,
`watcher.pid`. A legacy standalone-script journal
(`~/.local/state/herdr-sleeper/`) is adopted once, on the default session's
first startup.

Keybindings work like any plugin action:

```toml
[[keys.command]]
key = "prefix+z"
type = "plugin_action"
command = "djbclark.herdr-sleeper.sleep-pane"
description = "sleep this agent pane"
```

## Safety rules (all fail closed)

Inherited verbatim from the standalone script; the deltas the plugin
introduces are marked.

1. Eligible only when: a supported kind with a session id (and, for claude,
   an on-disk transcript — nothing to resume otherwise); Herdr status
   `idle`/`done`; pane not focused; not excluded; session id not open in
   another pane; idle ≥ window; original argv safely replayable
   (`--fork-session`, `--print`, `--session-id`, positional prompts,
   anything after `--`, options of unknown arity → refuse).
2. Everything is re-checked after the journal write and immediately before
   the exit, including that `state_change_seq` and the session id have not
   moved; the exit targets the pane id, never the name; "exited" needs
   Herdr to drop the agent *and* process-info to show no agent process —
   unreadable process-info is unknown, not gone. *Delta:* the exit is a
   SIGTERM to the pane's identified agent process (refused when the pid
   cannot be identified) — a signal cannot append to a composer draft the
   way a typed `/exit` could, which is what the old positive-empty-composer
   check guarded against. That check still guards kinds exited by typing.
3. The record is written *before* the exit. If the agent is still there
   after the wait it stays `exit-requested`; the next tick reconciles
   (dropped only after two *consecutive* sightings with a real process, or
   when the pane runs a different session; `asleep` once verifiably gone;
   `displaced` when our own wake put another session there — by hand
   only). A record is never deleted because its pane vanished; the manual
   resume command is printed instead. A pane that moved (new pane id, same
   `terminal_id`) is followed.
4. Wake refuses if the session id is live in any pane or any process that
   selects it, when that cannot be verified, or when the pane's cwd or
   `terminal_id` no longer matches the record (recycled pane id). *Delta:*
   focus-wakes are debounced by a per-pane lock (one click fires several
   `pane.focused` hooks); the in-pane stub runs the same checks before it
   execs, and refuses when the shell's cwd no longer matches the record.
5. A non-object state file, malformed config, unknown keys, booleans where
   numbers go, conflicting env aliases, or a `nan`/negative/`inf` window
   disables sleeping (logged every tick) — `wake`/`list`/`log` keep
   working, and damaged state files are quarantined to
   `*.damaged-<timestamp>`, never overwritten. A file lock serialises
   overlapping runs; the journal is re-read under it before every write.

Known limits: SIGTERM leaves a small window between the final recheck and
the process handling it (only a native Herdr operation could close it);
the stub's typed text path consumes anything typed into a slept pane
before Enter.

## Tests

`python3 -m pytest tests/ -q` — 77 tests: the decision logic, the
sleep/wake state machine, the watcher tick's idle clock, the focus hook,
the stub, and legacy migration, against a fake `herdr`. Verified on
Python 3.9 (Apple's `/usr/bin/python3`) and 3.14.

## Credits

- [`dalogax/herdr-agent-hibernate`](https://github.com/dalogax/herdr-agent-hibernate) —
  the SIGTERM exit path, the seq-based idle clock, wake-on-focus with a
  per-pane lock, `terminal_id` identity, code-stamped watcher replacement,
  `agent_name_taken` handling ideas.
- `prabhatgmp/herdr-park` — the sidebar claim (`pane report-agent`), the
  in-pane Enter stub, `notification show`, freed-MB reporting.
- [`scripts/herdr-sleeper`](https://github.com/djbclark/herdr/tree/herdr-sleeper/scripts/herdr-sleeper)
  (the standalone ancestor) — everything fail-closed.
