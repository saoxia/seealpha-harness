"""Trusted research gateway settings.

This is a SEPARATE entrypoint and security principal from the existing
unrestricted local SDK worker. Never install it inside Agent Shell.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class GatewaySettings:
    control_token: str
    engine_origin: str
    state_root: Path
    max_snapshot_bytes: int = 128 * 1024 * 1024
    max_calls_per_task: int = 500

    def __post_init__(self) -> None:
        if (
            not isinstance(self.control_token, str)
            or len(self.control_token) < 32
            or self.control_token.startswith("replace-")
        ):
            raise ValueError("GATEWAY_CONTROL_TOKEN requires at least 32 secret characters")
        if self.control_token == os.environ.get("HARNESS_API_TOKEN", ""):
            raise ValueError("Gateway and Harness control credentials must be distinct")
        uri = urlsplit(self.engine_origin)
        if (
            uri.scheme not in {"http", "https"} or not uri.hostname
            or uri.username or uri.password or uri.query or uri.fragment
            or uri.path not in {"", "/"}
        ):
            raise ValueError("ENGINE_RESEARCH_URL must be a fixed service origin")
        if not 1024 <= self.max_snapshot_bytes <= 512 * 1024 * 1024:
            raise ValueError("Invalid snapshot size ceiling")
        if not 1 <= self.max_calls_per_task <= 10_000:
            raise ValueError("Invalid per-task call limit")

    @classmethod
    def from_env(cls) -> "GatewaySettings":
        return cls(
            control_token=os.environ.get("GATEWAY_CONTROL_TOKEN", ""),
            engine_origin=os.environ.get("ENGINE_RESEARCH_URL", ""),
            state_root=Path(os.environ.get("GATEWAY_STATE_DIR", "/gateway-state")).resolve(),
            max_snapshot_bytes=int(os.environ.get(
                "GATEWAY_MAX_SNAPSHOT_BYTES", str(128 * 1024 * 1024),
            )),
            max_calls_per_task=int(os.environ.get("GATEWAY_MAX_CALLS_PER_TASK", "500")),
        )
