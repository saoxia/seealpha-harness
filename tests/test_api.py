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
        assert client.get("/health").status_code == 200
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
