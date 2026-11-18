# Bootstrap pinned marimo when Python is missing it

- Request: Opening a notebook in Graphimo failed because Zeron launched /opt/homebrew/Caskroom/miniconda/base/bin/python3 without the marimo module. Fix the launch environment selection/bootstrap, document the behavior and logs, and commit the change.
- Level: plugin runtime startup
- Date: 2026-11-18
- Slug: bootstrap-pinned-marimo-when-python-is-missing-it

## Summary
- [x] Confirmed the launch failure comes from Zeron's `python3` being the
  Miniconda base interpreter without the pinned `marimo` package. Graphimo now
  locates a compatible environment or prepares a private pinned environment.

## Repro Steps
- [x] Open Graphimo in Zeron and choose a `.py` notebook.
- [x] Observe `Could not start marimo` and a process log reporting
  `/opt/homebrew/Caskroom/miniconda/base/bin/python3: No module named marimo`.

## Environment
- Zeron macOS plugin runtime.
- Graphimo 0.2.0 before the fix; marimo adapter pinned to 0.25.2.
- Launcher interpreter: `/opt/homebrew/Caskroom/miniconda/base/bin/python3`.

## Observed vs Expected
- Observed: the plugin called `python3 -m marimo`, but the interpreter had no
  `marimo` module, so the process exited before exposing its local session API.
- Expected: Graphimo should find a project environment or create a private
  environment with the tested marimo version, without asking for a port.

## Hypotheses
- [x] Confirmed: Graphimo assumed the Python executable allowed by Zeron was
  also the Python environment containing marimo. The screenshot's process log
  proves this assumption was false.

## Investigation Plan
- [x] Read the screenshot's exact interpreter path and process log.
- [x] Reproduced interpreter absence with `importlib.util.find_spec('marimo')`.
- [x] Exercised the corrected bootstrap using the same Miniconda interpreter
  and a temporary home directory.

## Fix Plan
- [x] Check project venvs, Zeron's Python and a `marimo` CLI for version 0.25.2.
- [x] Create a Graphimo-owned private venv and install marimo 0.25.2 only when
  no compatible environment exists.
- [x] Store setup/install and marimo stdout/stderr in the per-notebook log.
- [x] Keep the external marimo service on loopback and let marimo choose its
  free port.

## Regression Tests
- [x] Live pinned-marimo startup and session reuse after WebSocket readiness.
- [x] Opt-in no-marimo interpreter test that bootstraps 0.25.2 into a temporary
  `HOME`, confirms the server responds, and terminates its process.
- [x] Full pytest suite, Luau parse, Ruff and Git whitespace checks.

## Release Notes
- [x] Graphimo plugin version 0.2.1. See `docs/marimo-runtime.md`.

## Risks
- [x] First-time auto-install requires package-index access. It writes only to
  `~/.local/share/graphimo/marimo-0.25.2/`; it does not alter global Python.

## Rollout
- [x] Zeron plugin uses the existing `python3` command permission. The private
  environment bootstrap is opt-in tested in the suite because it downloads
  packages; its actual run is validated locally with a temporary home.
