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

`GraphimoProject` is a convenience handle for execute, batch, events and close.
Closing it closes the project session and releases service-owned runtime
subscriptions when their addon supports unsubscribe. Closing a client does not
stop the shared service. Stop the foreground service with Ctrl+C; a detached
service can be stopped by terminating the PID recorded in the manifest.

## HTTP protocol

Every route uses JSON envelopes:

```json
{"ok": true, "result": {}}
```

Errors use a stable code and readable message:

```json
{"ok": false, "error": {"code": "invalid_action", "message": "..."}}
```

The versioned routes are:

| Route | Purpose |
| --- | --- |
| `GET /v1/health` | Protocol version and open project sessions |
| `POST /v1/projects/open` | Open/reuse a canonical local project file from `{"path":"..."}` |
| `POST /v1/projects/close` | Close a session from `{"project":"<id>"}` |
| `POST /v1/execute` | Call one application action with project, action and args |
| `POST /v1/batch` | Run ordered application actions with a per-step journal |
| `GET /v1/events` | Read a project event stream by cursor |

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

The event route accepts `project`, `cursor`, `limit` and `wait_ms` query
parameters. It returns `cursor`, `events` and `dropped`. The cursor is an
increasing decimal sequence. If old events were overwritten by the bounded
history, `dropped` reports how many sequences fell behind the supplied cursor.
The default page limit is 128, the maximum is 256, and long polling is capped
at one second. Host and addon events are JSON-bounded before entering the
journal.

## Request and trust bounds

- Listen and client connections are loopback-only.
- Each request needs the owner-only manifest's random bearer token.
- Request JSON is limited to 256 KiB and duplicate object keys are rejected.
- Individual events are limited to 64 KiB; the event journal retains a bounded
  number of recent events.
- Responses and batch journals are limited to 4 MiB.
- There are at most 128 operations per batch and 256 events per read.
- A project path must resolve to an existing local file.

The service does not expose arbitrary shell execution. It exposes only
`GraphimoApplication.execute` actions, batch execution of those actions and
the bounded project event stream. Zeron integration remains an adapter
milestone and must use this same service/application contract.
