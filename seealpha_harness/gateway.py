"""In-memory, fail-closed research gateway with independently scoped Engine clients.

Only a trusted Platform/controller may register grants or invoke tools. The
unrestricted Agent Shell must not hold the gateway control token or be able to
read this process's environment, filesystem, or HTTP traffic.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Callable

from .gateway_settings import GatewaySettings
from .research_client import EngineResearchClient, resource_id
from .research_tools import ResearchTools

_AUDIENCE = "seealpha-engine-research"
_TOOLS = {
    "engine.datasets.discover": "datasets:read",
    "engine.snapshots.create": "snapshots:create",
    "engine.snapshots.get": "snapshots:read",
    "engine.snapshots.fetch": "snapshots:read",
    "engine.backtests.submit": "backtests:submit",
    "engine.backtests.get": "backtests:read",
    "engine.backtests.cancel": "backtests:cancel",
}
_SCOPE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_MAX_TOOL_RESULT = 1024 * 1024


class GatewayDenied(Exception):
    pass


class GatewayNotFound(Exception):
    pass


class GatewayConflict(Exception):
    pass


class GatewayBudgetExceeded(Exception):
    pass


def _identifier(value: str) -> str:
    if not isinstance(value, str) or not _SCOPE_ID.fullmatch(value) or ".." in value:
        raise ValueError("Invalid task or tenant identifier")
    return value


def inspect_grant(token: str, tenant_id: str, task_id: str, *, now: int | None = None) -> dict:
    """Parse ONLY for routing consistency; the ENGINE verifies the HMAC.

    A caller with the gateway control credential is trusted to supply the
    token. Parsed claims alone are never proof of authorization.
    """
    now = int(time.time()) if now is None else now
    _identifier(tenant_id)
    _identifier(task_id)
    try:
        if not isinstance(token, str) or len(token) > 4096:
            raise ValueError
        payload, signature = token.split(".")
        if not payload or len(signature) < 30:
            raise ValueError
        raw = base64.b64decode(
            payload + "=" * (-len(payload) % 4), altchars=b"-_", validate=True,
        )
        if len(raw) > 3072:
            raise ValueError
        claims = json.loads(raw)
        if (
            not isinstance(claims, dict)
            or set(claims) != {
                "version", "audience", "tenant_id", "task_id",
                "ops", "datasets", "strategies", "not_before", "expires_at",
            }
            or type(claims["version"]) is not int or claims["version"] != 1
            or claims["audience"] != _AUDIENCE
            or claims["tenant_id"] != tenant_id or claims["task_id"] != task_id
        ):
            raise ValueError
        left, right = claims["not_before"], claims["expires_at"]
        if (
            type(left) is not int or type(right) is not int
            or left > now or right <= now or not 0 < right - left <= 3600
        ):
            raise ValueError
        for key in ("ops", "datasets", "strategies"):
            values = claims[key]
            if (
                not isinstance(values, list) or len(values) > 100
                or not all(isinstance(value, str) for value in values)
                or len(set(values)) != len(values)
            ):
                raise ValueError
            if key == "ops":
                if any(value not in _TOOLS.values() for value in values):
                    raise ValueError
            elif any(not _SCOPE_ID.fullmatch(v) or ".." in v for v in values):
                raise ValueError
        return claims
    except (ValueError, TypeError, UnicodeDecodeError, binascii.Error):
        raise GatewayDenied("Research grant does not match authorized task") from None


@dataclass
class Lease:
    tenant_id: str
    task_id: str
    expires_at: int
    permitted: frozenset[str]
    remaining: int
    client: EngineResearchClient = field(repr=False)
    tools: ResearchTools = field(repr=False)
    workspace: Path = field(repr=False)
    downloads: set[str] = field(default_factory=set, repr=False)
    revoked: bool = False
    lock: RLock = field(default_factory=RLock, repr=False)

    def public(self) -> dict:
        return {
            "tenant_id": self.tenant_id,
            "task_id": self.task_id,
            "expires_at": self.expires_at,
            "available_tools": sorted(self.permitted),
            "remaining_calls": self.remaining,
        }


class GatewayService:
    """One trusted gateway process. Registrations are volatile by design."""

    def __init__(
        self,
        settings: GatewaySettings,
        client_factory: Callable[[str], EngineResearchClient] | None = None,
    ):
        self.settings = settings
        self.root = Path(settings.state_root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.is_symlink():
            raise ValueError("Gateway state root may not be a symlink")
        self.lock = RLock()
        self.leases: dict[str, Lease] = {}
        self.client_factory = client_factory or (lambda token: EngineResearchClient(
            settings.engine_origin, token,
            max_snapshot_bytes=settings.max_snapshot_bytes,
        ))

    def _workspace(self, tenant_id: str, task_id: str) -> Path:
        identifier = hashlib.sha256(
            (tenant_id + "\x00" + task_id).encode("utf-8"),
        ).hexdigest()
        directory = self.root / "snapshots" / identifier
        if directory.is_symlink():
            raise GatewayDenied("Research workspace not available")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        return directory

    def register(
        self, *, tenant_id: str, task_id: str,
        engine_grant: str, max_calls: int,
    ) -> dict:
        claims = inspect_grant(engine_grant, tenant_id, task_id)
        if not 1 <= max_calls <= self.settings.max_calls_per_task:
            raise GatewayBudgetExceeded("Requested research budget exceeds gateway policy")
        permitted = frozenset(
            name for name, op in _TOOLS.items() if op in claims["ops"]
        )
        if not permitted:
            raise GatewayDenied("Research grant contains no permitted tools")
        with self.lock:
            if task_id in self.leases:
                raise GatewayConflict("Research task already registered")
            workspace = self._workspace(tenant_id, task_id)
            client = self.client_factory(engine_grant)
            try:
                lease = Lease(
                    tenant_id=tenant_id, task_id=task_id,
                    expires_at=claims["expires_at"], permitted=permitted,
                    remaining=max_calls, client=client,
                    tools=ResearchTools(client, workspace), workspace=workspace,
                )
                self.leases[task_id] = lease
                return lease.public()
            except Exception:
                client.close()
                raise

    def _find(self, task_id: str) -> Lease:
        try:
            _identifier(task_id)
        except ValueError:
            raise GatewayNotFound("Research task not registered") from None
        with self.lock:
            lease = self.leases.get(task_id)
        if lease is None:
            raise GatewayNotFound("Research task not registered")
        return lease

    @staticmethod
    def _active(lease: Lease) -> None:
        if lease.revoked or time.time() >= lease.expires_at:
            raise GatewayDenied("Research task authorization expired or revoked")

    def status(self, task_id: str) -> dict:
        lease = self._find(task_id)
        with lease.lock:
            self._active(lease)
            return lease.public()

    def invoke(self, task_id: str, name: str, arguments: dict) -> dict:
        lease = self._find(task_id)
        with lease.lock:
            self._active(lease)
            if name not in lease.permitted:
                raise GatewayDenied("Research tool not granted")
            if lease.remaining <= 0:
                raise GatewayBudgetExceeded("Research tool budget exhausted")
            lease.remaining -= 1
            result = lease.tools.invoke(name, arguments)
            try:
                payload = json.dumps(result, ensure_ascii=False, allow_nan=False)
            except (TypeError, ValueError) as exc:
                raise GatewayDenied("Research tool result is not safely serializable") from exc
            if len(payload.encode("utf-8")) > _MAX_TOOL_RESULT:
                raise GatewayBudgetExceeded("Research tool result exceeds response limit")
            if name == "engine.snapshots.fetch":
                snapshot_id = resource_id(arguments["snapshot_id"])
                lease.downloads.add(snapshot_id)
            return result

    def downloaded_file(self, task_id: str, snapshot_id: str) -> Path:
        lease = self._find(task_id)
        with lease.lock:
            self._active(lease)
            try:
                snapshot_id = resource_id(snapshot_id)
            except ValueError:
                raise GatewayNotFound("Snapshot not staged for this task") from None
            if snapshot_id not in lease.downloads:
                raise GatewayNotFound("Snapshot not staged for this task")
            path = lease.workspace / (snapshot_id + ".parquet")
            if path.is_symlink() or not path.is_file():
                raise GatewayNotFound("Snapshot no longer available")
            if path.stat().st_size > self.settings.max_snapshot_bytes:
                raise GatewayDenied("Snapshot exceeds gateway policy")
            return path

    def revoke(self, task_id: str) -> None:
        with self.lock:
            lease = self.leases.pop(task_id, None)
        if lease is None:
            return
        with lease.lock:
            lease.revoked = True
            lease.client.close()
            lease.downloads.clear()

    def shutdown(self) -> None:
        with self.lock:
            tasks = list(self.leases)
        for task_id in tasks:
            self.revoke(task_id)
