"""Opt-in, one-task Docker workload orchestrator. No implicit Docker actions.

This trusted controller is a different security principal from the Agent.
Only its caller can register Gateway leases and materialize the Bridge token.
CI exercises ALL commands through a fake runner (no real Docker).
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_IMAGE = re.compile(r"^[a-z0-9][a-z0-9._/-]*@sha256:[a-f0-9]{64}$")
_NET = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}$")
_BRIDGE_USER = "10003:10003"
_AGENT_USER = "10001:10001"


def _identity(value: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value) or ".." in value:
        raise ValueError("Invalid tenant or task identity")
    return value


def _origin(value: str) -> str:
    parts = urlsplit(value)
    if (parts.scheme not in {"http", "https"} or not parts.hostname
            or parts.username or parts.password or parts.path not in {"", "/"}
            or parts.query or parts.fragment):
        raise ValueError("Gateway must be a fixed private service origin")
    return value.rstrip("/")


@dataclass(frozen=True)
class WorkloadSpec:
    tenant_id: str
    task_id: str
    agent_image: str
    bridge_image: str
    gateway_network: str
    gateway_origin: str
    agent_state: Path
    bridge_secret_file: Path

    def validate(self) -> None:
        _identity(self.tenant_id)
        _identity(self.task_id)
        if (
            not _IMAGE.fullmatch(self.agent_image)
            or not _IMAGE.fullmatch(self.bridge_image)
            or self.agent_image == self.bridge_image
        ):
            raise ValueError("Task images must be separate, digest-pinned images")
        if not _NET.fullmatch(self.gateway_network):
            raise ValueError("Invalid trusted Gateway network")
        _origin(self.gateway_origin)
        # Agent and Bridge receive NO common file mounts.
        agent = Path(self.agent_state)
        secret = Path(self.bridge_secret_file)
        if (agent.is_symlink() or not agent.is_dir()
                or secret.is_symlink() or not secret.is_file()):
            raise ValueError("Task state and Bridge secret must already exist")
        agent = agent.resolve(strict=True)
        secret = secret.resolve(strict=True)
        if agent == secret.parent or agent in secret.parents:
            raise ValueError("Agent workdir may not contain Bridge secret")
        if secret.stat().st_mode & 0o077:
            raise ValueError("Bridge secret must not be readable by others")
        if secret.stat().st_size < 32 or secret.stat().st_size > 256:
            raise ValueError("Bridge secret size invalid")
        if (agent.stat().st_mode & 0o022):
            raise ValueError("Agent state must not be group/other writable")

    def names(self) -> tuple[str, str, str]:
        return resource_names(self.tenant_id, self.task_id)


def resource_names(tenant_id: str, task_id: str) -> tuple[str, str, str]:
    """Derive stable, non-secret resource names from validated identities."""
    _identity(tenant_id)
    _identity(task_id)
    digest = hashlib.sha256(
        (tenant_id + "\x00" + task_id).encode("utf-8"),
    ).hexdigest()[:22]
    return "sah-" + digest, "sah-agent-" + digest, "sah-bridge-" + digest


class DockerCommands:
    """Narrow, audit-friendly docker CLI adapter. Nothing uses shell=True."""

    def __init__(self, runner: Callable[..., object] | None = None):
        self.runner = runner or self._run

    @staticmethod
    def _run(*args):
        return subprocess.run(
            ["docker", *args], check=True,
            capture_output=True, text=True, timeout=30,
        )

    def run(self, *args):
        return self.runner(*args)


class TaskWorkloads:
    """Provision a task-private *internal* Agent/Bridge network with rollback.

    This class is not wired into TaskManager or Compose. It is called only by
    trusted, explicitly authorized control-plane code, never from the model.
    """

    def __init__(self, docker: DockerCommands, *, revoke_lease: Callable[[str], None]):
        self.docker = docker
        self.revoke_lease = revoke_lease

    def launch(self, spec: WorkloadSpec) -> dict:
        spec.validate()
        network, agent, bridge = spec.names()
        private_url = "http://research-sidecar:8782"
        made_network = False
        made_agent = False
        made_bridge = False
        try:
            # --internal removes ordinary Internet egress. A separate trusted
            # model proxy will be needed for real LLM calls in this topology.
            self.docker.run(
                "network", "create", "--internal", "--driver", "bridge",
                "--label", "seealpha.runtime=research",
                "--label", "seealpha.owner=" + network,
                "--label", "seealpha.role=task-network",
                network,
            )
            made_network = True
            self.docker.run(
                "create", "--name", bridge,
                "--label", "seealpha.runtime=research",
                "--label", "seealpha.owner=" + network,
                "--label", "seealpha.role=bridge",
                "--network", network, "--network-alias", "research-sidecar",
                "--user", _BRIDGE_USER, "--read-only",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
                "--pids-limit", "96", "--memory", "512m", "--cpus", "0.5",
                "--tmpfs", "/tmp:rw,nosuid,nodev,size=128m",
                "--mount", (
                    f"type=bind,src={Path(spec.bridge_secret_file).resolve()},"
                    "dst=/run/bridge-token,readonly"
                ),
                "--env", "BRIDGE_TASK_TOKEN_FILE=/run/bridge-token",
                "--env", "BRIDGE_TASK_ID=" + spec.task_id,
                "--env", "BRIDGE_GATEWAY_ORIGIN=" + _origin(spec.gateway_origin),
                "--env", "BRIDGE_PUBLIC_ORIGIN=" + private_url,
                "--pull", "never", spec.bridge_image,
            )
            made_bridge = True
            # Only Bridge is dual-homed; Agent is NOT connected to Gateway.
            self.docker.run(
                "network", "connect", spec.gateway_network, bridge,
            )
            self.docker.run(
                "create", "--name", agent,
                "--label", "seealpha.runtime=research",
                "--label", "seealpha.owner=" + network,
                "--label", "seealpha.role=agent",
                "--network", network,
                "--user", _AGENT_USER, "--read-only",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
                "--pids-limit", "256", "--memory", "4g", "--cpus", "2.0",
                "--tmpfs", "/tmp:rw,nosuid,nodev,size=512m",
                "--mount", (
                    f"type=bind,src={Path(spec.agent_state).resolve()},"
                    "dst=/state"
                ),
                "--env", "HARNESS_STATE_DIR=/state",
                "--env", "HARNESS_RESEARCH_MCP_ORIGIN=" + private_url,
                "--env", "HARNESS_RESEARCH_MCP_TASK_ID=" + spec.task_id,
                "--env", "HOME=/state/homes/" + spec.task_id,
                "--pull", "never", spec.agent_image,
                "python", "-m", "seealpha_harness.worker",
                "--task-id", spec.task_id,
            )
            made_agent = True
            self.docker.run("start", bridge)
            self.docker.run("start", agent)
            return {"task_id": spec.task_id, "agent": agent, "bridge": bridge,
                    "network": network, "state": "launched"}
        except Exception:
            self._remove(agent if made_agent else None,
                         bridge if made_bridge else None,
                         network if made_network else None)
            try:
                self.revoke_lease(spec.task_id)
            except Exception:
                # Original provisioning failure remains authoritative;
                # an orphan reconciler must also retry remote revocation.
                pass
            raise

    def stop(self, spec: WorkloadSpec) -> None:
        """Best-effort cleanup; revoke before disconnecting containers."""
        # Cleanup must work even if a secret file or state directory has
        # already been removed, but identifiers must still be safe.
        _identity(spec.tenant_id)
        _identity(spec.task_id)
        network, agent, bridge = spec.names()
        try:
            self.revoke_lease(spec.task_id)
        finally:
            self._remove(agent, bridge, network)

    def _remove(self, agent: str | None, bridge: str | None, network: str | None):
        for container in (agent, bridge):
            if container is not None:
                try:
                    self.docker.run("rm", "-f", container)
                except Exception:
                    # A later reconciler must retry removing any orphan.
                    pass
        if network is not None:
            try:
                self.docker.run("network", "rm", network)
            except Exception:
                pass
