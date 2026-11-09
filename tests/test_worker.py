import json
from types import SimpleNamespace

from seealpha_harness.store import TaskStore
from seealpha_harness.worker import execute


class FakeSession:
    def run(self, prompt, on_notification):
        for _ in range(20):
            on_notification(None)
        return SimpleNamespace(final_response=f"Analyzed: {prompt}", finish_reason="completed")


class FakeHarness:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def start_session(self, session_id):
        return FakeSession()

    def resume_session(self, session_id):
        return FakeSession()


def test_worker_persists_summary_and_sdk_activity(tmp_path):
    store = TaskStore(tmp_path)
    task = store.create("ETH mean reversion")
    assert execute(task["id"], store, runtime_factory=FakeHarness) == 0
    outcome = json.loads(store.outcome_path(task["id"]).read_text())
    assert outcome["ok"] is True
    assert outcome["final_response"] == "Analyzed: ETH mean reversion"
    assert store.list_artifacts(task["id"])[0]["id"] == "summary-1"
    assert len(store.events_after(task["id"], 0)) == 2


def test_worker_resume_uses_existing_session(tmp_path):
    store = TaskStore(tmp_path)
    task = store.create("First turn")
    store.change(task["id"], mode="resume", turn=2, prompt="Second turn")
    assert execute(task["id"], store, runtime_factory=FakeHarness) == 0
    assert store.artifact_path(task["id"], "summary-2").read_text() == "Analyzed: Second turn"
