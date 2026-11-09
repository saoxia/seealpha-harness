"""Task Docker lifecycle is tested as a command plan, never using Docker."""
from pathlib import Path

import pytest

from seealpha_harness.mcp_bridge import BridgeSettings
from seealpha_harness.task_runtime import DockerCommands, TaskWorkloads, WorkloadSpec

DIGEST_A = "seealpha-agent@sha256:" + "a" * 64
DIGEST_B = "seealpha-bridge@sha256:" + "b" * 64
TOKEN = "task-token-not-gateway-or-engine-key-xxxxxxxxxxxx"


class FakeDocker:
    def __init__(self, fail_at=None):
        self.commands = []
        self.fail_at = fail_at

    def __call__(self, *args):
        self.commands.append(args)
        if self.fail_at == len(self.commands):
            raise RuntimeError("simulated Docker failure")
        return None


@pytest.fixture
def spec(tmp_path):
    agent = tmp_path / "agent-task"
    agent.mkdir(mode=0o700)
    private = tmp_path / "privileged-secrets"
    private.mkdir(mode=0o700)
    token = private / "bridge-token"
    token.write_text(TOKEN)
    token.chmod(0o600)
    return WorkloadSpec(
        tenant_id="tenant-4", task_id="research-001",
        agent_image=DIGEST_A, bridge_image=DIGEST_B,
        gateway_network="trusted-gateway-only",
        gateway_origin="http://research-gateway:8780",
        agent_state=agent, bridge_secret_file=token,
    )


def test_task_runtime_docker_plan_never_exposes_secret_or_gateway_to_agent(spec):
    fake = FakeDocker()
    revoked = []
    workload = TaskWorkloads(DockerCommands(fake), revoke_lease=revoked.append)
    result = workload.launch(spec)
    commands = fake.commands
    assert result["task_id"] == "research-001"
    assert len(commands) == 6
    assert commands[0][:3] == ("network", "create", "--internal")
    assert not any("--privileged" in x or "docker.sock" in x for cmd in commands for x in cmd)
    created = [cmd for cmd in commands if cmd[0] == "create"]
    assert len(created) == 2
    bridge, agent = created
    assert bridge[1:3] == ("--name", spec.names()[2])
    assert agent[1:3] == ("--name", spec.names()[1])
    assert "--cap-drop" in agent and "--read-only" in agent
    assert "--cap-drop" in bridge and "--read-only" in bridge
    assert any(x == "BRIDGE_TASK_TOKEN_FILE=/run/bridge-token" for x in bridge)
    assert any(x.startswith("HARNESS_RESEARCH_MCP_ORIGIN=") for x in agent)
    assert not any("BRIDGE_TASK_TOKEN" in x for x in agent)
    assert not any("GATEWAY_CONTROL_TOKEN" in x for cmd in commands for x in cmd)
    assert not any(TOKEN in x for cmd in commands for x in cmd)
    assert not any(str(spec.bridge_secret_file) in x for x in agent)
    assert not any(str(spec.agent_state) in x for x in bridge)
    assert ("network", "connect", spec.gateway_network, spec.names()[2]) in commands
    assert all(spec.gateway_network not in x for x in agent)
    assert revoked == []


def test_rollback_releases_containers_network_and_revokes_lease(spec):
    fake = FakeDocker(fail_at=4)  # after the task network and Bridge exist
    revoked = []
    workload = TaskWorkloads(DockerCommands(fake), revoke_lease=revoked.append)
    with pytest.raises(RuntimeError, match="simulated Docker"):
        workload.launch(spec)
    assert revoked == [spec.task_id]
    assert ("rm", "-f", spec.names()[2]) in fake.commands
    assert ("network", "rm", spec.names()[0]) in fake.commands
    assert ("start", spec.names()[1]) not in fake.commands


def test_stop_is_idempotent_if_secrets_have_been_cleaned(spec):
    fake = FakeDocker()
    revoked = []
    workload = TaskWorkloads(DockerCommands(fake), revoke_lease=revoked.append)
    spec.bridge_secret_file.unlink()
    workload.stop(spec)
    workload.stop(spec)
    assert revoked == [spec.task_id, spec.task_id]
    assert fake.commands.count(("network", "rm", spec.names()[0])) == 2
    assert fake.commands.count(("rm", "-f", spec.names()[1])) == 2


def test_rejects_unpinned_images_and_unsafe_mounts(spec, tmp_path):
    from dataclasses import replace
    with pytest.raises(ValueError, match="digest-pinned"):
        replace(spec, agent_image="seealpha-agent:latest").validate()
    with pytest.raises(ValueError, match="trusted Gateway network"):
        replace(spec, gateway_network="bad/network").validate()
    with pytest.raises(ValueError, match="Bridge secret"):
        replace(spec, agent_state=spec.bridge_secret_file.parent).validate()
    spec.bridge_secret_file.chmod(0o644)
    with pytest.raises(ValueError, match="readable by others"):
        spec.validate()


def test_bridge_reads_secret_file_instead_of_inspectable_env(spec, monkeypatch):
    monkeypatch.setenv("BRIDGE_TASK_TOKEN_FILE", str(spec.bridge_secret_file))
    monkeypatch.delenv("BRIDGE_TASK_TOKEN", raising=False)
    monkeypatch.setenv("BRIDGE_TASK_ID", spec.task_id)
    monkeypatch.setenv("BRIDGE_GATEWAY_ORIGIN", spec.gateway_origin)
    monkeypatch.setenv("BRIDGE_PUBLIC_ORIGIN", "http://research-sidecar:8782")
    cfg = BridgeSettings.from_env()
    assert cfg.task_token == TOKEN
    assert TOKEN not in repr(cfg.task_id)
    monkeypatch.setenv("BRIDGE_TASK_TOKEN", TOKEN)
    with pytest.raises(ValueError):
        BridgeSettings.from_env()
    monkeypatch.delenv("BRIDGE_TASK_TOKEN", raising=False)
    spec.bridge_secret_file.chmod(0o644)
    with pytest.raises(ValueError, match="private permissions"):
        BridgeSettings.from_env()
