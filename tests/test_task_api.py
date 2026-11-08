import os
import time
from unittest.mock import patch

from fastapi.testclient import TestClient

from pocket_agent.api import create_app
from tests.test_executor import INITIAL, TASK, TestActor
from tests.test_runtime_v2 import RuntimeCase


class TaskAPITests(RuntimeCase):
    def test_auth_submit_and_durable_result(self):
        app = create_app(self.root, actor_factory=lambda: (TestActor(), "scripted-test"))
        with patch.dict(os.environ, {"POCKET_ADMIN_TOKEN": "unit-test-only"}):
            with TestClient(app) as client:
                request = {"task": TASK.model_dump(), "initial_state": INITIAL, "backend": "fixture"}
                self.assertEqual(client.post("/v3/tasks", json=request).status_code, 401)
                headers = {"Authorization": "Bearer unit-test-only"}
                response = client.post("/v3/tasks", json=request, headers=headers)
                self.assertEqual(response.status_code, 202)
                run_id = response.json()["run_id"]
                for _ in range(150):
                    result = client.get(f"/v3/tasks/{run_id}", headers=headers).json()
                    if result["status"] == "completed":
                        break
                    time.sleep(0.01)
                self.assertEqual(result["status"], "completed", result.get("error"))
                self.assertEqual(len(result["actions"]), 1)
                self.assertEqual(result["actions"][0]["status"], "confirmed")

    def test_api_refuses_missing_admin_token(self):
        with patch.dict(os.environ, {"POCKET_ADMIN_TOKEN": ""}):
            with TestClient(create_app(self.root)) as client:
                self.assertEqual(client.get("/health").status_code, 200)
                self.assertEqual(client.get("/v3/tasks/missing").status_code, 503)
