"""No live Docker or model calls: journal/recovery uses a deterministic runner."""
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from seealpha_harness.runtime_recovery import DurableTaskWorkloads, RuntimeJournal
from seealpha_harness.task_runtime import DockerCommands, WorkloadSpec


class MemoryDocker:
    def __init__(self):
        self.calls = []
        self.resources = {}
        self.fail_removal = False

    def __call__(self, *args):
        self.calls.append(args)
        if args[:2] in {("container", "inspect"), ("network", "inspect")}:
            name = args[-1]
            if name not in self.resources:
                raise subprocess.CalledProcessError(
                    1, args, stderr="Error: No such object: " + name,
                )
            return SimpleNamespace(stdout=json.dumps(self.resources[name]))
        if args[0] == "rm" or args[:2] == ("network", "rm"):
            if self.fail_removal:
                raise subprocess.CalledProcessError(1, args, stderr="daemon unavailable")
            self.resources.pop(args[-1], None)
        return SimpleNamespace(stdout="")


@pytest.fixture
def spec(tmp_path):
    state = tmp_path / "agent"
    state.mkdir(mode=0o700)
    private = tmp_path / "secrets"
    private.mkdir(mode=0o700)
    secret = private / "token"
    secret.write_text("a" * 48)
    secret.chmod(0o600)
    return WorkloadSpec(
        tenant_id="tenant-1", task_id="research-1",
        agent_image="agent@sha256:" + "a" * 64,
        bridge_image="bridge@sha256:" + "b" * 64,
        gateway_network="trusted-gateway",
        gateway_origin="http://gateway:8780",
        agent_state=state, bridge_secret_file=secret,
    )


def add_owned_resources(docker, spec):
    network, agent, bridge = spec.names()
    for name, role in (
        (network, "task-network"), (agent, "agent"), (bridge, "bridge"),
    ):
        docker.resources[name] = {
            "seealpha.runtime": "research",
            "seealpha.owner": network,
            "seealpha.role": role,
        }


def test_durable_launch_records_before_docker_and_never_persists_secrets(spec, tmp_path):
    docker = MemoryDocker()
    journal = RuntimeJournal(tmp_path / "runtime")
    revoked = []
    manager = DurableTaskWorkloads(
        DockerCommands(docker), journal=journal, revoke_lease=revoked.append,
    )
    result = manager.launch(spec)
    assert result["state"] == "launched"
    assert journal.read(spec.tenant_id, spec.task_id)["phase"] == "running"
    record = journal._path(spec.tenant_id, spec.task_id)
    raw = record.read_text()
    assert spec.bridge_secret_file.read_text() not in raw
    assert str(spec.bridge_secret_file) not in raw
    assert str(spec.agent_state) not in raw
    assert record.stat().st_mode & 0o077 == 0
    network, agent, bridge = spec.names()
    creates = [call for call in docker.calls if call[0] == "create"]
    assert len(creates) == 2
    for call, role in ((creates[0], "bridge"), (creates[1], "agent")):
        assert "seealpha.owner=" + network in call
        assert "seealpha.role=" + role in call
    assert "seealpha.role=task-network" in docker.calls[0]
    with pytest.raises(FileExistsError):
        manager.launch(spec)


def test_controller_restart_reconciles_only_owned_resources(spec, tmp_path):
    docker = MemoryDocker()
    add_owned_resources(docker, spec)
    journal = RuntimeJournal(tmp_path / "runtime")
    journal.begin(spec)
    journal.mark(spec.tenant_id, spec.task_id, "running")
    # A new controller instance sees the previous running task.
    revoked = []
    recovered = DurableTaskWorkloads(
        DockerCommands(docker), journal=RuntimeJournal(tmp_path / "runtime"),
        revoke_lease=revoked.append,
    )
    assert recovered.reconcile_pending() == [spec.task_id]
    assert revoked == [spec.task_id]
    assert docker.resources == {}
    assert journal.read(spec.tenant_id, spec.task_id)["phase"] == "cleaned"
    assert recovered.reconcile_pending() == []
    recovered.stop(spec.tenant_id, spec.task_id)  # idempotent
    assert revoked == [spec.task_id]


def test_recovery_refuses_foreign_containers_and_preserves_pending(spec, tmp_path):
    docker = MemoryDocker()
    add_owned_resources(docker, spec)
    network, agent, bridge = spec.names()
    docker.resources[agent]["seealpha.owner"] = "someone-else"
    journal = RuntimeJournal(tmp_path / "runtime")
    journal.begin(spec)
    revoked = []
    manager = DurableTaskWorkloads(
        DockerCommands(docker), journal=journal, revoke_lease=revoked.append,
    )
    with pytest.raises(RuntimeError, match="recovery incomplete"):
        manager.reconcile_pending()
    assert agent in docker.resources
    assert ("rm", "-f", agent) not in docker.calls
    assert journal.read(spec.tenant_id, spec.task_id)["phase"] == "cleanup_pending"
    docker.resources[agent]["seealpha.owner"] = network
    assert manager.reconcile_pending() == [spec.task_id]
    assert docker.resources == {}


def test_failed_revocation_does_not_mark_recovery_complete(spec, tmp_path):
    docker = MemoryDocker()
    add_owned_resources(docker, spec)
    journal = RuntimeJournal(tmp_path / "runtime")
    journal.begin(spec)
    calls = []

    def revoke(task_id):
        calls.append(task_id)
        if len(calls) == 1:
            raise RuntimeError("gateway not reachable")

    manager = DurableTaskWorkloads(
        DockerCommands(docker), journal=journal, revoke_lease=revoke,
    )
    with pytest.raises(RuntimeError, match="recovery incomplete"):
        manager.reconcile_pending()
    assert journal.read(spec.tenant_id, spec.task_id)["phase"] == "cleanup_pending"
    assert docker.resources == {}
    assert manager.reconcile_pending() == [spec.task_id]
    assert calls == [spec.task_id, spec.task_id]


def test_docker_daemon_error_is_not_mistaken_for_absent_resource(spec, tmp_path):
    docker = MemoryDocker()
    add_owned_resources(docker, spec)
    docker.fail_removal = True
    journal = RuntimeJournal(tmp_path / "runtime")
    journal.begin(spec)
    manager = DurableTaskWorkloads(
        DockerCommands(docker), journal=journal, revoke_lease=lambda _: None,
    )
    with pytest.raises(RuntimeError, match="recovery incomplete"):
        manager.stop(spec.tenant_id, spec.task_id)
    assert journal.read(spec.tenant_id, spec.task_id)["phase"] == "cleanup_pending"
    docker.fail_removal = False
    manager.stop(spec.tenant_id, spec.task_id)
    assert journal.read(spec.tenant_id, spec.task_id)["phase"] == "cleaned"


def test_journal_rejects_tampering_and_wrong_permissions(spec, tmp_path):
    journal = RuntimeJournal(tmp_path / "runtime")
    journal.begin(spec)
    path = journal._path(spec.tenant_id, spec.task_id)
    path.chmod(0o644)
    with pytest.raises(ValueError, match="permissions"):
        journal.read(spec.tenant_id, spec.task_id)
    path.chmod(0o600)
    obj = json.loads(path.read_text())
    obj["task_id"] = "other-task"
    path.write_text(json.dumps(obj))
    with pytest.raises(ValueError, match="identifier mismatch"):
        journal.pending()
