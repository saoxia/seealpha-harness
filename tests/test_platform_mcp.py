"""The resident SDK uses Platform MCP, without Engine tokens in the profile."""
from uuid import uuid4

from seealpha_harness.mcp_profile import build_platform_profile_patch
from seealpha_harness.store import TaskStore
from seealpha_harness.worker import execute


class FakeSession:
    def run(self, prompt, on_notification):
        from types import SimpleNamespace
        assert "mcp__seealpha_research__" in prompt
        assert "submit_python_candidate_backtest" in prompt
        return SimpleNamespace(final_response="Simulation queued", finish_reason="completed")


class FakeSDK:
    received = None

    def __init__(self, **kwargs):
        self.__class__.received = kwargs

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def start_session(self, _task_id):
        return FakeSession()


def test_task_scoped_sdk_mcp_patch_has_no_static_credential(tmp_path, monkeypatch):
    store = TaskStore(tmp_path)
    task_id = str(uuid4())
    store.create("Research ETH", task_id=task_id)
    token = "secret-capability-" + "a" * 48
    monkeypatch.setenv("SEEALPHA_PLATFORM_RESEARCH_ORIGIN", "https://platform.internal")
    monkeypatch.setenv("SEEALPHA_PLATFORM_RESEARCH_TASK_ID", task_id)
    monkeypatch.setenv("SEEALPHA_PLATFORM_RESEARCH_TOKEN", token)
    assert execute(task_id, store, runtime_factory=FakeSDK) == 0
    patch_path = store.home(task_id) / "patches" / "platform-research-mcp.patch.yml"
    patch = patch_path.read_text()
    assert FakeSDK.received["patches"] == (str(patch_path),)
    assert "https://platform.internal/api/research/tasks/" + task_id + "/mcp" in patch
    assert "!!js" in patch
    assert "process.env.SEEALPHA_PLATFORM_RESEARCH_TOKEN" in patch
    assert token not in patch
    assert patch_path.stat().st_mode & 0o077 == 0
    assert token not in store.outcome_path(task_id).read_text()


def test_wrong_task_identity_fails_closed_without_sdk_launch(tmp_path, monkeypatch):
    store = TaskStore(tmp_path)
    task = store.create("Check status")
    monkeypatch.setenv("SEEALPHA_PLATFORM_RESEARCH_ORIGIN", "https://platform.internal")
    monkeypatch.setenv("SEEALPHA_PLATFORM_RESEARCH_TASK_ID", str(uuid4()))
    monkeypatch.setenv("SEEALPHA_PLATFORM_RESEARCH_TOKEN", "capability-" + "a" * 48)
    FakeSDK.received = None
    assert execute(task["id"], store, runtime_factory=FakeSDK) == 1
    assert FakeSDK.received is None
    assert "capability-" not in store.outcome_path(task["id"]).read_text()


def test_mcp_profile_rejects_url_injection(tmp_path):
    task = str(uuid4())
    for origin in ("file:///etc/passwd", "https://host/other", "https://a@b"):
        try:
            build_platform_profile_patch(origin, task, tmp_path)
        except ValueError:
            pass
        else:
            raise AssertionError("Invalid MCP origin accepted")
