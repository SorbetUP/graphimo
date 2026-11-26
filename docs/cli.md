# Graphimo CLI

Any agent or script with a shell can drive Graphimo without an MCP connector:

```bash
graphimo <command> [options]     # or: python3 -m graphimo <command> [options]
```

The CLI is a thin client over the local Graphimo service (the same service
the Zeron plugin uses). The service is **auto-started on first use** and
reused afterwards; its manifest lives at
`~/.local/state/graphimo/service.json` (override with `--manifest`).

## Output contract

- Success: JSON on stdout, exit code 0 — `{"ok": true, "result": ...}`.
- Service error: JSON on stderr, exit code 1 —
  `{"ok": false, "error": {"code": "cell_not_found", "message": "..."}}`.
- `--compact` (global or after the subcommand) prints single-line JSON.
- Project notebook path: `--path /proj/nb.py` or the `GRAPHIMO_PROJECT`
  environment variable.

## Commands

| Command | Service action | Notes |
| --- | --- | --- |
| `health` | `GET /v1/health` | service + open projects |
| `attach --url U --session-id S [--token T]` | kernel attach | marimo base URL, e.g. `http://127.0.0.1:2719`; session id is the marimo websocket session |
| `inspect` | `notebook.inspect` | cells, edges, ports, previews, statuses |
| `run <cell_id>` | `notebook.run` | runs the cell plus stale ancestors |
| `add-cell <source>` / `delete-cell <cell_id>` | `notebook.add_cell` / `notebook.delete` | |
| `connect --from-cell X --variable V --to-cell Y --input-ref R [--output-index N]` | `notebook.connect` | structured errors: `cycle_detected`, `invalid_connection`, `cell_not_found` |
| `disconnect (--cell-id C \| --instance-id I) --input R` | `notebook.disconnect` | |
| `set-literal (--cell-id C \| --instance-id I) --input R --value '<json>'` | `notebook.set_literal` | value is a JSON literal |
| `disable` / `enable` (`--cell-id` or `--instance-id`) | `notebook.disable_instance` / `enable_instance` | |
| `capture-output --cell-id C --result-var V [--output-index N] [--pin]` | `notebook.capture_output` | stores an artifact |
| `components` | `component.list` | static contracts of `components/` |
| `component-add --component prepare.py --callable normalize --output norm [--bindings '{"values": {"variable": "data"}}']` | `component.add` | bindings: `{"input": {"variable": "x"}}` or `{"input": {"literal": ...}}` |
| `controls` / `groups` / `metablocks` / `artifacts` / `runs` / `scope` | corresponding `.list` / `.current` actions | |
| `metablock-create --dialect pytorch.model --id m1 [--request '{}']` | `metablock.create` | |
| `addons [--addon graphimo.pytorch]` | `addon.list` / `addon.describe` | installed addons, capabilities, dialects |
| `events [--cursor C] [--limit N] [--wait-ms MS] [--follow]` | event drain | `--follow` streams until interrupted |
| `batch <ops.json or '[...]'> [--continue-on-error]` | batch execution | array of `{"id", "action", "args"}` |
| `do <action> '<args json>'` | any action | generic escape hatch, e.g. `do metablock.apply '{...}'` |

## Examples for agents

```bash
# Attach to a live marimo kernel, then inspect and run
export GRAPHIMO_PROJECT=/proj/nb.py
graphimo attach --url http://127.0.0.1:2719 --session-id live-1
graphimo inspect --compact | jq '.result.cells[].id'
graphimo run Hbol

# Connect an output to a node input
graphimo connect --from-cell Hbol --variable data --to-cell zKhV --input-ref values

# Watch a long training run
graphimo events --follow --wait-ms 1000

# Script a whole pipeline atomically
graphimo batch ops.json --continue-on-error
```

Changes made through the CLI are visible in the Zeron plugin canvas: the
plugin polls the service event stream while a notebook is open and refreshes
the graph when a mutating action completes.

## Low-level entry points

- `graphimo-adapter` — stdin JSON `{"path", "operation": "execute"|"batch"|"events"|"telemetry_config", ...}`; this is what the Zeron plugin itself calls.
- `graphimo-service` — run the local service in the foreground.
