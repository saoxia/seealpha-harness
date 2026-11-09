"""Small filesystem-backed task journal for the single-worker local prototype."""

import hashlib
import json
import os
from pathlib import Path
import tempfile
from datetime import datetime, timezone
from uuid import UUID, uuid4


TERMINAL = frozenset({"completed", "failed", "cancelled", "interrupted"})


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def task_id_checked(task_id: str) -> str:
    try:
        if str(UUID(task_id, version=4)) != task_id:
            raise ValueError
    except (ValueError, AttributeError, TypeError) as exc:
        raise KeyError("Task not found") from exc
    return task_id


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            os.fchmod(out.fileno(), 0o600)
            json.dump(payload, out, ensure_ascii=False)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class TaskStore:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        for directory in ("jobs", "workspaces", "homes", "artifacts"):
            (self.root / directory).mkdir(parents=True, exist_ok=True)

    def path(self, task_id: str) -> Path:
        return self.root / "jobs" / (task_id_checked(task_id) + ".json")

    def home(self, task_id: str) -> Path:
        return self.root / "homes" / task_id_checked(task_id)

    def workspace(self, task_id: str) -> Path:
        return self.root / "workspaces" / task_id_checked(task_id)

    def outcome_path(self, task_id: str) -> Path:
        return self.root / "jobs" / (task_id_checked(task_id) + ".outcome.json")

    def events_path(self, task_id: str) -> Path:
        return self.root / "jobs" / (task_id_checked(task_id) + ".events.jsonl")

    def create(self, prompt: str) -> dict:
        task_id = str(uuid4())
        self.workspace(task_id).mkdir(mode=0o700)
        self.home(task_id).mkdir(mode=0o700)
        task = {
            "id": task_id, "status": "queued", "prompt": prompt,
            "turn": 1, "mode": "new", "created_at": utcnow(),
            "updated_at": utcnow(), "final_response": None, "error": None,
        }
        atomic_json(self.path(task_id), task)
        return task

    def get(self, task_id: str) -> dict:
        try:
            with self.path(task_id).open(encoding="utf-8") as handle:
                return json.load(handle)
        except FileNotFoundError as exc:
            raise KeyError("Task not found") from exc

    def change(self, task_id: str, **fields) -> dict:
        task = self.get(task_id)
        task.update(fields, updated_at=utcnow())
        atomic_json(self.path(task_id), task)
        return task

    def recover(self) -> None:
        for path in (self.root / "jobs").glob("*.json"):
            if path.name.endswith(".outcome.json"):
                continue
            task_id = path.stem
            try:
                task = self.get(task_id)
            except (ValueError, KeyError, json.JSONDecodeError):
                continue
            if task["status"] not in TERMINAL:
                self.change(task_id, status="interrupted", error="controller_restarted")

    def append_event(self, task_id: str, kind: str, payload: dict) -> None:
        path = self.events_path(task_id)
        sequence = 1
        if path.exists():
            with path.open(encoding="utf-8") as existing:
                for line in existing:
                    if line.strip():
                        try:
                            sequence = max(sequence, int(json.loads(line)["sequence"]) + 1)
                        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
                            continue
        event = {"sequence": sequence, "timestamp": utcnow(), "type": kind, "payload": payload}
        # Only a single worker or its watcher writes per task at a time.
        with path.open("a", encoding="utf-8") as output:
            output.write(json.dumps(event, ensure_ascii=False) + "\n")
            output.flush()

    def events_after(self, task_id: str, after: int) -> list[dict]:
        path = self.events_path(task_id)
        if not path.exists():
            return []
        events = []
        with path.open(encoding="utf-8") as inp:
            for line in inp:
                try:
                    event = json.loads(line)
                    if int(event["sequence"]) > after:
                        events.append(event)
                except (ValueError, KeyError, TypeError, json.JSONDecodeError):
                    continue
                if len(events) >= 200:
                    break
        return events

    def write_summary(self, task_id: str, turn: int, content: str) -> dict:
        dest = self.root / "artifacts" / task_id_checked(task_id)
        dest.mkdir(mode=0o700, exist_ok=True)
        path = dest / f"summary-{turn}.md"
        raw = content.encode("utf-8")
        with path.open("xb") as out:
            out.write(raw)
        return {"id": f"summary-{turn}", "kind": "markdown", "mime_type": "text/markdown",
                "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}

    def list_artifacts(self, task_id: str) -> list[dict]:
        base = self.root / "artifacts" / task_id_checked(task_id)
        if not base.exists():
            return []
        items = []
        for path in sorted(base.glob("summary-*.md")):
            if not path.is_file() or path.is_symlink():
                continue
            raw = path.read_bytes()
            items.append({"id": path.stem, "kind": "markdown", "mime_type": "text/markdown",
                          "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
        return items

    def artifact_path(self, task_id: str, artifact_id: str) -> Path:
        if not artifact_id.startswith("summary-") or not artifact_id[8:].isdigit():
            raise KeyError("Artifact not found")
        path = self.root / "artifacts" / task_id_checked(task_id) / f"{artifact_id}.md"
        if path.is_symlink() or not path.is_file():
            raise KeyError("Artifact not found")
        return path
