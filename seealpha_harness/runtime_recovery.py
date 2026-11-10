"""Opt-in durable bookkeeping and fail-closed recovery for task Docker workloads.

Only the trusted controller may use this module. Reconciliation is explicit:
it never auto-resumes a research Agent, never scans arbitrary Docker objects,
and never includes task grants, prompts or secrets in its disk journal.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Callable

from .store import atomic_json, utcnow
from .task_runtime import DockerCommands, TaskWorkloads, WorkloadSpec, resource_names

_PHASES = frozenset({"planned", "running", "cleanup_pending", "cleaned"})
_SCHEMA = 1


class RuntimeJournal:
    """Private on-disk journal; single trusted controller owns this directory."""

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.stat().st_mode & 0o077:
            raise ValueError("Runtime journal directory must be private")

    def _path(self, tenant_id: str, task_id: str) -> Path:
        network, _, _ = resource_names(tenant_id, task_id)
        return self.root / (network + ".json")

    @staticmethod
    def _validate(record: dict, filename: str) -> dict:
        if not isinstance(record, dict) or set(record) != {
            "schema", "tenant_id", "task_id", "phase", "created_at", "updated_at",
        } or record["schema"] != _SCHEMA or record["phase"] not in _PHASES:
            raise ValueError("Invalid runtime journal entry")
        if not all(isinstance(record[k], str) for k in (
            "tenant_id", "task_id", "created_at", "updated_at",
        )):
            raise ValueError("Invalid runtime journal fields")
        network, _, _ = resource_names(record["tenant_id"], record["task_id"])
        if filename != network + ".json":
            raise ValueError("Runtime journal identifier mismatch")
        return record

    def begin(self, spec: WorkloadSpec) -> dict:
        spec.validate()
        path = self._path(spec.tenant_id, spec.task_id)
        record = {
            "schema": _SCHEMA, "tenant_id": spec.tenant_id,
            "task_id": spec.task_id, "phase": "planned",
            "created_at": utcnow(), "updated_at": utcnow(),
        }
        # Create *before* any Docker mutation; do not overwrite a prior task.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle)
            handle.flush()
            os.fsync(handle.fileno())
        return record

    def read(self, tenant_id: str, task_id: str) -> dict:
        path = self._path(tenant_id, task_id)
        if path.is_symlink() or not path.is_file():
            raise ValueError("Runtime journal entry missing or unsafe")
        if path.stat().st_mode & 0o077 or path.stat().st_size > 4096:
            raise ValueError("Runtime journal entry permissions or size invalid")
        with path.open(encoding="utf-8") as handle:
            return self._validate(json.load(handle), path.name)

    def mark(self, tenant_id: str, task_id: str, phase: str) -> dict:
        record = self.read(tenant_id, task_id)
        transitions = {
            "planned": {"running", "cleanup_pending"},
            "running": {"cleanup_pending"},
            "cleanup_pending": {"cleanup_pending", "cleaned"},
            "cleaned": {"cleaned"},
        }
        if phase not in transitions[record["phase"]]:
            raise ValueError("Invalid runtime journal transition")
        record["phase"] = phase
        record["updated_at"] = utcnow()
        atomic_json(self._path(tenant_id, task_id), record)
        return record

    def pending(self) -> list[dict]:
        records = []
        for path in sorted(self.root.glob("*.json")):
            # Invalid/tampered entries fail closed rather than being skipped.
            if path.is_symlink() or path.stat().st_size > 4096:
                raise ValueError("Unsafe runtime journal entry")
            with path.open(encoding="utf-8") as handle:
                record = self._validate(json.load(handle), path.name)
            if record["phase"] != "cleaned":
                records.append(self.read(record["tenant_id"], record["task_id"]))
        return records


class DurableTaskWorkloads:
    """Journal before launch; explicitly recover/stop *only* journaled tasks.

    On controller restart, call reconcile_pending() from the trusted control
    plane before accepting new tasks. That operation revokes the Gateway lease
    and stops old containers; it does not replay the research prompt.
    """

    def __init__(
        self, docker: DockerCommands, *,
        journal: RuntimeJournal, revoke_lease: Callable[[str], None],
    ):
        self.docker = docker
        self.journal = journal
        self.revoke_lease = revoke_lease
        self.workloads = TaskWorkloads(docker, revoke_lease=revoke_lease)

    def launch(self, spec: WorkloadSpec) -> dict:
        self.journal.begin(spec)
        # A failed launch deliberately leaves a journal entry, even if the
        # existing best-effort rollback appeared to succeed.
        result = self.workloads.launch(spec)
        self.journal.mark(spec.tenant_id, spec.task_id, "running")
        return result

    def stop(self, tenant_id: str, task_id: str) -> None:
        self._reconcile(self.journal.read(tenant_id, task_id))

    def reconcile_pending(self) -> list[str]:
        cleaned = []
        for record in self.journal.pending():
            self._reconcile(record)
            cleaned.append(record["task_id"])
        return cleaned

    def _labels(self, kind: str, name: str) -> dict | None:
        if kind == "network":
            args = ("network", "inspect", "--format", "{{json .Labels}}", name)
        else:
            args = ("container", "inspect", "--format", "{{json .Config.Labels}}", name)
        try:
            result = self.docker.run(*args)
        except subprocess.CalledProcessError as exc:
            # Distinguish missing resources from Docker daemon/permission
            # errors; the latter must remain pending for a later retry.
            stderr = (exc.stderr or "").lower()
            if exc.returncode == 1 and "no such" in stderr:
                return None
            raise
        if not isinstance(getattr(result, "stdout", None), str):
            raise ValueError("Docker inspect response missing")
        labels = json.loads(result.stdout)
        if not isinstance(labels, dict):
            raise ValueError("Docker labels are not a mapping")
        return labels

    def _remove_owned(self, kind: str, name: str, owner: str, role: str) -> None:
        labels = self._labels(kind, name)
        if labels is None:
            return
        expected = {
            "seealpha.runtime": "research",
            "seealpha.owner": owner,
            "seealpha.role": role,
        }
        if any(labels.get(k) != v for k, v in expected.items()):
            raise ValueError("Refusing cleanup of unowned Docker resource")
        if kind == "network":
            self.docker.run("network", "rm", name)
        else:
            self.docker.run("rm", "-f", name)

    def _reconcile(self, record: dict) -> None:
        tenant_id, task_id = record["tenant_id"], record["task_id"]
        if self.journal.read(tenant_id, task_id)["phase"] == "cleaned":
            return
        self.journal.mark(tenant_id, task_id, "cleanup_pending")
        network, agent, bridge = resource_names(tenant_id, task_id)
        errors = []
        try:
            self.revoke_lease(task_id)
        except Exception as exc:
            errors.append(exc)
        for kind, name, role in (
            ("container", agent, "agent"),
            ("container", bridge, "bridge"),
            ("network", network, "task-network"),
        ):
            try:
                self._remove_owned(kind, name, network, role)
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise RuntimeError("Research task recovery incomplete; retry required") from errors[0]
        self.journal.mark(tenant_id, task_id, "cleaned")
