"""Contract and security tests for a trusted *separate* gateway process.

All Engine responses and grants are simulated. No DeepSeek, exchange, or Docker.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from seealpha_harness.api import build_app
from seealpha_harness.gateway_api import GATEWAY_HEADER, build_gateway
from seealpha_harness.gateway_settings import GatewaySettings
from seealpha_harness.research_client import (
    ENGINE_HEADER, ENGINE_CONTRACT, RESEARCH_HEADER, RESEARCH_CONTRACT,
    EngineResearchClient,
)
from seealpha_harness.settings import Settings

SECRET = "test-platform-signing-secret-xxxxxxxxxxxxxx"
CONTROL_TOKEN = "gateway-control-token-xxxxxxxxxxxxxxxx"
ADMIN_TOKEN = "admin-engine-token-is-not-allowed"
TASK = "research-001"
TENANT = "dev-tenant"
DATASET = "futures_um:ETHUSDT:1m"
BYTES = b"PAR1synthetic-example-PAR1"
COMMON = {
    "Authorization": "Bearer " + CONTROL_TOKEN,
    GATEWAY_HEADER: "1",
}
ENGINE_HEADERS = {
    ENGINE_HEADER: ENGINE_CONTRACT,
    RESEARCH_HEADER: RESEARCH_CONTRACT,
}


def signed_grant(*, task=TASK, tenant=TENANT, ops=None, datasets=None,
                 strategies=None, now=None, lifetime=600):
    now = int(time.time()) if now is None else now
    claims = {
        "version": 1, "audience": "seealpha-engine-research",
        "tenant_id": tenant, "task_id": task,
        "ops": (["datasets:read", "snapshots:create", "snapshots:read",
                 "backtests:submit", "backtests:read", "backtests:cancel"]
                if ops is None else ops),
        "datasets": [DATASET] if datasets is None else datasets,
        "strategies": ["account:desk"] if strategies is None else strategies,
        "not_before": now - 5,
        "expires_at": now + lifetime,
    }
    payload = base64.urlsafe_b64encode(json.dumps(
        claims, separators=(",", ":"), sort_keys=True,
    ).encode()).decode().rstrip("=")
    sig = base64.urlsafe_b64encode(
        hmac.digest(SECRET.encode(), payload.encode(), "sha256"),
    ).decode().rstrip("=")
    return payload + "." + sig


class FakeEngine:
    def __init__(self):
        self.calls = []
        self.big_response = False
        self.invalid_grant = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        assert request.url.host == "research-engine"
        assert request.url.path.startswith("/research/v1/")
        assert request.headers[ENGINE_HEADER] == ENGINE_CONTRACT
        assert request.headers[RESEARCH_HEADER] == RESEARCH_CONTRACT
        auth = request.headers["authorization"]
        assert auth.startswith("Bearer ") and ADMIN_TOKEN not in auth
        token = auth[len("Bearer "):]
        payload, signature = token.split(".")
        expected = base64.urlsafe_b64encode(
            hmac.digest(SECRET.encode(), payload.encode(), "sha256"),
        ).decode().rstrip("=")
        if self.invalid_grant or not hmac.compare_digest(expected, signature):
            return httpx.Response(401, headers=ENGINE_HEADERS, json={"detail": "bad secret"})
        if request.url.path == "/research/v1/datasets":
            data = {"datasets": [{"id": DATASET, "symbol": "ETHUSDT"}]}
            if self.big_response:
                data["padding"] = "a" * 1_200_000
            return httpx.Response(200, headers=ENGINE_HEADERS, json=data)
        if request.url.path == "/research/v1/snapshots":
            return httpx.Response(202, headers=ENGINE_HEADERS, json={
                "snapshot_id": "snap-1", "status": "queued",
            })
        if request.url.path == "/research/v1/snapshots/snap-1":
            return httpx.Response(200, headers=ENGINE_HEADERS, json={
                "snapshot_id": "snap-1", "status": "ready", "format": "parquet",
                "size_bytes": len(BYTES), "sha256": hashlib.sha256(BYTES).hexdigest(),
            })
        if request.url.path == "/research/v1/snapshots/snap-1/content":
            return httpx.Response(200, headers=ENGINE_HEADERS, content=BYTES)
        if request.url.path == "/research/v1/backtests":
            body = json.loads(request.content)
            assert body["purpose"] == "development"
            assert body["execution"] == "simulation"
            assert body["strategy_ref"] == "account:desk"
            assert body["parameters"] == {}
            return httpx.Response(202, headers=ENGINE_HEADERS, json={
                "backtest_id": "bt-1", "status": "queued",
            })
        if request.url.path == "/research/v1/backtests/bt-1":
            return httpx.Response(200, headers=ENGINE_HEADERS, json={
                "backtest_id": "bt-1", "status": "completed",
                "result": {"net_pnl": "12.34", "mode": "simulation"},
            })
        if request.url.path == "/research/v1/backtests/bt-1/cancel":
            return httpx.Response(200, headers=ENGINE_HEADERS, json={
                "backtest_id": "bt-1", "status": "cancelled",
            })
        return httpx.Response(404, headers=ENGINE_HEADERS, json={})


@pytest.fixture
def gateway(tmp_path):
    engine = FakeEngine()
    settings = GatewaySettings(
        control_token=CONTROL_TOKEN, engine_origin="http://research-engine",
        state_root=tmp_path / "trusted-private",
        max_snapshot_bytes=128 * 1024 * 1024, max_calls_per_task=10,
    )

    def factory(grant):
        return EngineResearchClient(
            settings.engine_origin, grant,
            transport=httpx.MockTransport(engine),
            max_snapshot_bytes=settings.max_snapshot_bytes,
        )

    app = build_gateway(settings, client_factory=factory)
    with TestClient(app) as client:
        yield client, engine, app


def register(client, *, token=None, task=TASK, tenant=TENANT, max_calls=8):
    return client.post("/internal/v1/leases", headers=COMMON, json={
        "tenant_id": tenant, "task_id": task,
        "engine_grant": token or signed_grant(task=task, tenant=tenant),
        "max_calls": max_calls,
    })


def invoke(client, tool, args=None, *, task=TASK):
    return client.post(
        f"/internal/v1/tasks/{task}/tools/{tool}",
        headers=COMMON, json={"arguments": {} if args is None else args},
    )


def test_gateway_denies_public_calls_and_is_separate_from_agent_api(gateway, tmp_path):
    client, _, _ = gateway
    assert client.get("/health").status_code == 200
    assert client.get("/internal/v1/tasks/" + TASK).status_code == 401
    assert client.post("/internal/v1/leases", headers={
        "Authorization": "Bearer " + ADMIN_TOKEN, GATEWAY_HEADER: "1",
    }, json={}).status_code == 401
    assert client.post("/internal/v1/leases", headers={
        "Authorization": "Bearer " + CONTROL_TOKEN,
    }, json={}).status_code == 426
    assert client.get("/internal/v1/orders", headers=COMMON).status_code == 404
    with TestClient(build_app(Settings(
        token="x" * 40, state_dir=tmp_path / "agent",
    ))) as local_agent:
        assert local_agent.get("/internal/v1/tasks/" + TASK).status_code == 404
    assert register(client).status_code == 201


def test_research_flow_and_snapshot_handoff(gateway):
    client, engine, app = gateway
    r = register(client)
    assert r.status_code == 201, r.text
    assert "engine_grant" not in r.text
    assert CONTROL_TOKEN not in r.text
    assert set(r.json()["available_tools"]) == {
        "engine.datasets.discover", "engine.snapshots.create",
        "engine.snapshots.get", "engine.snapshots.fetch",
        "engine.backtests.submit", "engine.backtests.get",
        "engine.backtests.cancel",
    }
    assert invoke(client, "engine.datasets.discover").json()["result"]["datasets"][0]["id"] == DATASET
    snapshot = invoke(client, "engine.snapshots.create", {
        "dataset_id": DATASET, "start": "2026-01-01", "end": "2026-01-02",
    })
    assert snapshot.status_code == 200
    assert snapshot.json()["result"]["snapshot_id"] == "snap-1"
    assert client.get(
        f"/internal/v1/tasks/{TASK}/snapshots/snap-1/content",
        headers=COMMON,
    ).status_code == 404
    fetched = invoke(client, "engine.snapshots.fetch", {"snapshot_id": "snap-1"})
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["result"]["filename"] == "snap-1.parquet"
    binary = client.get(
        f"/internal/v1/tasks/{TASK}/snapshots/snap-1/content",
        headers=COMMON,
    )
    assert binary.status_code == 200
    assert binary.content == BYTES
    assert all("private" not in str(r.url) for r in engine.calls)
    assert not (app.state.gateway.root / "snapshots" / TASK).exists()


def test_backtest_dispatch_and_budget(gateway):
    client, engine, _ = gateway
    assert register(client, max_calls=4).status_code == 201
    args = {
        "strategy_ref": "account:desk",
        "dataset_refs": [DATASET],
        "start": "2026-01-01", "end": "2026-01-02",
        "starting_balance": "10000", "quote_currency": "USDT",
        "parameters": {}, "idempotency_key": "research-1",
    }
    assert invoke(client, "engine.backtests.submit", args).json()["result"][
        "backtest_id"
    ] == "bt-1"
    assert invoke(client, "engine.backtests.get", {"backtest_id": "bt-1"}).json()[
        "result"
    ]["result"]["mode"] == "simulation"
    assert invoke(client, "engine.backtests.cancel", {
        "backtest_id": "bt-1",
    }).status_code == 200
    assert invoke(client, "engine.datasets.discover").status_code == 200
    count = len(engine.calls)
    assert invoke(client, "engine.datasets.discover").status_code == 429
    assert len(engine.calls) == count
    assert CONTROL_TOKEN not in str(engine.calls)


def test_scopes_cross_task_and_revoke(gateway):
    client, engine, _ = gateway
    only_read = signed_grant(ops=["datasets:read"])
    assert register(client, token=only_read).status_code == 201
    assert invoke(client, "engine.snapshots.create", {
        "dataset_id": DATASET, "start": "2026-01-01", "end": "2026-01-02",
    }).status_code == 403
    assert invoke(client, "engine.orders.submit", {}).status_code == 403
    assert invoke(client, "engine.datasets.discover", task="other").status_code == 404
    assert register(client, token=only_read, tenant="wrong").status_code == 403
    assert register(client, token=only_read).status_code == 409
    assert client.delete(f"/internal/v1/tasks/{TASK}", headers=COMMON).status_code == 204
    assert client.delete(f"/internal/v1/tasks/{TASK}", headers=COMMON).status_code == 204
    count = len(engine.calls)
    assert invoke(client, "engine.datasets.discover").status_code == 404
    assert len(engine.calls) == count


def test_expired_bad_signatures_and_no_grant_leak(gateway):
    client, engine, _ = gateway
    old = signed_grant(now=int(time.time()) - 5000)
    assert register(client, token=old).status_code == 403
    mismatched = signed_grant(tenant="other")
    assert register(client, token=mismatched).status_code == 403
    forged = signed_grant()
    forged = forged[:-1] + ("a" if forged[-1] != "a" else "b")
    assert register(client, token=forged).status_code == 201
    # The gateway checks untrusted grant structure only; Engine checks HMAC.
    engine.invalid_grant = True
    r = invoke(client, "engine.datasets.discover")
    assert r.status_code == 403
    assert SECRET not in r.text and "bad secret" not in r.text


def test_invocation_invalid_arguments_and_result_size(gateway):
    client, engine, _ = gateway
    assert register(client, max_calls=3).status_code == 201
    assert invoke(client, "engine.snapshots.fetch", {
        "snapshot_id": "../etc/passwd",
    }).status_code == 422
    assert invoke(client, "engine.snapshots.fetch", {
        "snapshot_id": "snap-1", "workspace": "/etc",
    }).status_code == 422
    engine.big_response = True
    assert invoke(client, "engine.datasets.discover").status_code == 429
    assert invoke(client, "engine.datasets.discover").status_code == 429
    assert invoke(client, "engine.datasets.discover").status_code == 429


def test_request_size_and_unknown_fields(gateway):
    client, _, _ = gateway
    assert client.post("/internal/v1/leases", headers=COMMON, json={
        "tenant_id": TENANT, "task_id": TASK,
        "engine_grant": signed_grant(),
        "engine_url": "http://evil/engine",
    }).status_code == 422
    assert client.post("/internal/v1/leases", headers=COMMON, json={
        "tenant_id": TENANT, "task_id": TASK,
        "engine_grant": signed_grant(),
        "extra": "a" * 70_000,
    }).status_code == 413
    assert register(client, max_calls=100).status_code == 429


def test_lease_expiry_and_gateway_settings_fail_closed(gateway, monkeypatch, tmp_path):
    client, engine, app = gateway
    assert register(client).status_code == 201
    before = len(engine.calls)
    lease = app.state.gateway.leases[TASK]
    lease.expires_at = 1
    assert invoke(client, "engine.datasets.discover").status_code == 403
    assert len(engine.calls) == before
    with pytest.raises(ValueError):
        GatewaySettings(
            control_token="weak", engine_origin="http://research-engine",
            state_root=tmp_path,
        )
    with pytest.raises(ValueError):
        GatewaySettings(
            control_token=CONTROL_TOKEN, engine_origin="http://evil/research/v1",
            state_root=tmp_path,
        )
