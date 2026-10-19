# GitHub Linux CI (ci/base)

CI runs on free GitHub-hosted runners via `ci/base` + `ci/run/**` (recipe
carried over from sudo-secretspec, 2026-10-18):

1. Workflow: `.github/workflows/linux-ci.yml` — lives only on the orphan
   `ci/base` branch, fires on pushes to `ci/run/**`.
2. Wrapper: `scripts/github-ci-run [--no-wait] [REF]` — overlays the workflow,
   wrapper and this doc onto REF (default HEAD) and force-pushes
   `ci/run/<slug>-<short>`; with wait it watches the run to completion.
3. The overlay commit removes any other `.github/workflows/*` so upstream
   workflows never fire on the `ci/run/**` push, and records the target SHA in
   `.ci-target-sha`.
4. Job: `python -m pytest tests -q` on ubuntu-latest, Python 3.13.

Requires a local `ci/base` or `origin/ci/base` (git fetch origin ci/base).
