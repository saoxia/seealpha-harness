"""Resident FIFO queue: no Docker launch, no unsafe automatic replay."""
import json
import subprocess
import sys
import time
from threading import Event

import pytest
from fastapi.testclient import TestClient

from seealpha_harness.api import CONTRACT_HEADER, build_app
from seealpha_harness.manager import BusyError, QueueFullError, TaskManager
from seealpha_harness.settings import Settings
from seealpha_harness.store import TaskStore, atomic_json

HEADERS = {"Authorization": "Bearer " + "x" * 40, CONTRACT_HEADER: "1"}


class FakeProcess:
    def __init__(self, task_id):
        self.pid = 1000
        self.task_id = task_id
        self.ready = Event()
        self.returncode = None

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if not self.ready.wait(timeout or 3):
            raise subprocess.TimeoutExpired("fake worker", timeout)
        self.returncode = 0
        return 0


def setup_fake(monkeypatch, tmp_path):
    import seealpha_harness.manager as module
    started = []

    def fake_popen(command, **kwargs):
        assert command[:3] == [sys.executable, "-m", "seealpha_harness.worker"]
        assert "docker" not in command
        task_id = command[-1]
        atomic_json(tmp_path / "jobs" / f"{task_id}.outcome.json", {
            "ok": True, "finish_reason": "completed", "final_response": "fake done",
        })
        proc = FakeProcess(task_id)
        started.append(proc)
        return proc

    monkeypatch.setattr(module.subprocess, "Popen", fake_popen)
    return started


def wait_until(predicate, seconds=3):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate(), "timed out waiting for task transition"


def test_fifo_queue_default_busy_cancel_and_completion(tmp_path, monkeypatch):
    started = setup_fake(monkeypatch, tmp_path)
    manager = TaskManager(Settings(token="x" * 40, state_dir=tmp_path))
    first = manager.create("first")
    assert first["status"] == "running"
    with pytest.raises(BusyError):
        manager.create("legacy busy remains 409")
    second = manager.create("second", queue_if_busy=True)
    third = manager.create("third", queue_if_busy=True)
    fourth = manager.create("fourth", queue_if_busy=True)
    assert [task["id"] for task in manager.store.pending_queue()] == [
        second["id"], third["id"], fourth["id"],
    ]
    assert len(started) == 1
    assert manager.cancel(third["id"])["status"] == "cancelled"
    assert manager.cancel(third["id"])["status"] == "cancelled"
    started[0].ready.set()
    wait_until(lambda: len(started) == 2)
    assert started[1].task_id == second["id"]
    started[1].ready.set()
    wait_until(lambda: len(started) == 3)
    assert started[2].task_id == fourth["id"]
    started[2].ready.set()
    wait_until(lambda: manager.store.get(fourth["id"])["status"] == "completed")
    assert len(started) == 3
    assert not manager.active
    assert manager.store.get(first["id"])["status"] == "completed"
    assert manager.store.get(second["id"])["status"] == "completed"
    assert manager.store.get(third["id"])["status"] == "cancelled"
    for task in (first, second, third, fourth):
        assert manager.store.events_after(task["id"], 0)[-1]["type"] == "terminal"


def test_queue_limit_and_queued_session_continue(tmp_path, monkeypatch):
    started = setup_fake(monkeypatch, tmp_path)
    manager = TaskManager(Settings(token="x" * 40, state_dir=tmp_path, max_queued_tasks=1))
    first = manager.create("first")
    waiting = manager.create("waiting", queue_if_busy=True)
    with pytest.raises(QueueFullError):
        manager.create("excess", queue_if_busy=True)
    started[0].ready.set()
    wait_until(lambda: len(started) == 2)
    started[1].ready.set()
    wait_until(lambda: manager.store.get(waiting["id"])["status"] == "completed")
    result = manager.continue_task(first["id"], "resume")
    assert result["mode"] == "resume" and result["turn"] == 2
    # Existing summary belongs to the same task; queue preserves session identity.
    assert len(started) == 3
    started[2].ready.set()
    wait_until(lambda: manager.store.get(first["id"])["status"] == "completed")


def test_restart_suspends_queue_when_old_worker_may_still_live(tmp_path, monkeypatch):
    started = setup_fake(monkeypatch, tmp_path)
    store = TaskStore(tmp_path)
    old = store.create("old", queue_managed=True)
    store.change(old["id"], status="running")
    pending = store.create("still waiting", queue_managed=True)
    manager = TaskManager(Settings(token="x" * 40, state_dir=tmp_path))
    assert manager.recovery_blocked is True
    assert store.get(old["id"])["status"] == "interrupted"
    assert store.get(pending["id"])["status"] == "queued"
    assert started == []
    with pytest.raises(BusyError):
        manager.create("not opted in")
    with TestClient(build_app(Settings(token="x" * 40, state_dir=tmp_path / "api-other"))) as client:
        assert client.post(
            "/v1/queue/resume", headers=HEADERS, json={},
        ).status_code == 422
    state = manager.resume_after_recovery(confirm_stale_workers_stopped=True)
    assert state["recovery_blocked"] is False
    assert len(started) == 1 and started[0].task_id == pending["id"]
    started[0].ready.set()
    wait_until(lambda: manager.store.get(pending["id"])["status"] == "completed")


def test_queued_without_inflight_recovers_without_replaying_executed_task(tmp_path, monkeypatch):
    started = setup_fake(monkeypatch, tmp_path)
    store = TaskStore(tmp_path)
    pending = store.create("unstarted", queue_managed=True)
    legacy = store.create("legacy not queued")
    manager = TaskManager(Settings(token="x" * 40, state_dir=tmp_path))
    assert manager.recovery_blocked is False
    assert len(started) == 1 and started[0].task_id == pending["id"]
    assert manager.store.get(legacy["id"])["status"] == "interrupted"
    started[0].ready.set()
    wait_until(lambda: manager.store.get(pending["id"])["status"] == "completed")


def test_http_opt_in_queued_without_leaking_prompts(tmp_path, monkeypatch):
    started = setup_fake(monkeypatch, tmp_path)
    app = build_app(Settings(token="x" * 40, state_dir=tmp_path, max_queued_tasks=1))
    with TestClient(app) as client:
        first = client.post("/v1/tasks", headers=HEADERS, json={"prompt": "secret prompt"})
        assert first.status_code == 202
        assert client.post("/v1/tasks", headers=HEADERS, json={
            "prompt": "no queue",
        }).status_code == 409
        queued = client.post("/v1/tasks", headers=HEADERS, json={
            "prompt": "waiting prompt", "queue_if_busy": True,
        })
        assert queued.status_code == 202
        assert queued.json()["status"] == "queued"
        assert client.post("/v1/tasks", headers=HEADERS, json={
            "prompt": "overflow", "queue_if_busy": True,
        }).status_code == 429
        assert client.get("/v1/queue").status_code == 401
        state = client.get("/v1/queue", headers=HEADERS)
        assert state.status_code == 200
        assert state.json()["active_task_id"] == first.json()["id"]
        assert state.json()["pending_task_ids"] == [queued.json()["id"]]
        assert "secret prompt" not in state.text
        assert "waiting prompt" not in state.text
        assert client.post("/v1/queue/resume", headers=HEADERS, json={
            "confirm_stale_workers_stopped": False,
        }).status_code == 422
        started[0].ready.set()
        wait_until(lambda: len(started) == 2)
        started[1].ready.set()
        wait_until(lambda: client.get(
            "/v1/tasks/" + queued.json()["id"], headers=HEADERS,
        ).json()["status"] == "completed")
