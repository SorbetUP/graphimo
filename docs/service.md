# Graphimo local application service

The service is a persistent, loopback-only HTTP+JSON transport to
`GraphimoApplication`. It owns project and marimo sessions while running. It is
not an MCP server and does not schedule notebook cells; marimo remains the
execution engine.

## Start and stop

Install Graphimo, then start the service:

```bash
graphimo-service --host 127.0.0.1 --port 0
```

Port `0` asks the OS to select a free port. The default discovery record is
`~/.graphimo/service.json`; `GRAPHIMO_SERVICE_FILE` or `--discovery-file` can
select another path. It contains the loopback address, port, process ID and
unguessable token. The service sets the discovery directory to mode `0700` and
the file to `0600` where the platform supports those permissions.

`GraphimoClient.connect_local()` reuses the discovered service or starts one in
the background when none responds. Call `client.stop_service()` to stop that
service and close its active projects. Closing one `GraphimoProject` leaves the
service running.

## Python client

```python
from graphimo.client import GraphimoClient, expr

client = GraphimoClient.connect_local()
project = client.open_project("experiment.py")

loader = project.add("load_dataset")
normalize = project.add("normalize", image=None)
project.connect(loader.output(0), normalize.input("image"))
normalize.run()
print(normalize.inspect())

project.close()
```

`project.add()` selects a unique palette title or exact palette ID. Keyword
values become Python literals (`"hello"` becomes a quoted string); wrap source
expressions with `expr("variable_name")`. `BlockReference.output()` accepts an
output index or a named Group output. All these operations call the same
application actions as other renderers.

Create a new ordinary marimo project file when needed:

```python
project = client.create_project("experiments/new_run.py")
```

Creation refuses to overwrite an existing file.

## Atomic and batch actions

`project.execute(action, **args)` sends one action exposed by the current
`GraphimoApplication`. The application validates and performs the operation.

`project.batch(operations)` sends an ordered plan in one request:

```python
result = project.batch([
    {
        "id": "source",
        "action": "add_palette_item",
        "args": {"item_id": "load_dataset", "bindings": {}},
    },
    {
        "id": "normalize",
        "action": "add_palette_item",
        "args": {
            "item_id": "normalize",
            "bindings": {"image": {"$ref": "source.result.result_var"}},
        },
    },
])
```

References resolve fields from earlier operation results. Operations run in
order, stop at the first error by default, and keep successful prior edits.
There is no implied multi-operation rollback. Each response includes a
per-operation journal and the final application view state. Batches are capped
at 256 operations.

## HTTP endpoints

All semantic endpoints require `Authorization: Bearer <token>` from the local
discovery record. The service accepts JSON requests up to 1 MB and responses up
to 4 MB.

- `GET /health` — process health only.
- `POST /v1/projects/create` — create a new marimo project and open a session.
- `POST /v1/projects/open` — open or reuse an active session for a file path.
- `POST /v1/projects/close` — close one project session.
- `POST /v1/execute` — call one application action with `project_id`, `action`
  and optional object `args`.
- `POST /v1/batch` — run an ordered list of application actions.
- `GET /v1/events?project=<id>&cursor=<n>&limit=<n>&wait_ms=<n>` — bounded
  incremental event retrieval with long polling (maximum 30 seconds).
- `POST /v1/shutdown` — stop the service and close all active project sessions.

The server rejects non-loopback binds, malformed JSON, bad tokens, oversized
requests and invalid event cursors with structured error codes. Event producers
write to a bounded in-memory journal without waiting for consumers. The stream
includes application operation start/completion/failure events and incremental
MetaBlock addon events; dropped entries are reported so consumers can resync.
The journal retains at most 512 events per project, replaces individual event
payloads larger than 128 KB with a truncation notice, and trims each response to
the 4 MB transport bound.

The current service transports addon events and application operation state.
Direct notebook stdout/stderr and generic Python execution telemetry remain in
marimo's own runtime channels.
