"""Single-container development controller with an opt-in durable FIFO queue.

Research tasks run as Python subprocesses in the resident Harness container:
no per-task Docker container, network, Engine instance, or privileged tool.
This is NOT a strong isolation boundary for arbitrary untrusted code.
"""
import json
import os
import signal
import subprocess
import sys
from threading import RLock, Thread

from .settings import Settings
from .store import TaskStore, TERMINAL, utcnow


class BusyError(Exception):
    pass


class QueueFullError(Exception):
    pass


class ConflictError(Exception):
    pass


class TaskManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = TaskStore(settings.state_dir)
        self.lock = RLock()
        self.active = {}
        self.watchers = {}
        self.stopping = False
        # An unexpected restart may have left a detached subprocess alive.
        # Preserve waiting jobs, but do NOT start them until an operator
        # verifies that no prior Worker is still running.
        interrupted_inflight = self.store.recover(preserve_managed_queue=True)
        self.recovery_blocked = bool(interrupted_inflight)
        with self.lock:
            self._dispatch_next()

    def _environment(self) -> dict:
        # Do not pass Platform/Engine credentials or the HTTP control token.
        allowed = (
            "PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "XDG_CACHE_HOME", "XDG_CONFIG_HOME",
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

    def queue_state(self) -> dict:
        with self.lock:
            return {
                "active_task_id": next(iter(self.active), None),
                "pending_task_ids": [task["id"] for task in self.store.pending_queue()],
                "recovery_blocked": self.recovery_blocked,
                "max_queued_tasks": self.settings.max_queued_tasks,
                "runtime_layout": "resident-container",
            }

    def _admit(self, *, queue_if_busy: bool) -> None:
        pending = self.store.pending_queue()
        busy = bool(self.active) or bool(pending) or self.recovery_blocked
        if busy and not queue_if_busy:
            raise BusyError("A research task is already running or queued")
        if len(pending) >= self.settings.max_queued_tasks:
            raise QueueFullError("Research queue capacity reached")

    def create(self, prompt: str, *, queue_if_busy: bool = False) -> dict:
        with self.lock:
            if self.stopping:
                raise BusyError("Research service is stopping")
            self._admit(queue_if_busy=queue_if_busy)
            task = self.store.create(prompt, queue_managed=True)
            self.store.append_event(task["id"], "progress", {"stage": "queued"})
            self._dispatch_next()
            return self.store.get(task["id"])

    def continue_task(self, task_id: str, prompt: str, *, queue_if_busy: bool = False) -> dict:
        with self.lock:
            if self.stopping:
                raise BusyError("Research service is stopping")
            task = self.store.get(task_id)
            if task["status"] != "completed":
                raise ConflictError("Only completed Sessions may be continued")
            self._admit(queue_if_busy=queue_if_busy)
            self.store.change(
                task_id, prompt=prompt, status="queued", mode="resume",
                turn=task["turn"] + 1, final_response=None, error=None,
                queue_managed=True, queued_at=utcnow(),
            )
            self.store.append_event(task_id, "progress", {"stage": "queued"})
            self._dispatch_next()
            return self.store.get(task_id)

    def _dispatch_next(self) -> None:
        # Called only under self.lock. Never auto-resume interrupted code.
        if self.stopping or self.recovery_blocked:
            return
        while not self.active:
            pending = self.store.pending_queue()
            if not pending:
                return
            self._launch(pending[0])

    def resume_after_recovery(self, *, confirm_stale_workers_stopped: bool) -> dict:
        if confirm_stale_workers_stopped is not True:
            raise ConflictError("Stale workers must be checked before queue resumption")
        with self.lock:
            if self.stopping:
                raise BusyError("Research service is stopping")
            self.recovery_blocked = False
            self._dispatch_next()
            return self.queue_state()

    def _launch(self, task: dict) -> None:
        task_id = task["id"]
        self.store.outcome_path(task_id).unlink(missing_ok=True)
        # Mark BEFORE spawn: after a crash a possibly spawned Worker must
        # be considered uncertain, not an unstarted queue item.
        self.store.change(task_id, status="starting")
        command = [sys.executable, "-m", "seealpha_harness.worker", "--task-id", task_id]
        try:
            with open(os.devnull, "wb") as sink:
                proc = subprocess.Popen(
                    command, env=self._environment(), cwd=self.store.workspace(task_id),
                    stdin=subprocess.DEVNULL, stdout=sink, stderr=sink, start_new_session=True,
                )
        except OSError:
            self.store.change(task_id, status="failed", error="worker_start_failed")
            self.store.append_event(task_id, "terminal", {
                "status": "failed", "error": "worker_start_failed",
            })
            return
        self.active[task_id] = proc
        self.store.change(task_id, status="running")
        watcher = Thread(target=self._watch, args=(task_id, proc), daemon=True)
        self.watchers[task_id] = watcher
        watcher.start()

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
            self.watchers.pop(task_id, None)
            self._dispatch_next()

    def cancel(self, task_id: str) -> dict:
        with self.lock:
            task = self.store.get(task_id)
            if task["status"] in TERMINAL:
                return task
            if task["status"] == "queued":
                task = self.store.change(task_id, status="cancelled", error=None)
                self.store.append_event(task_id, "terminal", {
                    "status": "cancelled", "error": None,
                })
                return task
            proc = self.active.get(task_id)
            if proc is None:
                # An in-flight process not owned by this controller cannot be
                # declared safely cancelled without an actual termination.
                task = self.store.change(task_id, status="interrupted", error="worker_missing")
                self.store.append_event(task_id, "terminal", {
                    "status": "interrupted", "error": "worker_missing",
                })
                return task
            if proc.poll() is not None:
                return task  # Watcher determines the true final state.
            task = self.store.change(task_id, status="cancelling")
            self._terminate(proc)
            return task

    def shutdown(self) -> None:
        with self.lock:
            self.stopping = True
            procs = list(self.active.items())
            watchers = list(self.watchers.values())
            for task_id, proc in procs:
                task = self.store.get(task_id)
                if task["status"] not in TERMINAL:
                    self.store.change(task_id, status="cancelling")
                self._terminate(proc)
        for _, proc in procs:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        for watcher in watchers:
            watcher.join(timeout=5)
