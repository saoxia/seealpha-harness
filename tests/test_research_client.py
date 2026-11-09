"""Mock-producer contract tests; no Engine server, model key, or Docker needed."""

from hashlib import sha256
import json

import httpx
import pytest

from seealpha_harness.research_client import (
    BacktestSpec,
    EngineResearchClient,
    ENGINE_HEADER,
    ENGINE_CONTRACT,
    RESEARCH_HEADER,
    RESEARCH_CONTRACT,
    ResearchConflict,
    ResearchDenied,
    ResearchIncompatible,
    ResearchUnavailable,
)
from seealpha_harness.research_tools import ResearchTools, TOOL_NAMES


HEADERS = {ENGINE_HEADER: ENGINE_CONTRACT, RESEARCH_HEADER: RESEARCH_CONTRACT}
PARQUET = b"PAR1sample-mock-research-dataPAR1"


class FakeEngineResearch:
    def __init__(self):
        self.calls = []
        self.snapshot_bytes = PARQUET
        self.force_status = None
        self.force_headers = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        assert request.headers[ENGINE_HEADER] == ENGINE_CONTRACT
        assert request.headers[RESEARCH_HEADER] == RESEARCH_CONTRACT
        assert request.headers["authorization"] == "Bearer task-scoped-test-token"
        assert request.url.host == "mock-engine"
        assert request.url.path.startswith("/research/v1/")
        h = HEADERS if self.force_headers is None else self.force_headers
        if self.force_status is not None:
            return httpx.Response(
                self.force_status, headers=h,
                json={"detail": "private token should not leak"},
            )
        path = request.url.path
        if path == "/research/v1/datasets":
            return httpx.Response(
                200, headers=h, json={"datasets": [
                    {"id": "eth-1m", "symbol": "ETHUSDT", "kind": "bar",
                     "start": "2026-01-01", "end": "2026-10-01"},
                ]},
            )
        if path == "/research/v1/snapshots" and request.method == "POST":
            body = json.loads(request.content)
            assert body == {"dataset_id": "eth-1m", "start": "2026-01-01",
                            "end": "2026-10-01", "format": "parquet"}
            return httpx.Response(
                202, headers=h, json={"snapshot_id": "snap-1", "status": "queued"},
            )
        if path == "/research/v1/snapshots/snap-1":
            return httpx.Response(
                200, headers=h, json={
                    "snapshot_id": "snap-1", "format": "parquet", "status": "ready",
                    "size_bytes": len(self.snapshot_bytes),
                    "sha256": sha256(self.snapshot_bytes).hexdigest(),
                },
            )
        if path == "/research/v1/snapshots/snap-1/content":
            return httpx.Response(200, headers=h, content=self.snapshot_bytes)
        if path == "/research/v1/backtests" and request.method == "POST":
            body = json.loads(request.content)
            assert body["purpose"] == "development"
            assert body["execution"] == "simulation"
            assert body["idempotency_key"] == "research-run-001"
            return httpx.Response(
                202, headers=h, json={"backtest_id": "bt-1", "status": "queued"},
            )
        if path == "/research/v1/backtests/bt-1" and request.method == "GET":
            return httpx.Response(
                200, headers=h, json={
                    "backtest_id": "bt-1", "status": "completed",
                    "result": {"net_pnl": "-12.50", "currency": "USDT"},
                },
            )
        if path == "/research/v1/backtests/bt-1/cancel":
            return httpx.Response(
                202, headers=h,
                json={"backtest_id": "bt-1", "status": "cancelling"},
            )
        return httpx.Response(404, headers=h)


def open_client(fake, **kwargs):
    return EngineResearchClient(
        "http://mock-engine:8766", "task-scoped-test-token",
        transport=httpx.MockTransport(fake), **kwargs,
    )


def spec():
    return BacktestSpec(
        strategy_ref="sha256:abc",
        dataset_refs=("eth-1m",),
        start="2026-01-01",
        end="2026-10-01",
        starting_balance="10000.00",
        quote_currency="USDT",
        parameters={"lookback": 20},
    )


def test_mock_end_to_end_metadata_snapshot_backtest(tmp_path):
    fake = FakeEngineResearch()
    with open_client(fake) as client:
        datasets = client.discover_datasets()
        assert datasets[0]["id"] == "eth-1m"
        created = client.create_snapshot("eth-1m", "2026-01-01", "2026-10-01")
        assert created["status"] == "queued"
        dest = client.fetch_snapshot("snap-1", tmp_path)
        assert dest.read_bytes() == PARQUET
        assert dest.name == "snap-1.parquet"
        assert client.submit_backtest(spec(), "research-run-001")["status"] == "queued"
        assert client.get_backtest("bt-1")["result"]["net_pnl"] == "-12.50"
        assert client.cancel_backtest("bt-1")["status"] == "cancelling"
        with pytest.raises(FileExistsError):
            client.fetch_snapshot("snap-1", tmp_path)
    assert len(fake.calls) == 8


def test_tool_dispatcher_only_accepts_fixed_research_operations(tmp_path):
    fake = FakeEngineResearch()
    with open_client(fake) as client:
        tools = ResearchTools(client, tmp_path)
        assert len(TOOL_NAMES) == 7
        assert tools.invoke("engine.datasets.discover", {})["datasets"][0]["id"] == "eth-1m"
        assert tools.invoke(
            "engine.snapshots.create",
            {"dataset_id": "eth-1m", "start": "2026-01-01", "end": "2026-10-01"},
        )["snapshot_id"] == "snap-1"
        assert tools.invoke("engine.snapshots.fetch", {"snapshot_id": "snap-1"})[
            "filename"
        ] == "snap-1.parquet"
        assert tools.invoke("engine.backtests.submit", {
            "strategy_ref": "sha256:abc", "dataset_refs": ["eth-1m"],
            "start": "2026-01-01", "end": "2026-10-01",
            "starting_balance": "10000", "quote_currency": "USDT",
            "idempotency_key": "research-run-001",
        })["backtest_id"] == "bt-1"
        assert tools.invoke("engine.backtests.get", {"backtest_id": "bt-1"})[
            "status"
        ] == "completed"
        assert tools.invoke("engine.backtests.cancel", {"backtest_id": "bt-1"})[
            "status"
        ] == "cancelling"
        with pytest.raises(ValueError):
            tools.invoke("engine.orders.submit", {"action": "buy"})
        with pytest.raises(ValueError):
            tools.invoke("engine.snapshots.fetch", {
                "snapshot_id": "snap-1", "workspace": "/etc",
            })
        with pytest.raises(ValueError):
            tools.invoke("engine.snapshots.fetch", {"snapshot_id": "../../etc/passwd"})


@pytest.mark.parametrize("status,expected", [
    (401, ResearchDenied), (403, ResearchDenied),
    (409, ResearchConflict), (426, ResearchIncompatible),
    (429, ResearchUnavailable), (503, ResearchUnavailable),
])
def test_rejected_response_sanitized(status, expected):
    fake = FakeEngineResearch()
    fake.force_status = status
    with open_client(fake) as client:
        with pytest.raises(expected, match="^(?!.*private token)"):
            client.discover_datasets()


def test_contract_header_required_in_success():
    fake = FakeEngineResearch()
    fake.force_headers = {ENGINE_HEADER: ENGINE_CONTRACT}
    with open_client(fake) as client:
        with pytest.raises(ResearchIncompatible):
            client.discover_datasets()


def test_response_limit():
    def oversized(_request):
        return httpx.Response(
            200, headers=HEADERS, content=b"x" * 2000,
        )
    with open_client(oversized, max_json_bytes=1024) as client:
        with pytest.raises(ResearchIncompatible, match="size limit"):
            client.discover_datasets()


def test_deny_malformed_urls_and_input():
    with pytest.raises(ValueError):
        EngineResearchClient("http://mock-engine/private?token=bad", "token")
    with pytest.raises(ValueError):
        EngineResearchClient("http://mock-engine", "")
    with pytest.raises(ValueError):
        BacktestSpec(
            strategy_ref="a/../../orders",
            dataset_refs=("eth-1m",),
            start="2026-01-01", end="2026-10-01",
            starting_balance="1", quote_currency="USDT",
        ).to_payload()
    with pytest.raises(ValueError):
        BacktestSpec(
            strategy_ref="strategy-1", dataset_refs=("eth-1m",),
            start="2026-10-01", end="2026-01-01",
            starting_balance="1", quote_currency="USDT",
        ).to_payload()
    with pytest.raises(ValueError):
        BacktestSpec(
            strategy_ref="strategy-1", dataset_refs=("eth-1m",),
            start="2026-01-01", end="2026-10-01",
            starting_balance="NaN", quote_currency="USDT",
        ).to_payload()
    with pytest.raises(ValueError):
        BacktestSpec(
            strategy_ref="strategy-1", dataset_refs=("eth-1m",),
            start="2026-01-01", end="2026-10-01",
            starting_balance="1", quote_currency="USDT",
            parameters={"nonfinite": float("nan")},
        ).to_payload()


def test_snapshot_checksum_and_size_fails_closed(tmp_path):
    fake = FakeEngineResearch()
    # Change bytes in transit without changing expected hash.
    def corrupt(request):
        response = fake(request)
        if request.url.path.endswith("/content"):
            return httpx.Response(200, headers=HEADERS, content=b"corrupt")
        return response
    with open_client(corrupt) as client:
        with pytest.raises(ResearchIncompatible):
            client.fetch_snapshot("snap-1", tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_snapshot_max_bytes_and_symlink(tmp_path):
    fake = FakeEngineResearch()
    with open_client(fake, max_snapshot_bytes=1) as client:
        with pytest.raises(ResearchIncompatible):
            client.fetch_snapshot("snap-1", tmp_path)
    target = tmp_path / "actual"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with open_client(fake) as client:
        with pytest.raises(ValueError):
            client.fetch_snapshot("snap-1", link)
    assert not list(target.iterdir())


def test_transport_failure_redacts_endpoint_and_credentials():
    def offline(_request):
        raise httpx.ConnectError("token=hidden-and-secret")
    with open_client(offline) as client:
        with pytest.raises(ResearchUnavailable) as err:
            client.discover_datasets()
        assert "hidden-and-secret" not in str(err.value)
