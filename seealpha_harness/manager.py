"""Trusted local controller for one isolated development workload at a time."""

import json
import os
import signal
import subprocess
import sys
from threading import RLock, Thread

from .settings import Settings
from .store import TaskStore, TERMINAL


class BusyError(Exception):
    pass


class ConflictError(Exception):
    pass


class TaskManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = TaskStore(settings.state_dir)
        self.lock = RLock()
        self.active = {}
        self.store.recover()

    def _environment(self) -> dict:
        # Deliberately do not pass Platform/Engine credentials or the HTTP control token.
        allowed = (
            "PATH", "HOME", "LANG", "LC_ALL", "XDG_CACHE_HOME", "XDG_CONFIG_HOME",
            "DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DSH_SYSTEM_PROMPT",
            "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY",
        )
        env = {k: os.environ[k] for k in allowed if k in os.environ}
        env.update({
            "HARNESS_STATE_DIR": str(self.store.root),
            "HARNESS_MODEL": self.settings.model,
            "HARNESS_PROVIDER": self.settings.provider,
            "HARNESS_PROFILE": self.settings.profile,
            "PYTHONUNBUFFERED": "1",
        })
        return env

    def create(self, prompt: str) -> dict:
        with self.lock:
            if self.active:
                raise BusyError("A research task is already running")
            task = self.store.create(prompt)
            self._launch(task)
            return self.store.get(task["id"])

    def continue_task(self, task_id: str, prompt: str) -> dict:
        with self.lock:
            if self.active:
                raise BusyError("A research task is already running")
            task = self.store.get(task_id)
            if task["status"] != "completed":
                raise ConflictError("Only completed Sessions may be continued")
            next_turn = task["turn"] + 1
            self.store.change(task_id, prompt=prompt, status="queued", mode="resume",
                              turn=next_turn, final_response=None, error=None)
            self._launch(self.store.get(task_id))
            return self.store.get(task_id)

    def _launch(self, task: dict) -> None:
        task_id = task["id"]
        self.store.outcome_path(task_id).unlink(missing_ok=True)
        command = [sys.executable, "-m", "seealpha_harness.worker", "--task-id", task_id]
        try:
            with open(os.devnull, "wb") as sink:
                proc = subprocess.Popen(
                    command, env=self._environment(), cwd=self.store.workspace(task_id),
                    stdin=subprocess.DEVNULL, stdout=sink, stderr=sink, start_new_session=True,
                )
        except OSError:
            self.store.change(task_id, status="failed", error="worker_start_failed")
            return
        self.active[task_id] = proc
        self.store.change(task_id, status="running")
        Thread(target=self._watch, args=(task_id, proc), daemon=True).start()

    @staticmethod
    def _terminate(proc) -> None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def _watch(self, task_id: str, proc) -> None:
        timed_out = False
        try:
            proc.wait(timeout=self.settings.task_timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            self._terminate(proc)
            proc.wait()
        with self.lock:
            task = self.store.get(task_id)
            if task["status"] == "cancelling":
                status, error, response = "cancelled", None, None
            elif timed_out:
                status, error, response = "failed", "task_timeout", None
            elif proc.returncode == 0:
                try:
                    outcome = json.loads(self.store.outcome_path(task_id).read_text("utf-8"))
                except (OSError, ValueError, json.JSONDecodeError):
                    outcome = {}
                if outcome.get("ok") is True and outcome.get("finish_reason") == "completed":
                    status, error = "completed", None
                    response = str(outcome.get("final_response", ""))[:100_000]
                else:
                    status, error, response = "failed", "invalid_worker_outcome", None
            else:
                status, error, response = "failed", "worker_failed", None
                try:
                    outcome = json.loads(self.store.outcome_path(task_id).read_text("utf-8"))
                    error = outcome.get("error", error)
                except (OSError, ValueError, json.JSONDecodeError):
                    pass
            if task["status"] not in TERMINAL:
                self.store.change(task_id, status=status, error=error, final_response=response)
                self.store.append_event(task_id, "terminal", {"status": status, "error": error})
            self.active.pop(task_id, None)

    def cancel(self, task_id: str) -> dict:
        with self.lock:
            task = self.store.get(task_id)
            if task["status"] in TERMINAL:
                return task
            proc = self.active.get(task_id)
            if proc is None:
                return self.store.change(task_id, status="interrupted", error="worker_missing")
            if proc.poll() is not None:
                # Watcher still determines the final result; do not erase a completed task.
                return task
            task = self.store.change(task_id, status="cancelling")
            self._terminate(proc)
            return task

    def shutdown(self) -> None:
        with self.lock:
            procs = list(self.active.items())
        for task_id, proc in procs:
            with self.lock:
                task = self.store.get(task_id)
                if task["status"] not in TERMINAL:
                    self.store.change(task_id, status="cancelling")
            self._terminate(proc)
        for _, proc in procs:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
