from fastapi.testclient import TestClient

from seealpha_harness.api import CONTRACT_HEADER, build_app
from seealpha_harness.settings import Settings


HEADERS = {
    "Authorization": "Bearer " + "x" * 40,
    CONTRACT_HEADER: "1",
}


def test_auth_version_tasks_and_stream(tmp_path):
    app = build_app(Settings(token="x" * 40, state_dir=tmp_path))
    with TestClient(app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["runtime_layout"] == "resident-container"
        assert health.json()["task_execution"] == "subprocess"
        assert client.get("/v1/tasks/missing").status_code == 401
        no_version = {k: v for k, v in HEADERS.items() if k != CONTRACT_HEADER}
        assert client.post("/v1/tasks", headers=no_version, json={"prompt": "x"}).status_code == 426
        assert client.post("/v1/tasks", headers=HEADERS, json={"prompt": ""}).status_code == 422

        # A stub launcher tests the controller contract with no model calls.
        manager = app.state.manager
        manager._launch = lambda task: manager.store.change(task["id"], status="completed")
        response = client.post("/v1/tasks", headers=HEADERS, json={"prompt": "Research ETH"})
        assert response.status_code == 202
        task_id = response.json()["id"]
        assert client.get(f"/v1/tasks/{task_id}", headers=HEADERS).json()["status"] == "completed"
        assert client.get(f"/v1/tasks/{task_id}/artifacts", headers=HEADERS).json() == {"artifacts": []}
        assert client.get(f"/v1/tasks/{task_id}/artifacts/../something", headers=HEADERS).status_code == 404
        assert client.get(f"/v1/tasks/{task_id}/events", headers=HEADERS).status_code == 200

        resumed = client.post(
            f"/v1/tasks/{task_id}/continue", headers=HEADERS, json={"prompt": "More"}
        )
        assert resumed.status_code == 202
        assert resumed.json()["turn"] == 2


def test_bad_id_and_extra_input(tmp_path):
    app = build_app(Settings(token="x" * 40, state_dir=tmp_path))
    with TestClient(app) as client:
        assert client.get("/v1/tasks/not-a-uuid", headers=HEADERS).status_code == 404
        assert client.post("/v1/tasks", headers=HEADERS, json={
            "prompt": "a", "command": "rm -rf /",
        }).status_code == 422


def test_history_and_timeline_are_authenticated_bounded_and_prompt_free(tmp_path):
    app = build_app(Settings(token="x" * 40, state_dir=tmp_path))
    with TestClient(app) as client:
        manager = app.state.manager
        manager._launch = lambda task: manager.store.change(task["id"], status="completed")
        first = client.post(
            "/v1/tasks", headers=HEADERS, json={"prompt": "sensitive research prompt"},
        ).json()
        second = client.post(
            "/v1/tasks", headers=HEADERS, json={"prompt": "different private strategy"},
        ).json()
        tasks_url = "/v1/tasks?limit=1"
        assert client.get(tasks_url).status_code == 401
        latest = client.get(tasks_url, headers=HEADERS)
        assert latest.status_code == 200
        listed = latest.json()["tasks"]
        assert len(listed) == 1 and listed[0]["id"] == second["id"]
        assert "prompt" not in listed[0] and "final_response" not in listed[0]
        assert "private" not in latest.text
        assert client.get("/v1/tasks?limit=101", headers=HEADERS).status_code == 422

        timeline = client.get(
            f"/v1/tasks/{first['id']}/timeline?after=0", headers=HEADERS,
        )
        assert timeline.status_code == 200
        events = timeline.json()["events"]
        assert events and events[0]["type"] == "progress"
        assert timeline.json()["next_cursor"] == events[-1]["sequence"]
        after = client.get(
            f"/v1/tasks/{first['id']}/timeline?after={events[-1]['sequence']}",
            headers=HEADERS,
        )
        assert after.json()["events"] == []
        assert client.get(f"/v1/tasks/{first['id']}/timeline").status_code == 401
        assert client.get("/v1/tasks/invalid/timeline", headers=HEADERS).status_code == 404



def test_placeholder_model_key_is_not_reported_as_ready(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "replace-with-dev-only-credential")
    app = build_app(Settings(token="x" * 40, state_dir=tmp_path))
    with TestClient(app) as client:
        status = client.get("/health")
        assert status.status_code == 200
        assert status.json()["runtime_configured"] is False
