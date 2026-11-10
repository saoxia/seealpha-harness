"""Harness-only Research Artifact v1 tests: fake workspaces, no model/Engine/Docker."""
import hashlib
import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from seealpha_harness.api import CONTRACT_HEADER, build_app
from seealpha_harness.artifacts import (
    MAX_ARTIFACT_BYTES, ArtifactImportError, ArtifactIntegrityError,
)
from seealpha_harness.settings import Settings
from seealpha_harness.store import TaskStore

HEADERS = {"Authorization": "Bearer " + "x" * 40, CONTRACT_HEADER: "1"}


def prepared_task(tmp_path):
    store = TaskStore(tmp_path)
    task = store.create("Inspect a synthetic research result")
    exports = store.workspace(task["id"]) / "exports"
    exports.mkdir()
    store.change(task["id"], status="completed")
    return store, task["id"], exports


def test_typed_manifest_immutable_snapshot_and_backward_compatible_summary(tmp_path):
    store, task_id, exports = prepared_task(tmp_path)
    legacy = store.write_summary(task_id, 1, "# Summary")
    assert legacy["id"] == "summary-1"
    report = exports / "pnl.csv"
    report.write_text("t,pnl\n1,2\n", encoding="utf-8")
    meta = store.import_artifact(task_id, "exports/pnl.csv")
    assert meta["schema_version"] == 1
    assert meta["task_id"] == task_id
    assert meta["kind"] == "table"
    assert meta["mime_type"] == "text/csv"
    assert meta["approval_status"] == "unreviewed"
    assert meta["provenance"] == "untrusted_workspace"
    assert meta["source_refs"] == ["workspace:exports/pnl.csv"]
    assert meta["size"] == 10
    assert meta["sha256"] == hashlib.sha256(b"t,pnl\n1,2\n").hexdigest()

    report.write_text("mutated after import", encoding="utf-8")
    loaded = TaskStore(tmp_path)
    summary = loaded.read_artifact(task_id, "summary-1")
    assert summary[1] == b"# Summary" and summary[0]["schema_version"] == 1
    assert loaded.read_artifact(task_id, meta["id"])[1] == b"t,pnl\n1,2\n"
    assert {a["id"] for a in loaded.list_artifacts(task_id)} == {
        "summary-1", meta["id"],
    }
    assert loaded.read_artifact(task_id, meta["id"])[0] == meta
    assert loaded.read_artifact(task_id, "summary-1")[0]["kind"] == "markdown"
    assert not (tmp_path / "artifacts" / task_id / "mutated after import").exists()


@pytest.mark.parametrize("name,data,kind", [
    ("alpha.py", b"print(1)\n", "source_code"),
    ("hypotheses.json", b'{"ideas": [1, 2]}', "structured_data"),
    ("figure.png", b"\x89PNG\r\n\x1a\n" + b"x" * 9, "image"),
    ("series.parquet", b"PAR1" + b"data" + b"PAR1", "dataset"),
    ("memo.md", b"# Findings", "markdown"),
    ("notes.txt", b"synthetic notes", "text"),
])
def test_supported_draft_formats(tmp_path, name, data, kind):
    store, task_id, exports = prepared_task(tmp_path)
    (exports / name).write_bytes(data)
    meta = store.import_artifact(task_id, "exports/" + name)
    assert meta["kind"] == kind
    assert store.read_artifact(task_id, meta["id"])[1] == data


@pytest.mark.parametrize("source_file", [
    "../secrets.txt", "/etc/passwd", "exports/../secret.csv",
    "exports/sub/file.csv", "not-exports/result.csv",
    "exports/.env", "exports/evil..csv", "exports/result.sh",
    "exports/interactive.html", "exports/chart.svg",
    "exports/script.js", "exports/", "exports\\result.csv",
])
def test_rejects_unsafe_paths_and_extensions(tmp_path, source_file):
    store, task_id, exports = prepared_task(tmp_path)
    with pytest.raises(ArtifactImportError):
        store.import_artifact(task_id, source_file)


def test_rejects_symlinks_hardlinks_and_special_files(tmp_path):
    store, task_id, exports = prepared_task(tmp_path)
    (exports / "regular.csv").write_text("a,b", encoding="utf-8")
    (exports / "link.csv").symlink_to("regular.csv")
    with pytest.raises(ArtifactImportError):
        store.import_artifact(task_id, "exports/link.csv")

    os.link(exports / "regular.csv", exports / "hard.csv")
    with pytest.raises(ArtifactImportError):
        store.import_artifact(task_id, "exports/hard.csv")

    (exports / "directory.csv").mkdir()
    with pytest.raises(ArtifactImportError):
        store.import_artifact(task_id, "exports/directory.csv")

    other_store, other_id, other_exports = prepared_task(tmp_path / "other")
    other_exports.rmdir()
    other_exports.symlink_to(exports, target_is_directory=True)
    with pytest.raises(ArtifactImportError):
        other_store.import_artifact(other_id, "exports/regular.csv")


def test_oversize_invalid_content_and_not_completed(tmp_path):
    store, task_id, exports = prepared_task(tmp_path)
    path = exports / "data.csv"
    with path.open("wb") as out:
        out.truncate(MAX_ARTIFACT_BYTES + 1)
    with pytest.raises(ArtifactImportError):
        store.import_artifact(task_id, "exports/data.csv")

    (exports / "bad.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ArtifactImportError):
        store.import_artifact(task_id, "exports/bad.json")
    (exports / "bad.png").write_bytes(b"not a PNG")
    with pytest.raises(ArtifactImportError):
        store.import_artifact(task_id, "exports/bad.png")
    (exports / "bad.parquet").write_bytes(b"incorrect")
    with pytest.raises(ArtifactImportError):
        store.import_artifact(task_id, "exports/bad.parquet")

    store.change(task_id, status="running")
    with pytest.raises(ValueError, match="completed"):
        store.import_artifact(task_id, "exports/data.csv")


def test_detects_tampered_bytes_and_manifest(tmp_path):
    store, task_id, exports = prepared_task(tmp_path)
    (exports / "report.csv").write_bytes(b"date,pnl\n")
    meta = store.import_artifact(task_id, "exports/report.csv")
    stored = store.artifact_path(task_id, meta["id"])
    stored.write_bytes(b"altered")
    with pytest.raises(ArtifactIntegrityError):
        store.read_artifact(task_id, meta["id"])
    with pytest.raises(ArtifactIntegrityError):
        store.list_artifacts(task_id)

    stored.write_bytes(b"date,pnl\n")
    manifest_path = tmp_path / "artifacts" / task_id / (meta["id"] + ".manifest.json")
    modified = json.loads(manifest_path.read_text())
    modified["size"] = -1
    manifest_path.write_text(json.dumps(modified))
    with pytest.raises(ArtifactIntegrityError):
        store.read_artifact(task_id, meta["id"])


def test_task_scope_and_unregistered_files_not_listed(tmp_path):
    store, task_id, exports = prepared_task(tmp_path)
    (exports / "orphan.csv").write_bytes(b"secret")
    another = store.create("another task")
    store.change(another["id"], status="completed")
    assert store.list_artifacts(task_id) == []
    assert store.list_artifacts(another["id"]) == []
    with pytest.raises(KeyError):
        store.read_artifact(another["id"], "summary-1")
    with pytest.raises(KeyError):
        store.read_artifact(task_id, "../artifact-unsafe")


def test_authenticated_import_and_attachment_download(tmp_path):
    app = build_app(Settings(token="x" * 40, state_dir=tmp_path))
    with TestClient(app) as client:
        store = app.state.manager.store
        task = store.create("synthetic")
        exports = store.workspace(task["id"]) / "exports"
        exports.mkdir()
        (exports / "alpha.py").write_text("print('synthetic')", encoding="utf-8")
        base = f"/v1/tasks/{task['id']}/artifacts"
        body = {"source_file": "exports/alpha.py"}
        assert client.post(base + "/import", json=body).status_code == 401
        assert client.post(base + "/import", json=body, headers={
            "Authorization": HEADERS["Authorization"],
        }).status_code == 426
        assert client.post(base + "/import", json=body, headers=HEADERS).status_code == 409
        store.change(task["id"], status="completed")
        imported = client.post(base + "/import", json=body, headers=HEADERS)
        assert imported.status_code == 201, imported.text
        artifact_id = imported.json()["id"]
        assert imported.json()["approval_status"] == "unreviewed"
        assert client.get(base, headers=HEADERS).json()["artifacts"][0]["id"] == artifact_id
        assert client.get(base + "/" + artifact_id).status_code == 401
        result = client.get(base + "/" + artifact_id, headers=HEADERS)
        assert result.status_code == 200
        assert result.content == b"print('synthetic')"
        assert result.headers["content-type"] == "application/octet-stream"
        assert result.headers["x-content-type-options"] == "nosniff"
        assert result.headers["content-disposition"] == 'attachment; filename="alpha.py"'
        assert client.get(base + "/artifact-00000000000000000000000000000000", headers=HEADERS).status_code == 404
        (exports / "evil.html").write_text("<script>evil()</script>")
        assert client.post(base + "/import", headers=HEADERS, json={
            "source_file": "exports/evil.html",
        }).status_code == 422
        # The HTTP contract returns an integrity error, never corrupt content.
        store.artifact_path(task["id"], artifact_id).write_bytes(b"tampered")
        assert client.get(base + "/" + artifact_id, headers=HEADERS).status_code == 409
        assert client.get(base, headers=HEADERS).status_code == 409
