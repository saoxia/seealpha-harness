"""No-model MCP Bridge tests with a fake task-scoped Gateway endpoint."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from starlette.testclient import TestClient

from seealpha_harness.mcp_bridge import BridgeClient, BridgeError, BridgeSettings, build_bridge
from seealpha_harness.mcp_profile import build_research_profile_patch
from seealpha_harness.store import TaskStore
from seealpha_harness.worker import execute

TOKEN = "task-bridge-token-of-high-entropy-xxxxxxxxxxxx"
TASK = "research-001"
GATEWAY = "http://gateway-private:8780"
PUBLIC = "http://task-bridge:8782"
PARQUET = b"PAR1mock-data-PAR1"
GW_HEADER = "X-SeeAlpha-Gateway-Contract-Version"


class MockGateway:
    def __init__(self):
        self.calls = []
        self.revoked = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        assert request.url.host == "gateway-private"
        assert request.headers["authorization"] == "Bearer " + TOKEN
        assert request.headers[GW_HEADER] == "1"
        assert "/bridge/v1/tasks/" + TASK + "/" in request.url.path
        if self.revoked:
            return httpx.Response(403, headers={GW_HEADER: "1"}, json={})
        if request.url.path.endswith("/tools/engine.datasets.discover"):
            return httpx.Response(200, headers={GW_HEADER: "1"}, json={
                "result": {"datasets": [{"id": "futures_um:ETHUSDT:1m"}]},
            })
        if request.url.path.endswith("/tools/engine.snapshots.fetch"):
            body = json.loads(request.content)
            return httpx.Response(200, headers={GW_HEADER: "1"}, json={
                "result": {
                    "snapshot_id": body["arguments"]["snapshot_id"],
                    "filename": "snap-1.parquet", "size_bytes": len(PARQUET),
                },
            })
        if request.url.path.endswith("/snapshots/snap-1/content"):
            return httpx.Response(
                200, headers={GW_HEADER: "1"}, content=PARQUET,
            )
        return httpx.Response(404, headers={GW_HEADER: "1"}, json={})


@pytest.fixture
def settings():
    return BridgeSettings(
        task_id=TASK, task_token=TOKEN,
        gateway_origin=GATEWAY, public_origin=PUBLIC,
    )


def test_task_pinned_client_disallows_arbitrary_paths_and_hides_token(settings):
    gateway = MockGateway()
    client = BridgeClient(settings, transport=httpx.MockTransport(gateway))
    assert client.invoke("engine.datasets.discover", {})["datasets"][0][
        "id"
    ] == "futures_um:ETHUSDT:1m"
    with pytest.raises(BridgeError):
        client.invoke("engine.orders.submit", {"side": "BUY"})
    with pytest.raises(BridgeError):
        client.invoke("engine.snapshots.fetch", {"payload": "X" * 70_000})
    assert len(gateway.calls) == 1
    gateway.revoked = True
    with pytest.raises(BridgeError) as error:
        client.invoke("engine.datasets.discover", {})
    assert error.value.code == "research_not_authorized"
    assert TOKEN not in str(error.value)
    client.close()


def test_mcp_tools_and_private_snapshot_relay(settings):
    fake = MockGateway()
    app = build_bridge(settings, transport=httpx.MockTransport(fake))
    async def inspect():
        response = await app.state.mcp_server.call_tool("discover_datasets", {})
        assert "ETHUSDT" in str(response)
        fetched = await app.state.mcp_server.call_tool(
            "fetch_snapshot", {"snapshot_id": "snap-1"},
        )
        assert "/snapshots/snap-1/content" in str(fetched)
        assert TOKEN not in str(fetched)
        failed = await app.state.mcp_server.call_tool(
            "fetch_snapshot", {"snapshot_id": "../etc/passwd"},
        )
        assert "error" in str(failed)
    with TestClient(app, base_url=PUBLIC) as client:
        asyncio.run(inspect())
        assert client.get("/health").json()["contract_version"] == 1
        downloaded = client.get("/snapshots/snap-1/content")
        assert downloaded.status_code == 200
        assert downloaded.content == PARQUET
        assert downloaded.headers["cache-control"] == "private, no-store"
        assert client.get("/snapshots/../secret/content").status_code in (404, 307)
    assert all(TOKEN not in str(call.url) for call in fake.calls)


def test_mcp_bridge_refuses_invalid_settings():
    with pytest.raises(ValueError):
        BridgeSettings(
            task_id="../another", gateway_origin=GATEWAY,
            public_origin=PUBLIC, task_token=TOKEN,
        )
    with pytest.raises(ValueError):
        BridgeSettings(
            task_id=TASK, gateway_origin="http://evil/private?token=x",
            public_origin=PUBLIC, task_token=TOKEN,
        )
    with pytest.raises(ValueError):
        BridgeSettings(
            task_id=TASK, gateway_origin=GATEWAY,
            public_origin=PUBLIC, task_token="weak",
        )


def test_sdk_patch_contains_only_mcp_url(tmp_path):
    path = build_research_profile_patch(PUBLIC, tmp_path / "profiles")
    data = path.read_text()
    assert "serverName: seealpha_research" in data
    assert "@deepseek-ai/dsh-mcp-client" in data
    assert "transport: streamable-http" in data
    assert PUBLIC + "/mcp" in data
    assert TOKEN not in data and "Authorization:" not in data
    with pytest.raises(ValueError):
        build_research_profile_patch(
            'http://evil"\nadmin: true', tmp_path / "injection",
        )


class FakeSession:
    def run(self, prompt, on_notification):
        return SimpleNamespace(final_response="Research summary", finish_reason="completed")


class FakeHarness:
    received = []

    def __init__(self, **kwargs):
        self.received.append(kwargs)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def start_session(self, session_id):
        return FakeSession()


def test_sdk_worker_is_opt_in_and_task_bound(tmp_path, monkeypatch):
    FakeHarness.received.clear()
    store = TaskStore(tmp_path)
    task = store.create("Analyze sample bars")
    monkeypatch.setenv("HARNESS_RESEARCH_MCP_ORIGIN", PUBLIC)
    monkeypatch.setenv("HARNESS_RESEARCH_MCP_TASK_ID", task["id"])
    assert execute(task["id"], store, runtime_factory=FakeHarness) == 0
    assert len(FakeHarness.received) == 1
    patch = FakeHarness.received[0]["patches"][0]
    assert Path(patch).is_file()
    assert PUBLIC in Path(patch).read_text()
    assert store.get(task["id"])["status"] == "queued"


def test_sdk_worker_rejects_wrong_task_bridge(tmp_path, monkeypatch):
    store = TaskStore(tmp_path)
    task = store.create("x")
    monkeypatch.setenv("HARNESS_RESEARCH_MCP_ORIGIN", PUBLIC)
    monkeypatch.setenv("HARNESS_RESEARCH_MCP_TASK_ID", "another-task")
    assert execute(task["id"], store, runtime_factory=FakeHarness) == 1
    assert store.outcome_path(task["id"]).is_file()


def test_mcp_http_handshake_and_tool_discovery(settings):
    """Exercise the real MCP Streamable HTTP transport without an LLM."""
    app = build_bridge(settings, transport=httpx.MockTransport(MockGateway()))
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": "2025-06-18",
    }
    with TestClient(app, base_url=PUBLIC) as client:
        initialized = client.post("/mcp", headers=headers, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "offline-test", "version": "1.0.0"},
            },
        })
        assert initialized.status_code == 200, initialized.text
        assert "serverInfo" in initialized.text
        available = client.post("/mcp", headers=headers, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/list",
            "params": {},
        })
        assert available.status_code == 200, available.text
        payload = available.json()
        names = {item["name"] for item in payload["result"]["tools"]}
        assert names == {
            "discover_datasets", "create_snapshot", "get_snapshot",
            "fetch_snapshot", "submit_backtest", "get_backtest",
            "cancel_backtest",
        }
        assert TOKEN not in available.text
