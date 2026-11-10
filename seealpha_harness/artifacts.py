"""Local Research Artifact v1: explicit, immutable draft imports from task exports.

This is a single-user development feature, NOT a safe untrusted-tenant boundary.
Import never executes code or publishes strategies to the trading Engine. No
workspace tree enumeration is exposed to API clients. Content is copied into a
private artifact store only on an authenticated, completed-task import request.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .store import atomic_json, task_id_checked, utcnow


MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
_ARTIFACT_ID = re.compile(r"artifact-[0-9a-f]{32}\Z")
_EXPORT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_SUMMARY_ID = re.compile(r"summary-[1-9][0-9]*\Z")
_FORMATS = {
    ".md": ("markdown", "text/markdown"),
    ".py": ("source_code", "text/x-python"),
    ".csv": ("table", "text/csv"),
    ".json": ("structured_data", "application/json"),
    ".parquet": ("dataset", "application/vnd.apache.parquet"),
    ".png": ("image", "image/png"),
    ".txt": ("text", "text/plain"),
}
_PNG_HEADER = b"\x89PNG\r\n\x1a\n"


class ArtifactImportError(ValueError):
    """Requested workspace file cannot be imported as a safe draft."""


class ArtifactIntegrityError(ValueError):
    """A registered draft is corrupt or does not match its manifest."""


class ArtifactRegistry:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()

    def _directory(self, task_id: str) -> Path:
        directory = self.root / "artifacts" / task_id_checked(task_id)
        if directory.is_symlink():
            raise ArtifactIntegrityError("Artifact integrity check failed")
        return directory

    @staticmethod
    def _format(filename: str) -> tuple[str, str, str]:
        if not _EXPORT_NAME.fullmatch(filename) or ".." in filename:
            raise ArtifactImportError("Invalid export filename")
        ext = Path(filename).suffix.lower()
        if ext not in _FORMATS:
            raise ArtifactImportError("Unsupported artifact format")
        kind, mime = _FORMATS[ext]
        return ext, kind, mime

    @staticmethod
    def _read_regular(path: Path) -> bytes:
        if path.is_symlink():
            raise ArtifactIntegrityError("Artifact integrity check failed")
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) |
                         getattr(os, "O_NONBLOCK", 0))
        except OSError as exc:
            raise ArtifactIntegrityError("Artifact integrity check failed") from exc
        with os.fdopen(fd, "rb") as inp:
            info = os.fstat(inp.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_ARTIFACT_BYTES:
                raise ArtifactIntegrityError("Artifact integrity check failed")
            raw = inp.read(MAX_ARTIFACT_BYTES + 1)
            if len(raw) > MAX_ARTIFACT_BYTES:
                raise ArtifactIntegrityError("Artifact integrity check failed")
            return raw

    @staticmethod
    def _check_content(ext: str, content: bytes) -> None:
        if ext == ".png" and not content.startswith(_PNG_HEADER):
            raise ArtifactImportError("Invalid PNG artifact")
        if ext == ".parquet" and (len(content) < 8 or not
                                  (content.startswith(b"PAR1") and content.endswith(b"PAR1"))):
            raise ArtifactImportError("Invalid Parquet artifact")
        if ext == ".json":
            try:
                json.loads(content)
            except (ValueError, UnicodeDecodeError) as exc:
                raise ArtifactImportError("Invalid JSON artifact") from exc

    def import_from_workspace(self, task_id: str, source_file: str) -> dict:
        task_id = task_id_checked(task_id)
        if not isinstance(source_file, str) or source_file.count("/") != 1:
            raise ArtifactImportError("Export must be a file under exports/")
        folder, filename = source_file.split("/")
        if folder != "exports":
            raise ArtifactImportError("Export must be a file under exports/")
        ext, kind, mime = self._format(filename)
        workspace = self.root / "workspaces" / task_id
        export_dir = workspace / "exports"
        if workspace.is_symlink() or export_dir.is_symlink():
            raise ArtifactImportError("Unsafe export directory")
        try:
            dir_fd = os.open(export_dir, os.O_RDONLY |
                             getattr(os, "O_DIRECTORY", 0) |
                             getattr(os, "O_NOFOLLOW", 0))
        except OSError as exc:
            raise ArtifactImportError("Export directory unavailable") from exc
        try:
            fd = os.open(filename, os.O_RDONLY |
                         getattr(os, "O_NOFOLLOW", 0) |
                         getattr(os, "O_NONBLOCK", 0), dir_fd=dir_fd)
            with os.fdopen(fd, "rb") as inp:
                before = os.fstat(inp.fileno())
                if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                        or before.st_size > MAX_ARTIFACT_BYTES):
                    raise ArtifactImportError("Unsafe or oversized export file")
                raw = inp.read(MAX_ARTIFACT_BYTES + 1)
                after = os.fstat(inp.fileno())
                if (len(raw) > MAX_ARTIFACT_BYTES
                        or (before.st_ino, before.st_size, before.st_mtime_ns,
                            before.st_ctime_ns) !=
                           (after.st_ino, after.st_size, after.st_mtime_ns,
                            after.st_ctime_ns)):
                    raise ArtifactImportError("Export changed during import")
        except OSError as exc:
            raise ArtifactImportError("Export file unavailable") from exc
        finally:
            os.close(dir_fd)
        self._check_content(ext, raw)

        dest_dir = self._directory(task_id)
        if dest_dir.is_symlink():
            raise ArtifactImportError("Unsafe artifact directory")
        dest_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        artifact_id = "artifact-" + uuid4().hex
        content_path = dest_dir / (artifact_id + ext)
        meta_path = dest_dir / (artifact_id + ".manifest.json")
        manifest = {
            "schema_version": 1, "id": artifact_id, "artifact_id": artifact_id,
            "task_id": task_id, "kind": kind, "mime_type": mime,
            "filename": filename, "size": len(raw), "size_bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(), "created_at": utcnow(),
            "source_refs": ["workspace:" + source_file],
            "provenance": "untrusted_workspace", "approval_status": "unreviewed",
        }
        try:
            with content_path.open("xb") as output:
                os.fchmod(output.fileno(), 0o600)
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
            atomic_json(meta_path, manifest)
        except Exception:
            content_path.unlink(missing_ok=True)
            raise
        return manifest

    def _imported(self, task_id: str, artifact_id: str) -> tuple[dict, Path]:
        if not _ARTIFACT_ID.fullmatch(artifact_id):
            raise KeyError("Artifact not found")
        directory = self._directory(task_id)
        path = directory / (artifact_id + ".manifest.json")
        try:
            raw = self._read_regular(path)
            if len(raw) > 4096:
                raise ValueError
            meta = json.loads(raw)
            if not isinstance(meta, dict):
                raise ValueError
            filename = meta["filename"]
            ext, kind, mime = self._format(filename)
            expected_keys = {
                "schema_version", "id", "artifact_id", "task_id", "kind", "mime_type",
                "filename", "size", "size_bytes", "sha256", "created_at",
                "source_refs", "provenance", "approval_status",
            }
            if (set(meta) != expected_keys or meta["schema_version"] != 1
                    or meta["id"] != artifact_id or meta["artifact_id"] != artifact_id
                    or meta["task_id"] != task_id or meta["kind"] != kind
                    or meta["mime_type"] != mime or meta["provenance"] != "untrusted_workspace"
                    or meta["approval_status"] != "unreviewed"
                    or meta["source_refs"] != ["workspace:exports/" + filename]
                    or type(meta["size"]) is not int or not 0 <= meta["size"] <= MAX_ARTIFACT_BYTES
                    or meta["size_bytes"] != meta["size"]
                    or not isinstance(meta["created_at"], str)
                    or not re.fullmatch(r"[0-9a-f]{64}", meta["sha256"])):
                raise ValueError
        except FileNotFoundError as exc:
            raise KeyError("Artifact not found") from exc
        except (ArtifactIntegrityError, ArtifactImportError, OSError,
                ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            if not path.exists():
                raise KeyError("Artifact not found") from exc
            raise ArtifactIntegrityError("Artifact integrity check failed") from exc
        content_path = directory / (artifact_id + ext)
        return meta, content_path

    def _legacy_summary(self, task_id: str, artifact_id: str) -> tuple[dict, Path]:
        if not _SUMMARY_ID.fullmatch(artifact_id):
            raise KeyError("Artifact not found")
        path = self._directory(task_id) / (artifact_id + ".md")
        if not path.is_file() or path.is_symlink():
            raise KeyError("Artifact not found")
        raw = self._read_regular(path)
        return {
            "schema_version": 1, "id": artifact_id, "artifact_id": artifact_id,
            "task_id": task_id, "kind": "markdown", "mime_type": "text/markdown",
            "filename": artifact_id + ".md", "size": len(raw), "size_bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "created_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
            "source_refs": [], "provenance": "worker_summary",
            "approval_status": "unreviewed",
        }, path

    def resolve(self, task_id: str, artifact_id: str) -> tuple[dict, Path]:
        task_id = task_id_checked(task_id)
        if not isinstance(artifact_id, str):
            raise KeyError("Artifact not found")
        if _SUMMARY_ID.fullmatch(artifact_id):
            return self._legacy_summary(task_id, artifact_id)
        return self._imported(task_id, artifact_id)

    def read_bytes(self, task_id: str, artifact_id: str) -> tuple[dict, bytes]:
        meta, path = self.resolve(task_id, artifact_id)
        raw = self._read_regular(path)
        if len(raw) != meta["size_bytes"] or hashlib.sha256(raw).hexdigest() != meta["sha256"]:
            raise ArtifactIntegrityError("Artifact integrity check failed")
        return meta, raw

    def list(self, task_id: str) -> list[dict]:
        task_id = task_id_checked(task_id)
        directory = self._directory(task_id)
        if not directory.exists():
            return []
        if directory.is_symlink():
            raise ArtifactIntegrityError("Artifact integrity check failed")
        result = []
        for path in sorted(directory.glob("summary-*.md")):
            if _SUMMARY_ID.fullmatch(path.stem):
                result.append(self.read_bytes(task_id, path.stem)[0])
        for path in sorted(directory.glob("artifact-*.manifest.json")):
            artifact_id = path.name.removesuffix(".manifest.json")
            if _ARTIFACT_ID.fullmatch(artifact_id):
                result.append(self.read_bytes(task_id, artifact_id)[0])
        return result
