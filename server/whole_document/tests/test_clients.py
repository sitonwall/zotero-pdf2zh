import base64
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server.whole_app import create_app
from translation.coordinator import task_identity
from translation.gateway import FileCircuit, GatewayError
from translation.store import Store, atomic_json


class ClientTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = {
            "base_url": "https://example.invalid/v1",
            "model": "fixture",
            "api_key": "private-fixture",
            "target_language": "zh-CN",
            "context_limit": 128000,
            "output_limit": 32000,
            "max_requests": 3,
            "max_task_tokens": 50000,
            "timeout_seconds": 5,
            "capacity_verified": True,
        }
        atomic_json(self.root / "config.json", self.cfg)
        self.app = create_app(self.root / "config.json", self.root / "runs")
        self.client = self.app.test_client()

    def seed(self, status):
        raw = b"%PDF-1.7\nfixture"
        task_id = task_identity(hashlib.sha256(raw).hexdigest(), self.cfg)
        store = Store(self.root / "runs", task_id)
        path = (
            store.path / "PARTIAL-fixture.mono.pdf"
            if status == "partial"
            else store.path / "fixture.mono.pdf"
        )
        path.write_bytes(b"fixture-output")
        atomic_json(
            store.path / "status.json",
            {
                "taskId": task_id,
                "fileName": "fixture.pdf",
                "status": status,
                "finished": True,
                "fileList": [path.name],
                "filePaths": [str(path)],
                "progress": 100 if status == "complete" else None,
            },
        )
        return raw, task_id, path

    def test_health_and_gets_do_not_invoke_model(self):
        for url in ["/health", "/", "/api/tasks", "/api/history", "/api/v2/tasks"]:
            result = self.client.get(url)
            self.assertEqual(result.status_code, 200)
            self.assertNotIn(b"private-fixture", result.data)
            result.close()
        self.assertFalse(list((self.root / "runs").glob("*/ledger.jsonl")))

    def test_missing_configuration_health_is_unready_without_crashing(self):
        (self.root / "config.json").unlink()
        result = self.client.get("/health")
        self.assertEqual(result.status_code, 200)
        self.assertFalse(result.json["configured"])
        self.assertIsNone(result.json["model"])

    def test_partial_is_not_legacy_success_or_discoverable(self):
        _, _, path = self.seed("partial")
        task = self.client.get("/api/tasks").json["tasks"][0]
        self.assertEqual(task["status"], "failed")
        self.assertEqual(task["resultState"], "partial")
        self.assertEqual(task["fileList"], [])
        self.assertEqual(self.client.get("/translatedInfo").json["files"], [])
        current = self.client.get("/api/v2/tasks").json["tasks"][0]
        self.assertEqual(current["status"], "partial")
        self.assertTrue(current["fileList"])
        with self.client.get("/translatedFile/" + path.name) as response:
            self.assertEqual(response.status_code, 200)

    def test_complete_legacy_file_contract(self):
        _, _, path = self.seed("complete")
        task = self.client.get("/api/history").json["history"][0]
        self.assertEqual(task["status"], "success")
        self.assertEqual(task["fileList"], [path.name])
        with self.client.get("/translatedFile/" + path.name) as response:
            self.assertEqual(response.data, b"fixture-output")

    def test_duplicate_submit_uses_same_task_no_dispatch(self):
        raw, task_id, _ = self.seed("complete")
        data = {"fileName": "fixture.pdf", "fileContent": base64.b64encode(raw).decode(), "asyncJob": True}
        for _ in range(2):
            response = self.client.post("/translate", json=data)
            self.assertEqual(response.json, {"status": "accepted", "taskId": task_id})
        self.assertFalse(list((self.root / "runs").glob("*/ledger.jsonl")))

    def test_invalid_input_and_browser_origin(self):
        self.assertEqual(self.client.post("/translate", json={"fileName": "../test.pdf"}).status_code, 400)
        self.assertEqual(self.client.get("/health", headers={"Host": "attacker.test"}).status_code, 403)
        self.assertEqual(
            self.client.post("/translate", json={}, headers={"Origin": "https://attacker.test"}).status_code,
            403,
        )
        self.assertEqual(self.client.get("/translatedFile/../channel.private.json").status_code, 404)
        self.assertEqual(self.client.get("/api/v2/tasks/invalid/qa").status_code, 404)

    def test_restart_does_not_resume_or_complete_interrupted_task(self):
        _, task_id, _ = self.seed("running")
        p = self.root / "runs" / task_id / "status.json"
        d = json.loads(p.read_text())
        d["finished"] = False
        atomic_json(p, d)
        task = self.client.get("/api/v2/tasks").json["tasks"][0]
        self.assertEqual(task["status"], "interrupted")
        self.assertFalse(list((self.root / "runs").glob("*/ledger.jsonl")))

    def test_circuit_persists_across_worker_instances(self):
        store = Store(self.root, "circuit")
        FileCircuit(store).record("fatal")
        with self.assertRaisesRegex(GatewayError, "circuit_open"):
            FileCircuit(store).check()


if __name__ == "__main__":
    unittest.main()
