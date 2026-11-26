# Local Graphimo service

The persistent service exposes the host-independent `GraphimoApplication` to
local renderers and automation. It is a transport around the application API;
it does not add a graph scheduler or take over marimo execution. The service
keeps one application and addon manager alive for each open project file.

## Start and connect

Install Graphimo, then start the service in the foreground:

```bash
python -m pip install -e .
graphimo-service --host 127.0.0.1 --port 0
```

Port `0` asks the OS to choose an available port. The service writes its
selected URL and random bearer token to
`$XDG_STATE_HOME/graphimo/service.json`, or
`~/.local/state/graphimo/service.json` when `XDG_STATE_HOME` is not set. The
manifest is atomically replaced and restricted to the current OS user. The
service binds only to `127.0.0.1`. The client rejects URLs on other hosts and
sends the token only in the `Authorization` header.

`GraphimoClient.connect_local()` reads that manifest and checks health. By
default it starts the service as a detached local process if no reachable
service is present; pass `auto_start=False` to require an already-running
service. Startup diagnostics are written beside the manifest in
`service.json.log`.

```python
from graphimo.client import GraphimoClient

client = GraphimoClient.connect_local()
project = client.open_project("/path/to/notebook.py")

scope = project.execute("scope.current")
print(scope["breadcrumbs"])

batch = project.batch(
    [
        {"id": "scope", "action": "scope.current"},
        {
            "id": "layout",
            "action": "layout.get",
            "args": {"scope_id": {"$ref": "scope.result.scope.scope_id"}},
        },
    ]
)
print(batch["operations"])

events = project.events(cursor="0", limit=128, wait_ms=750)
print(events["events"])
project.close()
client.close()
```

When a live marimo session is attached, ordinary component work can use the
same application intents as the Zeron canvas and agent tools:

```python
project.attach_kernel(url="http://127.0.0.1:2718", token="", session_id="session-id")
project.execute(
    "component.write",
    {
        "path": "normalize.py",
        "source": "from graphimo import node\n\n@node\ndef normalize(value: float) -> float:\n    return value / 2\n",
    },
)
added = project.add_component(
    "normalize.py",
    "normalize",
    output_name="normalized",
    bindings={"value": {"variable": "data"}},
)
snapshot = project.inspect_notebook()
project.run(added["cell_id"])
```

`project.connect(source_cell_id, variable, target_cell_id, input_ref)` applies
the same conservative source validation as the canvas. `project.run(cell_id)`
checks disabled dependencies, then asks marimo to run that cell and its normal
reactive dependents. `notebook.add_cell` is available for explicitly supplied
cell code; reusable components should use `component.add` or
`GraphimoProject.add_component` so component validation and source markers are
preserved. `project.disable_instance(cell_id)` and
`project.enable_instance(cell_id)` operate only on canonical Graphimo node
instance cells; disabling keeps source connections intact, and blocked
descendants are rejected by `run` until the upstream instance is enabled.
`project.set_literal(instance_id, input_name, value)` updates one validated
`@node` argument from a bounded JSON literal while preserving its other
bindings. `project.disconnect_instance(cell_id, input_name)` removes one
binding from a canonical instance; running that instance is refused with
`missing_required_binding` until every required input is bound again.
Notebook actions fail with `no live marimo kernel is attached` until
the project has been attached through this method or the Zeron session flow.

`GraphimoProject` is a convenience handle for execute, batch, events, ordinary
notebook inspect/add/connect/run operations and close.
Use `project.inspect_artifact(artifact_id)` to resolve a stored reference to
its stable ID and kind, and `project.artifact_stats()` to read logical bytes,
physical bytes, deduplication and object counts. The equivalent read-only
application actions are `artifact.inspect` and `artifact.stats`; agents can
call them directly or include them in a batch. Artifact inspection does not
materialize file contents or expose backend paths.
Closing it closes the project session and releases service-owned runtime
subscriptions when their addon supports unsubscribe. Closing a client does not
stop the shared service. Stop the foreground service with Ctrl+C; a detached
service can be stopped by terminating the PID recorded in the manifest.

The Zeron adapter uses the same `GraphimoClient` transport through a small
stdio command, without exposing the service token to Luau:

```bash
printf '%s\n' '{"path":"/path/to/notebook.py","operation":"execute","action":"scope.current","args":{}}' \
  | python3 -m graphimo.service.invoke
```

The command accepts `execute`, `batch` and `events` requests and returns the
same `{ok,result}` or `{ok:false,error}` JSON envelope. The Python environment
selected by Zeron must have the Graphimo distribution installed. The command
is transport-only; application actions and persistence remain in Graphimo's
Python Core.

## HTTP protocol

Every route uses JSON envelopes:

```json
{"ok": true, "result": {}}
```

Errors use a stable code and readable message:

```json
{"ok": false, "error": {"code": "invalid_action", "message": "..."}}
```

When an application or addon error carries a lowercase stable code such as
`pytorch_import_unsupported`, the HTTP service preserves it if it matches the
bounded `^[a-z][a-z0-9_]{0,127}$` form. Errors without a valid domain code use
`invalid_action`.

The versioned routes are:

| Route | Purpose |
| --- | --- |
| `GET /v1/health` | Protocol version and open project sessions |
| `POST /v1/projects/open` | Open/reuse a canonical local project file from `{"path":"..."}` |
| `POST /v1/projects/close` | Close a session from `{"project":"<id>"}` |
| `POST /v1/execute` | Call one application action with project, action and args |
| `POST /v1/batch` | Run ordered application actions with a per-step journal |
| `GET /v1/events` | Read a project event stream by cursor |
| `POST /v1/kernels/attach` | Attach an authenticated loopback marimo kernel port to a project session |
| `POST /v1/events/publish` | Ingest a bounded JSON event batch from the attached kernel |

The `/v1/execute` body is `{"project":"<id>","action":"scope.current","args":{}}`.
Batch operations use the same actions. A reference such as
`{"$ref":"create.result.metablock_id"}` reads a field from an earlier
successful operation. References cannot point forward or to failed steps.
Batch actions execute sequentially. By default, the first error marks later
steps `not_executed`; `continue_on_error: true` continues independent steps.
Completed side effects are not rolled back if a later step fails. The journal
is bounded to the response limit; if an action runs but its result cannot fit,
the step reports `batch_response_too_large` and later work stops because the
journal cannot fit in the response.

Component actions include `component.list`, `component.write`,
`component.validate`, `component.instance_source`, `component.add` and
recoverable `component.delete`. Component insertion uses the same validated
source builder and marimo host intent from the canvas and Python client. See
[Agent-authored components](components.md) for the write, validation and
instance-source contract.

`notebook.inspect`, `notebook.add_cell`, `notebook.connect`, `notebook.run`
and `notebook.delete` are bounded host intents forwarded to the attached
marimo code-mode context. They do not maintain a second graph or execution
queue. `notebook.inspect` identifies canonical Graphimo instances, their
bindings and declared output ports alongside marimo's live definitions,
references, previews and errors. `notebook.run` asks marimo to queue the requested cell; marimo owns the
resulting reactive execution. `notebook.connect` accepts only a canonical
Graphimo `@node` instance as its target, validates the declared input and
selected live source, then regenerates the instance through the Core source
builder. Ordinary user assignments are never rewritten. Component insertion
creates a separate reusable definition cell and canonical instance cell.

The event route accepts `project`, `cursor`, `limit` and `wait_ms` query
parameters. It returns `cursor`, `events` and `dropped`. The cursor is an
increasing decimal sequence. If old events were overwritten by the bounded
history, `dropped` reports how many sequences fell behind the supplied cursor.
The default page limit is 128, the maximum is 256, and long polling is capped
at one second. Host and addon events are JSON-bounded before entering the
journal.

Kernel attachment and event publication require the same owner-only service
bearer token as other routes. Attach accepts a loopback URL, marimo token and
session identity in the request body; the service keeps that configuration in
memory for the project session. Event publication accepts bounded JSON only
and validates the attached project/session identity. These credentials are
not saved in notebook source or project sidecars.

## Request and trust bounds

- Listen and client connections are loopback-only.
- Each request needs the owner-only manifest's random bearer token.
- Request JSON is limited to 5 MiB and duplicate object keys are rejected.
- Individual events are limited to 64 KiB; the event journal retains a bounded
  number of recent events.
- Responses and batch journals are limited to 4 MiB.
- There are at most 128 operations per batch and 256 events per read.
- A project path must resolve to an existing local file.

The service does not expose arbitrary shell execution. It exposes only
`GraphimoApplication.execute` actions, batch execution of those actions and
the bounded project event stream. The Zeron integration details and the
marimo/runtime boundary are described in the
[Zeron service adapter guide](zeron-service-adapter.md).
