"""Loopback-only whole-document service with a legacy-safe Zotero boundary."""

import argparse
import base64
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from flask import Flask, Response, abort, jsonify, request, send_file

from translation.coordinator import task_identity
from translation.store import Store, atomic_json
from worker.run import validate_config


def legacy_payload(task):
    result = dict(task)
    result["resultState"] = task["status"]
    if task["status"] == "complete":
        result["status"] = "success"
    elif task.get("finished"):
        result.update(
            status="failed",
            fileList=[],
            filePaths=[],
            message=(
                "部分成功：请在监控页下载带标记的部分译文和 QA 清单。"
                if task["status"] == "partial"
                else "任务未完整完成，请查看监控页。"
            ),
        )
    return result


def create_app(config_path, runs, python=sys.executable):
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024
    runs = Path(runs)
    runs.mkdir(parents=True, exist_ok=True)
    config_path = Path(config_path)
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pdf-worker")
    lock = threading.Lock()
    futures = {}
    session_tasks = set()

    def settings():
        value = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Channel configuration must be an object")
        return value

    def records():
        result = []
        for path in runs.glob("*/status.json"):
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
                item["can_resume"] = (path.parent / "channel.private.json").exists() and (
                    path.parent / "source.pdf"
                ).exists()
                if not item.get("finished") and item["taskId"] not in session_tasks:
                    item.update(status="interrupted", stage="explicit_resume_required")
                elif not item.get("finished") and item.get("started_at"):
                    item["elapsed_seconds"] = round(time.time() - item["started_at"], 1)
                result.append(item)
            except (ValueError, OSError):
                pass
        return sorted(result, key=lambda x: x.get("taskId", ""))

    def launch(task_id, source, task_config, resume=False):
        env = os.environ.copy()
        for name in ("PYTHONHOME", "PYTHONPATH"):
            env.pop(name, None)
        env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONNOUSERSITE="1")
        path = runs / task_id
        with (path / "worker.log").open("ab") as log:
            cmd = [
                str(python),
                "-u",
                str(ROOT / "worker/run.py"),
                str(source),
                "--config",
                str(task_config),
                "--runs",
                str(runs),
            ]
            if resume:
                cmd.append("--resume")
            proc = subprocess.Popen(
                cmd,
                cwd=ROOT,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            while proc.poll() is None:
                if (path / "cancel.request").exists():
                    # Gateway checks cancellation at dispatch; kill bounds long synchronous HTTP wait.
                    proc.kill()
                    proc.wait()
                    break
                time.sleep(0.25)
        status_path = path / "status.json"
        state = json.loads(status_path.read_text(encoding="utf-8"))
        if not state.get("finished"):
            state.update(
                status="cancelled" if (path / "cancel.request").exists() else "failed",
                finished=True,
                stage="finished",
                fileList=[],
                errors=["worker_interrupted"],
            )
            atomic_json(status_path, state)
        return state

    @app.before_request
    def local_requests_only():
        # Prevent browser DNS rebinding and cross-site paid submissions.
        host = request.host.split(":")[0].lower()
        if host not in ("localhost", "127.0.0.1", "[::1]"):
            abort(403)
        origin = request.headers.get("Origin", "")
        port = request.host.rsplit(":", 1)[1] if ":" in request.host else "80"
        if origin and origin not in ("null", f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
            abort(403)

    @app.get("/health")
    def health():
        ready = False
        cfg = {}
        try:
            cfg = settings()
            validate_config(cfg)
            from translation.gateway import Gateway

            Gateway(cfg, lambda event: None)
            ready = True
        except (ValueError, KeyError, OSError):
            pass
        return jsonify(
            status="ok",
            version="whole-document-v1",
            worker="explicit-document-adapter",
            configured=ready,
            model=cfg.get("model"),
            automatic_translation=False,
            protocol=["accepted", "whole-document-v2"],
        )

    @app.get("/")
    def index():
        return send_file(ROOT / "server/whole_index.html")

    @app.get("/api/tasks")
    def tasks():
        return jsonify(status="success", tasks=[legacy_payload(t) for t in records()])

    @app.get("/api/history")
    def history():
        return jsonify(status="success", history=[legacy_payload(t) for t in records() if t.get("finished")])

    @app.get("/api/v2/tasks")
    def tasks_v2():
        result = records()
        for item in result:
            events = Store(runs, item["taskId"]).events()
            completed = [e for e in events if e.get("event") == "result"]
            usages = [e["usage"] for e in completed if e.get("usage")]
            item["usage"] = {
                key: sum(u.get(key, 0) or 0 for u in usages)
                for key in ("prompt_tokens", "completion_tokens", "total_tokens")
            }
            item["attempts"] = sum(e.get("event") == "dispatch" for e in events)
            item["usage"]["unknown_attempts"] = item["attempts"] - len(usages)
        return jsonify(tasks=result)

    @app.post("/translate")
    def submit():
        try:
            data = request.get_json()
            name = data.get("fileName", "")
            if not isinstance(name, str) or "/" in name or "\\" in name or not name.lower().endswith(".pdf"):
                raise ValueError("invalid_filename")
            raw = data.get("fileContent", "")
            if raw.startswith("data:application/pdf;base64,"):
                raw = raw.split(",", 1)[1]
            content = base64.b64decode(raw, validate=True)
            if not content.startswith(b"%PDF-"):
                raise ValueError("invalid_pdf")
            cfg = settings()
            cfg["target_language"] = data.get("targetLang") or cfg["target_language"]
            validate_config(cfg)
            from translation.gateway import Gateway

            Gateway(cfg, lambda event: None)  # Validate monetary settings before creating a paid job.
            task_id = task_identity(hashlib.sha256(content).hexdigest(), cfg)
            path = runs / task_id
            with lock:
                if not (path / "status.json").exists():
                    path.mkdir(parents=True, exist_ok=True)
                    source = path / "source.pdf"
                    source.write_bytes(content)
                    atomic_json(path / "request.json", {"fileName": name})
                    # Private per-task snapshot; never allow browser to override endpoint or credentials.
                    atomic_json(path / "channel.private.json", cfg)
                    atomic_json(
                        path / "status.json",
                        {
                            "taskId": task_id,
                            "fileName": name,
                            "status": "queued",
                            "finished": False,
                            "progress": None,
                            "stage": "queued",
                            "fileList": [],
                        },
                    )
                    session_tasks.add(task_id)
                    futures[task_id] = executor.submit(launch, task_id, source, path / "channel.private.json")
                future = futures.get(task_id)
            asynchronous = data.get("asyncJob") is True or request.headers.get("X-PDF2zh-Protocol") in (
                "accepted",
                "whole-document-v2",
            )
            if asynchronous:
                return jsonify(status="accepted", taskId=task_id)
            if future:
                result = future.result()
            else:
                result = json.loads((path / "status.json").read_text(encoding="utf-8"))
            return jsonify(legacy_payload(result))
        except (ValueError, KeyError, TypeError, OSError):
            return jsonify(
                status="error", message="Invalid PDF or channel settings; see local configuration."
            ), 400

    @app.post("/api/v2/tasks/<task_id>/cancel")
    def cancel(task_id):
        if not valid_id(task_id) or not (runs / task_id / "status.json").exists():
            abort(404)
        (runs / task_id / "cancel.request").touch()
        return jsonify(status="cancellation_requested")

    @app.post("/api/v2/tasks/<task_id>/resume")
    def resume(task_id):
        if not valid_id(task_id) or not (runs / task_id / "source.pdf").exists():
            abort(404)
        path = runs / task_id
        with lock:
            if task_id in futures and not futures[task_id].done():
                return jsonify(status="accepted", taskId=task_id)
            (path / "cancel.request").unlink(missing_ok=True)
            session_tasks.add(task_id)
            futures[task_id] = executor.submit(
                launch, task_id, path / "source.pdf", path / "channel.private.json", True
            )
        return jsonify(status="accepted", taskId=task_id)

    @app.get("/translatedFile/<path:filename>")
    def download(filename):
        if Path(filename).name != filename or "/" in filename or "\\" in filename:
            abort(404)
        for record in records():
            if record["status"] not in ("complete", "partial"):
                continue
            for path in record.get("filePaths", []):
                p = Path(path)
                if p.name == filename and p.resolve().is_relative_to(runs.resolve()) and p.exists():
                    return send_file(p, as_attachment=request.args.get("preview") != "true")
        abort(404)

    @app.get("/api/v2/tasks/<task_id>/qa")
    def qa(task_id):
        if not valid_id(task_id):
            abort(404)
        path = runs / task_id / "qa.json"
        if not path.exists():
            abort(404)
        return send_file(path, as_attachment=True)

    @app.get("/translatedInfo")
    def files():
        result = []
        for record in records():
            if record["status"] != "complete":
                continue  # Legacy discovery must never attach partial output.
            for path in record.get("filePaths", []):
                p = Path(path)
                if p.exists():
                    result.append({"fileName": p.name, "filePath": str(p), "size": p.stat().st_size})
        return jsonify(status="ok", files=result)

    @app.get("/events")
    def events():
        def stream():
            for _ in range(30):
                yield (
                    "data: "
                    + json.dumps({"type": "tasks", "data": [legacy_payload(t) for t in records()]})
                    + "\n\n"
                )
                time.sleep(1)

        return Response(stream(), mimetype="text/event-stream")

    return app


def valid_id(value):
    return len(value) == 64 and all(c in "0123456789abcdef" for c in value)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--runs", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8891)
    args = parser.parse_args()
    create_app(args.config, args.runs).run(
        host="127.0.0.1", port=args.port, threaded=True, use_reloader=False
    )
