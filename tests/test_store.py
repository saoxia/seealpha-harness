import json
from pathlib import Path

import pytest

from seealpha_harness.store import TaskStore


def test_task_recovery_and_artifact_bounds(tmp_path: Path):
    store = TaskStore(tmp_path)
    task = store.create("Research a signal")
    store.append_event(task["id"], "progress", {"stage": "starting"})
    assert store.events_after(task["id"], 0)[0]["sequence"] == 1
    store.recover()
    assert store.get(task["id"])["status"] == "interrupted"
    summary = store.write_summary(task["id"], 1, "# Result")
    assert summary["kind"] == "markdown"
    assert store.list_artifacts(task["id"])[0]["sha256"] == summary["sha256"]
    assert store.artifact_path(task["id"], "summary-1").read_text() == "# Result"
    with pytest.raises(KeyError):
        store.get("../../etc/passwd")
    with pytest.raises(KeyError):
        store.artifact_path(task["id"], "../summary-1")


def test_recovery_does_not_corrupt_completed_task(tmp_path):
    store = TaskStore(tmp_path)
    task = store.create("x")
    store.change(task["id"], status="completed")
    store.recover()
    assert store.get(task["id"])["status"] == "completed"


def test_event_cursor(tmp_path):
    store = TaskStore(tmp_path)
    task = store.create("x")
    store.append_event(task["id"], "progress", {"number": 1})
    store.append_event(task["id"], "progress", {"number": 2})
    assert len(store.events_after(task["id"], 0)) == 2
    assert store.events_after(task["id"], 1)[0]["payload"]["number"] == 2
