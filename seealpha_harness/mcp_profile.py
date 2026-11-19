"""Generate a non-secret DeepSeek Harness MCP profile patch for one Agent task.

Official DeepSeek Harness sdk-minimal supports MCP via the dsh-mcp-client
Cordis row in a per-launch patches=() configuration. The patch contains ONLY
the private task-bridge URL, never Gateway/Engine credentials.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

_PUBLIC_ORIGIN = re.compile(r"https?://[A-Za-z0-9_.-]+(?::[0-9]{2,5})?\Z")


def build_research_profile_patch(origin: str, directory: Path) -> Path:
    if not isinstance(origin, str) or not _PUBLIC_ORIGIN.fullmatch(origin):
        raise ValueError("Research MCP origin must be a fixed private HTTP(S) origin")
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / "research-mcp.patch.yml"
    # JSON-quoted strings are valid YAML. Prevent YAML injection.
    endpoint = json.dumps(f"{origin}/mcp")
    payload = (
        "- insert:\n"
        "    - id: mcp-seealpha-research\n"
        "      name: '@deepseek-ai/dsh-mcp-client'\n"
        "      config:\n"
        "        serverName: seealpha_research\n"
        "        transport: streamable-http\n"
        f"        url: {endpoint}\n"
        "        failOnStartupError: true\n"
        "        toolCallTimeoutMs: 60000\n"
    )
    fd, staged = tempfile.mkstemp(prefix=".mcp-patch-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(staged, path)
    finally:
        if os.path.exists(staged):
            os.unlink(staged)
    return path


def build_platform_profile_patch(origin: str, task_id: str, directory: Path) -> Path:
    """Generate the task MCP connection without materializing its bearer token.

    The DeepSeek MCP plugin resolves the already task-scoped Platform capability
    from the Worker environment. The Engine grant never enters this process.
    """
    from uuid import UUID

    if not isinstance(origin, str) or not _PUBLIC_ORIGIN.fullmatch(origin):
        raise ValueError("Platform MCP requires a fixed private HTTP(S) origin")
    try:
        if str(UUID(task_id, version=4)) != task_id:
            raise ValueError
    except (TypeError, ValueError, AttributeError):
        raise ValueError("Invalid Platform research task identity") from None
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / "platform-research-mcp.patch.yml"
    endpoint = json.dumps(f"{origin}/api/research/tasks/{task_id}/mcp")
    # !!js is the officially supported SDK environment-backed header syntax;
    # no bearer value is copied into this file, model prompt, or task outcome.
    payload = (
        "- insert:\n"
        "    - id: mcp-seealpha-platform-research\n"
        "      name: '@deepseek-ai/dsh-mcp-client'\n"
        "      config:\n"
        "        serverName: seealpha_research\n"
        "        transport: streamable-http\n"
        f"        url: {endpoint}\n"
        "        headers:\n"
        "          Authorization: !!js '`Bearer ${process.env.SEEALPHA_PLATFORM_RESEARCH_TOKEN}`'\n"
        "        failOnStartupError: true\n"
        "        toolCallTimeoutMs: 60000\n"
    )
    fd, staged = tempfile.mkstemp(prefix=".mcp-patch-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(staged, path)
    finally:
        if os.path.exists(staged):
            os.unlink(staged)
    return path
