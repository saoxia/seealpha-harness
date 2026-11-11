"""Regression: task creation reuses resident Harness, never creates a Docker."""
import json
import sys
from threading import Event
from pathlib import Path

from seealpha_harness.manager import TaskManager
from seealpha_harness.settings import Settings
from seealpha_harness.store import atomic_json


def test_multiple_sequential_tasks_use_python_subprocess_in_same_controller(tmp_path, monkeypatch):
    import seealpha_harness.manager as module

    sdk_temp_dir = str(tmp_path / "sdk-tmp")
    monkeypatch.setenv("TMPDIR", sdk_temp_dir)
    monkeypatch.setenv("HARNESS_API_TOKEN", "controller-only")
    launches = []
    completed = Event()

    class FakeProcess:
        returncode = 0
        pid = 777

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

    def fake_popen(command, **kwargs):
        assert command[:3] == [sys.executable, "-m", "seealpha_harness.worker"]
        assert command[3] == "--task-id"
        assert "docker" not in command
        assert kwargs["env"]["HARNESS_STATE_DIR"] == str(tmp_path)
        assert kwargs["env"]["TMPDIR"] == sdk_temp_dir
        assert "HARNESS_API_TOKEN" not in kwargs["env"]
        task_id = command[4]
        assert Path(kwargs["cwd"]) == tmp_path / "workspaces" / task_id
        atomic_json(tmp_path / "jobs" / f"{task_id}.outcome.json", {
            "ok": True, "finish_reason": "completed",
            "final_response": "Synthetic test result",
        })
        launches.append(task_id)
        return FakeProcess()

    monkeypatch.setattr(module.subprocess, "Popen", fake_popen)
    manager = TaskManager(Settings(token="x" * 40, state_dir=tmp_path))
    watcher = manager._watch

    def watch_and_signal(*args):
        try:
            watcher(*args)
        finally:
            completed.set()

    manager._watch = watch_and_signal
    for i in range(2):
        completed.clear()
        task = manager.create(f"Research hypothesis {i}")
        assert completed.wait(timeout=5), "Worker watcher did not finish"
        assert manager.store.get(task["id"])["status"] == "completed"
        assert not manager.active
        assert (tmp_path / "workspaces" / task["id"]).is_dir()
        assert (tmp_path / "homes" / task["id"]).is_dir()

    assert len(set(launches)) == 2
    assert manager.store.get(launches[0])["final_response"] == "Synthetic test result"
    assert manager.store.get(launches[1])["final_response"] == "Synthetic test result"
