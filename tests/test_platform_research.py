"""Task-scoped Platform CLI and resident Harness control contract."""
import hashlib
import time
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from seealpha_harness.api import build_app
from seealpha_harness.platform_research import PlatformResearchClient, ResearchCommandError
from seealpha_harness.settings import Settings

SERVICE = "Bearer " + "x" * 40
HEADERS = {
    "Authorization": SERVICE,
    "X-SeeAlpha-Harness-Contract-Version": "1",
}
TOKEN = "capability-" + "a" * 50


def _access():
    return {"origin": "https://platform.local", "token": TOKEN,
            "expires_at": int(time.time()) + 600}


def test_bound_research_credentials_not_visible_in_public_task_or_history(tmp_path):
    app = build_app(Settings(token="x" * 40, state_dir=tmp_path))
    with TestClient(app) as http:
        manager = app.state.manager
        seen = []
        def launch(task):
            seen.append(manager._environment(task["id"]))
            manager.store.change(task["id"], status="completed")
        manager._launch = launch
        task_id = str(uuid4())
        response = http.post("/v1/tasks", headers=HEADERS, json={
            "task_id": task_id, "prompt": "ETH research",
            "queue_if_busy": False, "research_access": _access(),
        })
        assert response.status_code == 202
        assert response.json()["id"] == task_id
        assert TOKEN not in response.text
        assert TOKEN not in http.get(f"/v1/tasks/{task_id}", headers=HEADERS).text
        assert TOKEN not in http.get("/v1/tasks", headers=HEADERS).text
        assert seen[0]["SEEALPHA_PLATFORM_RESEARCH_TOKEN"] == TOKEN
        assert "ENGINE_TOKEN" not in seen[0]
        assert "HARNESS_API_TOKEN" not in seen[0]
        assert http.post("/v1/tasks", headers=HEADERS, json={
            "task_id": task_id, "prompt": "collision",
            "queue_if_busy": False, "research_access": _access(),
        }).status_code == 409

        resumed = http.post(f"/v1/tasks/{task_id}/continue", headers=HEADERS, json={
            "prompt": "follow up", "queue_if_busy": False, "research_access": _access(),
        })
        assert resumed.status_code == 202
        assert resumed.json()["turn"] == 2
        assert TOKEN not in resumed.text


@pytest.mark.parametrize("payload", [
    {"prompt": "hi", "research_access": {"origin": "https://platform", "token": TOKEN,
                                     "expires_at": 2000000000}},
    {"prompt": "hi", "task_id": "not-a-uuid", "research_access": {
        "origin": "https://platform", "token": TOKEN, "expires_at": 2000000000,
    }},
    {"prompt": "hi", "task_id": str(uuid4()), "queue_if_busy": True,
     "research_access": _access()},
])
def test_rejects_unbound_or_queued_capabilities(tmp_path, payload):
    app = build_app(Settings(token="x" * 40, state_dir=tmp_path))
    with TestClient(app) as http:
        assert http.post("/v1/tasks", headers=HEADERS, json=payload).status_code in {409, 422}


def test_platform_client_fixed_commands_and_snapshot_checksum(tmp_path):
    task_id = str(uuid4())
    snapshot = "a" * 32
    data = b"synthetic-parquet"
    digest = hashlib.sha256(data).hexdigest()
    paths = []

    def handler(request):
        assert request.headers["authorization"] == "Bearer " + TOKEN
        assert request.url.host == "platform.local"
        assert request.method == "POST"
        paths.append(request.url.path)
        assert request.url.path.startswith(f"/api/research/tasks/{task_id}/commands/")
        if request.url.path.endswith("snapshot_get"):
            return httpx.Response(200, json={
                "snapshot_id": snapshot, "status": "ready", "format": "parquet",
                "size_bytes": len(data), "sha256": digest,
            })
        if request.url.path.endswith("snapshot_fetch"):
            return httpx.Response(200, headers={
                "Content-Type": "application/octet-stream",
            }, content=data)
        if request.url.path.endswith("backtest_submit"):
            assert b'"idempotency_key":"iteration-1"' in request.content
            return httpx.Response(202, json={"backtest_id": "bt-1"})
        return httpx.Response(200, json={"datasets": []})
    with PlatformResearchClient(
        "https://platform.local", TOKEN, task_id, transport=httpx.MockTransport(handler),
    ) as client:
        assert client.datasets() == {"datasets": []}
        path = client.fetch_snapshot(snapshot, tmp_path / "data")
        assert path.read_bytes() == data
        assert client.submit_backtest(
            start="2026-01-01", end="2026-01-02", starting_balance="1000",
            quote_currency="USDT", idempotency_key="iteration-1",
        ) == {"backtest_id": "bt-1"}
    assert len(paths) == 4


def test_platform_client_refuses_redirect_invalid_origin_and_cross_server_calls():
    task_id = str(uuid4())
    for origin in ("file:///etc/passwd", "https://host/subpath", "https://a@b"):
        with pytest.raises(ValueError):
            PlatformResearchClient(origin, TOKEN, task_id)
    with PlatformResearchClient(
        "https://platform.local", TOKEN, task_id,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(302, headers={"Location": "https://evil.local"}),
        ),
    ) as client:
        with pytest.raises(ResearchCommandError):
            client.datasets()
        with pytest.raises(ValueError):
            client._request("shell", {"command": "rm -rf /"})


def test_candidate_cli_package_and_submit_are_fixed_and_checksum_bound(tmp_path):
    import base64
    import io
    import json
    import zipfile

    source = tmp_path / "candidate.py"
    source.write_text(
        "CANDIDATE = {'kind':'ema_revert','symbol':'ETHUSDT','fast_period':8,"
        "'slow_period':21,'allocation':'1','risk_limit':'1'}\n",
        encoding="utf-8",
    )
    task_id = str(uuid4())
    registered = {}
    calls = []

    def handler(request):
        assert request.headers["authorization"] == "Bearer " + TOKEN
        payload = json.loads(request.content)["arguments"]
        calls.append(request.url.path)
        if request.url.path.endswith("/candidate_create"):
            raw = base64.b64decode(payload["archive"], validate=True)
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                assert "ema_revert" in archive.read("strategy.py").decode()
                manifest = json.loads(archive.read("seealpha-package.json"))
                assert manifest["entry_point"] == "strategy:CANDIDATE"
            registered["sha256"] = hashlib.sha256(raw).hexdigest()
            return httpx.Response(200, json={
                "candidate_id": "a" * 32, "package_hash": registered["sha256"],
            })
        if request.url.path.endswith("/candidate_submit"):
            assert payload["candidate_id"] == "a" * 32
            assert payload["idempotency_key"] == "iteration-2"
            return httpx.Response(202, json={"backtest_id": "b" * 32, "status": "queued"})
        return httpx.Response(200, json={"candidate_id": "a" * 32})

    with PlatformResearchClient(
        "https://platform.local", TOKEN, task_id,
        transport=httpx.MockTransport(handler),
    ) as client:
        created = client.register_candidate(source, version="2")
        assert created["package_hash"] == registered["sha256"]
        result = client.submit_candidate(
            candidate_id=created["candidate_id"], start="2026-01-01",
            end="2026-01-02", starting_balance="1000",
            quote_currency="USDT", idempotency_key="iteration-2",
        )
        assert result["status"] == "queued"
        assert client.get_candidate(created["candidate_id"])["candidate_id"] == created["candidate_id"]
    assert len(calls) == 3
    with PlatformResearchClient(
        "https://platform.local", TOKEN, task_id,
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(ValueError):
            client.register_candidate(source, name="../../evil")
