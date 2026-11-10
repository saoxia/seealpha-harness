"""HTTP v1 contract for a single-user local development Harness."""

import asyncio
from contextlib import asynccontextmanager
import hmac
import json
import os
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from .manager import BusyError, ConflictError, QueueFullError, TaskManager
from .artifacts import ArtifactImportError, ArtifactIntegrityError
from .settings import Settings
from .store import TERMINAL


CONTRACT_VERSION = "1"
CONTRACT_HEADER = "X-SeeAlpha-Harness-Contract-Version"


class PromptBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    prompt: str = Field(min_length=1, max_length=20_000)
    queue_if_busy: bool = False


class ImportArtifactBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    source_file: str = Field(min_length=1, max_length=140)


def build_app(settings: Settings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.manager = TaskManager(settings or Settings.from_env())
        try:
            yield
        finally:
            app.state.manager.shutdown()

    app = FastAPI(title="SeeAlpha Harness (local development)", lifespan=lifespan)

    @app.middleware("http")
    async def contract_header(request: Request, call_next):
        response = await call_next(request)
        response.headers[CONTRACT_HEADER] = CONTRACT_VERSION
        return response

    def protected(request: Request) -> TaskManager:
        manager = request.app.state.manager
        auth = request.headers.get("authorization", "")
        kind, _, token = auth.partition(" ")
        if kind.lower() != "bearer" or not hmac.compare_digest(token, manager.settings.token):
            raise HTTPException(401, "Unauthorized")
        if request.headers.get(CONTRACT_HEADER) != CONTRACT_VERSION:
            raise HTTPException(426, "Harness contract version 1 required")
        return manager

    def find_task(manager: TaskManager, task_id: str) -> dict:
        try:
            return manager.store.get(task_id)
        except KeyError as exc:
            raise HTTPException(404, "Task not found") from exc

    @app.get("/health")
    def health():
        return {
            "service": "seealpha-harness", "contract_version": 1,
            "runtime_configured": bool(os.environ.get("DEEPSEEK_API_KEY")),
            "mode": "single-user-development",
            "runtime_layout": "resident-container",
            "task_execution": "subprocess",
        }

    @app.post("/v1/tasks", status_code=202)
    def create_task(body: PromptBody, manager: TaskManager = Depends(protected)):
        try:
            return manager.create(body.prompt, queue_if_busy=body.queue_if_busy)
        except BusyError as exc:
            raise HTTPException(409, str(exc)) from exc
        except QueueFullError as exc:
            raise HTTPException(429, str(exc)) from exc

    @app.get("/v1/queue")
    def queue_status(manager: TaskManager = Depends(protected)):
        return manager.queue_state()

    class RecoveryBody(BaseModel):
        model_config = ConfigDict(extra="forbid")
        confirm_stale_workers_stopped: Literal[True]

    @app.post("/v1/queue/resume")
    def resume_queue(body: RecoveryBody, manager: TaskManager = Depends(protected)):
        try:
            return manager.resume_after_recovery(
                confirm_stale_workers_stopped=body.confirm_stale_workers_stopped,
            )
        except (BusyError, ConflictError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/v1/tasks/{task_id}")
    def get_task(task_id: str, manager: TaskManager = Depends(protected)):
        return find_task(manager, task_id)

    @app.post("/v1/tasks/{task_id}/continue", status_code=202)
    def continue_task(task_id: str, body: PromptBody, manager: TaskManager = Depends(protected)):
        find_task(manager, task_id)
        try:
            return manager.continue_task(
                task_id, body.prompt, queue_if_busy=body.queue_if_busy,
            )
        except (BusyError, ConflictError) as exc:
            raise HTTPException(409, str(exc)) from exc
        except QueueFullError as exc:
            raise HTTPException(429, str(exc)) from exc

    @app.post("/v1/tasks/{task_id}/cancel", status_code=202)
    def cancel_task(task_id: str, manager: TaskManager = Depends(protected)):
        find_task(manager, task_id)
        return manager.cancel(task_id)

    @app.get("/v1/tasks/{task_id}/events")
    async def task_events(
        task_id: str, request: Request, after: int = Query(default=0, ge=0),
        manager: TaskManager = Depends(protected),
    ):
        find_task(manager, task_id)
        last = request.headers.get("last-event-id")
        if last:
            if not last.isdecimal():
                raise HTTPException(400, "Invalid event cursor")
            after = max(after, int(last))

        async def generate():
            cursor = after
            while True:
                new_events = manager.store.events_after(task_id, cursor)
                for event in new_events:
                    cursor = event["sequence"]
                    yield (f"id: {cursor}\nevent: {event['type']}\n"
                           f"data: {json.dumps(event, ensure_ascii=False)}\n\n")
                if manager.store.get(task_id)["status"] in TERMINAL and not new_events:
                    yield "event: end\ndata: {}\n\n"
                    break
                if await request.is_disconnected():
                    break
                await asyncio.sleep(0.5)

        return StreamingResponse(
            generate(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/v1/tasks/{task_id}/artifacts")
    def list_artifacts(task_id: str, manager: TaskManager = Depends(protected)):
        find_task(manager, task_id)
        try:
            return {"artifacts": manager.store.list_artifacts(task_id)}
        except ArtifactIntegrityError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/v1/tasks/{task_id}/artifacts/import", status_code=201)
    def import_artifact(
        task_id: str, body: ImportArtifactBody,
        manager: TaskManager = Depends(protected),
    ):
        find_task(manager, task_id)
        with manager.lock:
            try:
                return manager.store.import_artifact(task_id, body.source_file)
            except ArtifactImportError as exc:
                raise HTTPException(422, str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc

    @app.get("/v1/tasks/{task_id}/artifacts/{artifact_id}")
    def read_artifact(
        task_id: str, artifact_id: str, manager: TaskManager = Depends(protected),
    ):
        find_task(manager, task_id)
        try:
            meta, raw = manager.store.read_artifact(task_id, artifact_id)
        except KeyError as exc:
            raise HTTPException(404, "Artifact not found") from exc
        except ArtifactIntegrityError as exc:
            raise HTTPException(409, str(exc)) from exc
        # Always an attachment. Generated Python/JSON/PNG must never be
        # rendered as trusted same-origin active content.
        filename = meta["filename"]
        return Response(
            content=raw,
            media_type=("text/markdown" if meta["kind"] == "markdown"
                        else "application/octet-stream"),
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "no-store",
            },
        )

    return app


app = build_app()
