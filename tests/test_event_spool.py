import time

from graphimo import event_spool
from graphimo_pytorch.telemetry import EventQueue, PyTorchEvent


def test_project_event_spool_is_async_cursor_based_and_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(event_spool, "MAX_SPOOL_EVENTS", 2)
    project = tmp_path / "project.py"
    spool = event_spool.ProjectEventSpool(event_spool.event_spool_path(project, "model"))
    for index in range(4):
        assert spool.submit({"kind": "metric.scalar", "step": index})
    spool._pending.join()

    page = event_spool.drain_project_event_spool(project, "model", 0, 10)
    assert page["cursor"] == 4
    assert page["dropped"] == 2
    assert [item["step"] for item in page["events"]] == [2, 3]


def test_project_event_spool_rejects_invalid_and_oversized_events(tmp_path, monkeypatch):
    monkeypatch.setattr(event_spool, "MAX_EVENT_BYTES", 32)
    spool = event_spool.ProjectEventSpool(tmp_path / "events.jsonl")
    assert not spool.submit({"not_json": object()})
    assert not spool.submit({"payload": "x" * 40})
    time.sleep(0.01)
    assert spool.drain(0, 10) == {"cursor": 0, "events": [], "dropped": 2}


def test_event_queue_fanout_does_not_depend_on_primary_queue_capacity():
    events = EventQueue(maxsize=1)
    event = PyTorchEvent(
        1, "event-1", "probe", 1.0, "model", "revision", "runtime", None, None, (), None, {}
    )
    forwarded = []
    events.add_nonblocking_sink(forwarded.append)

    assert events.put(event)
    assert events.put(event) is False
    assert forwarded == [event, event]
