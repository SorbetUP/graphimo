# Graphimo marimo runtime and startup logs

## Why startup can fail

Graphimo runs its small bridge with the `python3` command allowed by Zeron.
That interpreter may differ from the Python environment where marimo was
installed. For example, Zeron can resolve `python3` to a Conda base interpreter
that has no `marimo` module. In that case `marimo edit` cannot start, and
Graphimo reports the process log path and the missing-module error.

## How Graphimo selects marimo

When **Open notebook** is used, Graphimo:

1. Reuses a registered local marimo session for the selected notebook when one
   already exists.
2. Looks for `marimo==0.25.2` in the notebook's `.venv` or `venv`, the Python
   interpreter provided by Zeron, and Graphimo's managed environment.
3. Checks a `marimo` executable on `PATH` and uses it only when it reports the
   pinned version.
4. If no matching runtime exists, creates an isolated environment at
   `~/.local/share/graphimo/marimo-0.25.2/` and installs the pinned package
   there. It does not install into or change the global Python environment.
5. Starts `marimo edit` bound to `127.0.0.1`. Marimo selects an available port;
   the port and URL are not entered by the user.

The first automatic installation needs access to the Python package index. The
environment is reused for future notebooks. If an interrupted setup left an
incomplete environment, the next startup clears and recreates that Graphimo-
owned environment.

## Logs and troubleshooting

Graphimo stores logs outside the notebook directory:

- `~/.local/state/graphimo/problems.log` — append-only Graphimo problems.
- `~/.local/state/graphimo/marimo-<notebook-hash>.log` — environment setup and
  stdout/stderr from the managed marimo process for that notebook.

In Graphimo, open **Logs**, then choose **Read marimo log** to load the saved
problem and process output. **Export logs** saves the currently loaded log view
to a text file. No log file is written into the notebook, and `.graphimo.json`
continues to contain presentation state only.

If startup fails:

1. Open **Logs** and use **Read marimo log**.
2. Find the `marimo-<notebook-hash>.log` path in the error message if Graphimo
   could not finish creating the environment.
3. Check whether the package index was reachable and whether the log identifies
   a Python/venv creation or package installation error.
4. Retry **Open notebook** after correcting the environment or network issue.

Zeron needs the plugin's `commands` permission for `python3`/`curl` and its
`files` permission for the native notebook picker and log export dialog.

## Regression checks

The live bridge tests cover automatic port selection, startup and reuse after a
marimo WebSocket session is established. The network-backed private environment
bootstrap test is opt-in and must run under a Python that does not already have
marimo installed. For example:

```bash
python3 -m venv /tmp/graphimo-bootstrap-test
/tmp/graphimo-bootstrap-test/bin/python -m pip install pytest
GRAPHIMO_TEST_BOOTSTRAP=1 /tmp/graphimo-bootstrap-test/bin/python -m pytest -q \
  tests/test_luau_marimo_live.py::test_managed_launcher_bootstraps_pinned_marimo_when_python_has_no_marimo
```

Keep the shell's normal `PATH` so the test can find the Luau CLI. The test uses
a temporary `HOME`, installs marimo only into that test directory, and
terminates the test server when done. A successful clean-environment run was
verified on 2026-11-26 (`1 passed`).
