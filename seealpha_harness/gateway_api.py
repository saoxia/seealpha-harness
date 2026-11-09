"""Dedicated trusted HTTP entrypoint for research tools (not mounted in api.py).

Deploy with credentials and state separate from the DeepSeek SDK Shell.
Only trusted Platform / isolated controller callers may use this API.
"""
from __future__ import annotations

import hmac
import json
from contextlib import asynccontextmanager
from typing import Any, Callable

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.concurrency import run_in_threadpool

from .gateway import (
    GatewayBudgetExceeded,
    GatewayConflict,
    GatewayDenied,
    GatewayNotFound,
    GatewayService,
)
from .gateway_settings import GatewaySettings
from .research_client import (
    EngineResearchClient,
    ResearchConflict,
    ResearchDenied,
    ResearchError,
    ResearchIncompatible,
    ResearchUnavailable,
)

GATEWAY_CONTRACT = "1"
GATEWAY_HEADER = "X-SeeAlpha-Gateway-Contract-Version"
_MAX_REQUEST_BYTES = 64 * 1024


class LeaseBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    tenant_id: str = Field(min_length=1, max_length=128)
    task_id: str = Field(min_length=1, max_length=128)
    engine_grant: str = Field(min_length=40, max_length=4096)
    max_calls: int = Field(default=100, ge=1, le=10_000)


class ToolBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    arguments: dict[str, Any]


async def _decode(request: Request, schema: type[BaseModel]) -> BaseModel:
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > _MAX_REQUEST_BYTES:
            raise HTTPException(413, "Research gateway request exceeds 64 KiB")
        data.extend(chunk)
    try:
        object_data = json.loads(data)
        return schema.model_validate(object_data)
    except (UnicodeDecodeError, ValueError, ValidationError):
        raise HTTPException(422, "Invalid research gateway request") from None


def build_gateway(
    settings: GatewaySettings | None = None,
    *,
    client_factory: Callable[[str], EngineResearchClient] | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.gateway = GatewayService(settings or GatewaySettings.from_env(), client_factory)
        try:
            yield
        finally:
            app.state.gateway.shutdown()

    app = FastAPI(
        title="SeeAlpha Trusted Research Gateway",
        lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None,
    )

    @app.middleware("http")
    async def protocol_header(request: Request, call_next):
        response = await call_next(request)
        response.headers[GATEWAY_HEADER] = GATEWAY_CONTRACT
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/health")
    def health():
        return {"service": "seealpha-research-gateway", "contract_version": 1}

    def trusted(
        request: Request,
        authorization: str = Header(default=""),
        contract: str = Header(default="", alias=GATEWAY_HEADER),
    ) -> GatewayService:
        gateway = request.app.state.gateway
        kind, _, token = authorization.partition(" ")
        if kind.lower() != "bearer" or not hmac.compare_digest(
            token, gateway.settings.control_token,
        ):
            raise HTTPException(401, "Research gateway authorization required")
        if contract != GATEWAY_CONTRACT:
            raise HTTPException(426, "Research gateway contract version mismatch")
        return gateway

    router = APIRouter(prefix="/internal/v1", dependencies=[Depends(trusted)])

    def service(request: Request) -> GatewayService:
        return request.app.state.gateway

    @router.post("/leases", status_code=201)
    async def register_lease(request: Request):
        body = await _decode(request, LeaseBody)
        assert isinstance(body, LeaseBody)
        gateway = service(request)
        try:
            return gateway.register(**body.model_dump())
        except GatewayDenied as exc:
            raise HTTPException(403, str(exc)) from None
        except GatewayConflict as exc:
            raise HTTPException(409, str(exc)) from None
        except GatewayBudgetExceeded as exc:
            raise HTTPException(429, str(exc)) from None
        except ValueError:
            raise HTTPException(422, "Invalid research lease scope") from None

    @router.get("/tasks/{task_id}")
    def task_status(task_id: str, request: Request):
        gateway = service(request)
        try:
            return gateway.status(task_id)
        except GatewayNotFound:
            raise HTTPException(404, "Research task not registered") from None
        except GatewayDenied:
            raise HTTPException(403, "Research task grant expired") from None

    @router.delete("/tasks/{task_id}", status_code=204)
    def revoke_task(task_id: str, request: Request):
        # Authenticated revocation is idempotent. In-flight requests may finish.
        service(request).revoke(task_id)
        return None

    @router.post("/tasks/{task_id}/tools/{tool_name}")
    async def invoke_tool(task_id: str, tool_name: str, request: Request):
        body = await _decode(request, ToolBody)
        assert isinstance(body, ToolBody)
        gateway = service(request)
        try:
            # Engine I/O and snapshot streaming are synchronous and must not
            # block the gateway's ASGI event loop.
            result = await run_in_threadpool(
                gateway.invoke, task_id, tool_name, body.arguments,
            )
            return {"result": result}
        except GatewayNotFound:
            raise HTTPException(404, "Research task not registered") from None
        except (GatewayDenied, ResearchDenied):
            raise HTTPException(403, "Research operation not authorized") from None
        except GatewayBudgetExceeded:
            raise HTTPException(429, "Research budget exhausted") from None
        except (GatewayConflict, ResearchConflict, FileExistsError):
            raise HTTPException(409, "Research operation conflicts with current state") from None
        except (ValueError, TypeError):
            raise HTTPException(422, "Invalid research tool arguments") from None
        except ResearchUnavailable:
            raise HTTPException(503, "Engine research service unavailable") from None
        except (ResearchIncompatible, ResearchError):
            raise HTTPException(502, "Engine research contract or response invalid") from None

    @router.get("/tasks/{task_id}/snapshots/{snapshot_id}/content")
    def staged_snapshot(task_id: str, snapshot_id: str, request: Request):
        gateway = service(request)
        try:
            path = gateway.downloaded_file(task_id, snapshot_id)
        except GatewayNotFound:
            raise HTTPException(404, "Research snapshot not staged") from None
        except GatewayDenied:
            raise HTTPException(403, "Research snapshot access denied") from None
        return FileResponse(
            path, media_type="application/octet-stream",
            filename=path.name, headers={"Cache-Control": "private, no-store"},
        )

    app.include_router(router)
    return app


app = build_gateway()
