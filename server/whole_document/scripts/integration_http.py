"""Console-free HTTP acceptance against a local synthetic model, never a paid endpoint."""

import base64
import json
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from translation.document import TOKEN_RE
from translation.store import atomic_json


def main():
    work = Path(sys.argv[1])
    source = Path(sys.argv[2])
    work.mkdir(parents=True, exist_ok=True)
    calls = []

    class Model(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            model = body["model"]
            inputs = json.loads(body["messages"][1]["content"])
            calls.append({"model": model, "ids": [x["paragraph_id"] for x in inputs]})
            if model == "fixture-fatal":
                self.send_response(503)
                self.end_headers()
                self.wfile.write(b'{"error":"No available channel"}')
                return
            result = []
            for index, p in enumerate(inputs):
                tokens = "".join(TOKEN_RE.findall(p["source"]))
                text = "科学翻译测试：保留公式、标题与实验结果。" + tokens
                if model == "fixture-partial" and index == 0:
                    text = p["source"]
                result.append({"paragraph_id": p["paragraph_id"], "translated_text": text})
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {
                        "choices": [
                            {
                                "finish_reason": "stop",
                                "message": {"content": json.dumps(result, ensure_ascii=False)},
                            }
                        ]
                    }
                ).encode()
            )

    model_server = ThreadingHTTPServer(("127.0.0.1", 0), Model)
    thread = threading.Thread(target=model_server.serve_forever, daemon=True)
    thread.start()
    config = {
        "base_url": f"http://127.0.0.1:{model_server.server_port}/v1",
        "model": "fixture-ok",
        "api_key": "synthetic",
        "target_language": "zh-CN",
        "context_limit": 128000,
        "output_limit": 32000,
        "max_requests": 4,
        "max_task_tokens": 100000,
        "timeout_seconds": 30,
        "capacity_verified": True,
    }
    config_file = work / "channel.json"
    atomic_json(config_file, config)
    base = "http://127.0.0.1:8893"

    def get(route):
        with urllib.request.urlopen(base + route, timeout=10) as r:
            return json.load(r)

    def service(action):
        cmd = [
            sys.executable,
            str(ROOT / "scripts/service.py"),
            action,
            "--runtime",
            str(work),
            "--port",
            "8893",
        ]
        if action == "start":
            cmd += ["--config", str(config_file), "--runs", str(work / "runs")]
        subprocess.run(cmd, check=True, cwd=ROOT, capture_output=True)

    results = []
    service("start")
    try:
        for model, expected in [
            ("fixture-ok", "complete"),
            ("fixture-partial", "partial"),
            ("fixture-fatal", "failed"),
        ]:
            config["model"] = model
            atomic_json(config_file, config)
            content = source.read_bytes()
            data = {
                "fileName": "integration.pdf",
                "fileContent": base64.b64encode(content).decode(),
                "asyncJob": True,
            }
            req = urllib.request.Request(
                base + "/translate", json.dumps(data).encode(), {"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                accepted = json.load(r)
            task_id = accepted["taskId"]
            deadline = time.monotonic() + 90
            observed = []
            while time.monotonic() < deadline:
                record = next(t for t in get("/api/v2/tasks")["tasks"] if t["taskId"] == task_id)
                observed.append(record["stage"])
                if record.get("finished"):
                    break
                time.sleep(0.3)
            assert record["status"] == expected, record
            legacy = next(t for t in get("/api/history")["history"] if t["taskId"] == task_id)
            assert legacy["status"] == ("success" if expected == "complete" else "failed"), legacy
            if expected != "complete":
                assert not legacy["fileList"]
            if expected == "partial":
                assert all(name.startswith("PARTIAL-") for name in record["fileList"])
            results.append(
                {
                    "model": model,
                    "status": expected,
                    "calls": sum(x["model"] == model for x in calls),
                    "stages_seen": sorted(set(observed)),
                    "task_id": task_id,
                }
            )
        assert results[0]["calls"] == 1
        assert 1 <= results[1]["calls"] <= 3
        assert results[2]["calls"] == 1
        before = len(calls)
        for _ in range(3):
            get("/health")
            get("/api/tasks")
        assert len(calls) == before
        service("stop")
        service("start")
        get("/api/v2/tasks")
        assert len(calls) == before
        atomic_json(
            work / "http-acceptance.json",
            {
                "results": results,
                "reload_no_dispatch": True,
                "restart_no_dispatch": True,
                "paid_model_calls": 0,
                "console_free": True,
            },
        )
        print(json.dumps(results, ensure_ascii=True))
    finally:
        service("stop")
        model_server.shutdown()
        model_server.server_close()


if __name__ == "__main__":
    main()
