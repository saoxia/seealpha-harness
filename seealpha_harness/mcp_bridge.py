"""One task's privileged MCP bridge. NEVER run in the Agent Shell container.

The bridge knows only a task-scoped Gateway credential, never the Engine grant
or Gateway administrative token. Its MCP endpoint is intentionally credential-
free for the one task private network; enforcing that topology is a deployer's
responsibility, and is NOT verified by a container unit test.
"""
from __future__ import annotations

import hmac
import json
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Mount, Route

from .research_client import resource_id

MAX_JSON = 1_100_000
MAX_SNAPSHOT = 128 * 1024 * 1024
GW_HEADER = "X-SeeAlpha-Gateway-Contract-Version"


def _origin(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("A fixed service origin is required")
    uri = urlsplit(value)
    if (
        uri.scheme not in {"http", "https"} or not uri.hostname
        or uri.username or uri.password or uri.path not in {"", "/"}
        or uri.query or uri.fragment
    ):
        raise ValueError("Expected a fixed HTTP(S) service origin")
    return value.rstrip("/")


@dataclass(frozen=True)
class BridgeSettings:
    task_id: str
    gateway_origin: str
    public_origin: str
    task_token: str

    def __post_init__(self):
        resource_id(self.task_id)
        _origin(self.gateway_origin)
        _origin(self.public_origin)
        if not isinstance(self.task_token, str) or len(self.task_token) < 32:
            raise ValueError("Bridge needs a strong task-only Gateway token")
        if hmac.compare_digest(self.task_token, os.environ.get("GATEWAY_CONTROL_TOKEN", "")):
            raise ValueError("Bridge must not use the Gateway administrative token")

    @classmethod
    def from_env(cls):
        return cls(
            task_id=os.environ.get("BRIDGE_TASK_ID", ""),
            gateway_origin=os.environ.get("BRIDGE_GATEWAY_ORIGIN", ""),
            public_origin=os.environ.get("BRIDGE_PUBLIC_ORIGIN", ""),
            task_token=os.environ.get("BRIDGE_TASK_TOKEN", ""),
        )


class BridgeError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class BridgeClient:
    """Task-pinned transport: no arbitrary HTTP methods, hosts or task IDs."""

    def __init__(self, settings: BridgeSettings, *, transport=None):
        self.settings = settings
        self.client = httpx.Client(
            base_url=settings.gateway_origin,
            headers={
                "Authorization": f"Bearer {settings.task_token}",
                GW_HEADER: "1",
            },
            timeout=httpx.Timeout(30),
            trust_env=False,
            follow_redirects=False,
            transport=transport,
        )

    def close(self):
        self.client.close()

    @staticmethod
    def _check_response(response: httpx.Response):
        if response.status_code in {401, 403, 404}:
            raise BridgeError("research_not_authorized")
        if response.status_code in {409, 429}:
            raise BridgeError("research_conflict_or_budget_exhausted")
        if response.status_code >= 300:
            raise BridgeError("research_gateway_unavailable")
        if response.headers.get(GW_HEADER) != "1":
            raise BridgeError("research_gateway_contract_mismatch")

    def invoke(self, tool_name: str, arguments: dict) -> dict:
        allowed = {
            "engine.datasets.discover",
            "engine.snapshots.create",
            "engine.snapshots.get",
            "engine.snapshots.fetch",
            "engine.backtests.submit",
            "engine.backtests.get",
            "engine.backtests.cancel",
        }
        if tool_name not in allowed or not isinstance(arguments, dict):
            raise BridgeError("research_tool_not_allowed")
        try:
            request_bytes = json.dumps(arguments, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise BridgeError("research_tool_arguments_invalid") from exc
        if len(request_bytes) > 60_000:
            raise BridgeError("research_tool_arguments_too_large")
        path = f"/bridge/v1/tasks/{self.settings.task_id}/tools/{tool_name}"
        try:
            with self.client.stream(
                "POST", path, json={"arguments": arguments},
            ) as response:
                self._check_response(response)
                buf = bytearray()
                for block in response.iter_bytes():
                    if len(buf) + len(block) > MAX_JSON:
                        raise BridgeError("research_result_too_large")
                    buf.extend(block)
        except httpx.RequestError:
            raise BridgeError("research_gateway_unavailable") from None
        try:
            data = json.loads(buf)
        except (ValueError, UnicodeDecodeError):
            raise BridgeError("research_gateway_invalid_response") from None
        if not isinstance(data, dict) or not isinstance(data.get("result"), dict):
            raise BridgeError("research_gateway_invalid_response")
        return data["result"]

    def stream_snapshot(self, snapshot_id: str):
        try:
            snapshot_id = resource_id(snapshot_id)
        except ValueError:
            raise BridgeError("snapshot_not_found") from None
        path = (
            f"/bridge/v1/tasks/{self.settings.task_id}/snapshots/"
            f"{snapshot_id}/content"
        )
        try:
            with self.client.stream("GET", path) as response:
                self._check_response(response)
                total = 0
                for block in response.iter_bytes():
                    total += len(block)
                    if total > MAX_SNAPSHOT:
                        raise BridgeError("snapshot_exceeds_bridge_limit")
                    yield block
        except httpx.RequestError:
            raise BridgeError("research_gateway_unavailable") from None


def build_bridge(
    settings: BridgeSettings | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
):
    settings = settings or BridgeSettings.from_env()
    client = BridgeClient(settings, transport=transport)
    server = MCPServer("SeeAlpha Research", version="1")

    async def call(name: str, **arguments):
        try:
            return await run_in_threadpool(client.invoke, name, arguments)
        except BridgeError as exc:
            # No credential or internal Gateway/Engine URL is ever returned.
            return {"error": exc.code}

    @server.tool()
    async def discover_datasets() -> dict:
        """List authorized market-data datasets and their coverage."""
        return await call("engine.datasets.discover")

    @server.tool()
    async def create_snapshot(dataset_id: str, start: str, end: str) -> dict:
        """Request an immutable, bounded Parquet market-data snapshot."""
        return await call(
            "engine.snapshots.create", dataset_id=dataset_id, start=start, end=end,
        )

    @server.tool()
    async def get_snapshot(snapshot_id: str) -> dict:
        """Check snapshot readiness, size, SHA-256 and data provenance."""
        return await call("engine.snapshots.get", snapshot_id=snapshot_id)

    @server.tool()
    async def fetch_snapshot(snapshot_id: str) -> dict:
        """Stage snapshot and return read-only URL for Python/DuckDB research."""
        try:
            result = await call("engine.snapshots.fetch", snapshot_id=snapshot_id)
            if "error" in result:
                return result
            snapshot_id = resource_id(snapshot_id)
            if result.get("snapshot_id") != snapshot_id:
                return {"error": "research_gateway_invalid_response"}
            # No secret in this URL; only one task-isolated network may reach it.
            return {
                **result,
                "download_url": (
                    f"{settings.public_origin}/snapshots/{snapshot_id}/content"
                ),
            }
        except ValueError:
            return {"error": "snapshot_not_found"}

    @server.tool()
    async def submit_backtest(
        strategy_ref: str, dataset_refs: list[str],
        start: str, end: str, starting_balance: str,
        quote_currency: str, idempotency_key: str,
        parameters: dict[str, Any] | None = None,
    ) -> dict:
        """Submit a development-only simulation backtest to Engine."""
        return await call(
            "engine.backtests.submit",
            strategy_ref=strategy_ref, dataset_refs=dataset_refs,
            start=start, end=end, starting_balance=starting_balance,
            quote_currency=quote_currency,
            idempotency_key=idempotency_key, parameters=parameters or {},
        )

    @server.tool()
    async def get_backtest(backtest_id: str) -> dict:
        """Read Engine backtest status and recorded simulation result."""
        return await call("engine.backtests.get", backtest_id=backtest_id)

    @server.tool()
    async def cancel_backtest(backtest_id: str) -> dict:
        """Request cancellation of an authorized development backtest."""
        return await call("engine.backtests.cancel", backtest_id=backtest_id)

    async def health(_request: Request):
        return JSONResponse({"service": "seealpha-task-mcp-bridge", "contract_version": 1})

    async def snapshot(request: Request):
        try:
            snapshot_id = resource_id(request.path_params["snapshot_id"])
        except ValueError:
            raise HTTPException(404, "Snapshot not found") from None

        # Establish Gateway authorization and status BEFORE sending 200 OK.
        # Response streaming still fails closed on size or transport errors.
        upstream_client = httpx.AsyncClient(
            base_url=settings.gateway_origin,
            headers={
                "Authorization": f"Bearer {settings.task_token}",
                GW_HEADER: "1",
            },
            timeout=httpx.Timeout(30),
            trust_env=False, follow_redirects=False,
            transport=transport,
        )
        path = (
            f"/bridge/v1/tasks/{settings.task_id}/snapshots/"
            f"{snapshot_id}/content"
        )
        try:
            upstream = await upstream_client.send(
                upstream_client.build_request("GET", path), stream=True,
            )
            client._check_response(upstream)
        except (httpx.RequestError, BridgeError):
            await upstream_client.aclose()
            raise HTTPException(404, "Snapshot unavailable") from None

        async def blocks():
            count = 0
            try:
                async for chunk in upstream.aiter_bytes():
                    count += len(chunk)
                    if count > MAX_SNAPSHOT:
                        raise BridgeError("snapshot_exceeds_bridge_limit")
                    yield chunk
            finally:
                await upstream.aclose()
                await upstream_client.aclose()

        return StreamingResponse(
            blocks(), media_type="application/octet-stream",
            headers={
                "Cache-Control": "private, no-store",
                "Content-Disposition": f'attachment; filename="{snapshot_id}.parquet"',
            },
        )

    @asynccontextmanager
    async def lifespan(_app):
        async with server.session_manager.run():
            try:
                yield
            finally:
                client.close()

    addr = urlsplit(settings.public_origin)
    default_port = 443 if addr.scheme == "https" else 80
    allow_host = f"{addr.hostname}:{addr.port or default_port}"
    transport_security = TransportSecuritySettings(
        allowed_hosts=[allow_host],
        allowed_origins=[],
    )
    mcp_app = server.streamable_http_app(
        stateless_http=True,
        json_response=True,
        host=addr.hostname,
        transport_security=transport_security,
        max_request_body_size=64 * 1024,
    )

    app = Starlette(
        routes=[
            Route("/health", health),
            Route("/snapshots/{snapshot_id}/content", snapshot),
            Mount("/", app=mcp_app),
        ],
        lifespan=lifespan,
    )
    app.state.mcp_server = server
    app.state.bridge_client = client
    return app


app = build_bridge()
