"""Start/stop a dedicated console-free development or release server. No model calls."""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def health(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as response:
            return json.load(response)
    except (OSError, ValueError):
        return None


def main():
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["start", "stop", "status"])
    p.add_argument("--runtime", type=Path, required=True)
    p.add_argument("--config", type=Path)
    p.add_argument("--runs", type=Path)
    p.add_argument("--python", type=Path, default=Path(sys.executable))
    p.add_argument("--port", type=int, default=8891)
    a = p.parse_args()
    a.runtime.mkdir(parents=True, exist_ok=True)
    marker = a.runtime / f"service-{a.port}.json"
    if a.action == "status":
        print(json.dumps(health(a.port)))
        return
    if a.action == "stop":
        if not marker.exists():
            raise SystemExit("No owned service marker; refusing to stop another process")
        data = json.loads(marker.read_text(encoding="utf-8"))
        import psutil

        try:
            proc = psutil.Process(data["pid"])
            if str(ROOT / "server/whole_app.py") not in proc.cmdline():
                raise SystemExit("PID ownership mismatch; refusing to stop")
            children = proc.children(recursive=True)
            for child in children:
                child.terminate()
            proc.terminate()
            psutil.wait_procs(children + [proc], timeout=10)
        except psutil.NoSuchProcess:
            pass
        marker.unlink()
        print("Owned service stopped; checkpoints preserved")
        return
    if a.port == 8890:
        raise SystemExit("Production activation disabled until online and human release gates pass")
    current = health(a.port)
    if current:
        if current.get("version") != "whole-document-v1":
            raise SystemExit("Port occupied by another service")
        print("Already running")
        return
    if not a.config or not a.runs:
        raise SystemExit("--config and --runs required")
    # Probe the exact worker interpreter and explicit engine patch before launching.
    subprocess.run([str(a.python), str(ROOT / "worker/run.py"), "--check"], check=True, cwd=ROOT)
    env = os.environ.copy()
    for key in ("PYTHONPATH", "PYTHONHOME"):
        env.pop(key, None)
    env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONNOUSERSITE="1")
    with (a.runtime / f"server-{a.port}.log").open("ab") as log:
        proc = subprocess.Popen(
            [
                str(a.python),
                str(ROOT / "server/whole_app.py"),
                "--config",
                str(a.config.resolve()),
                "--runs",
                str(a.runs.resolve()),
                "--port",
                str(a.port),
            ],
            cwd=ROOT,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    marker.write_text(json.dumps({"pid": proc.pid, "root": str(ROOT), "port": a.port}), encoding="utf-8")
    for _ in range(40):
        result = health(a.port)
        if result:
            print(json.dumps(result))
            return
        if proc.poll() is not None:
            raise SystemExit("Server exited; inspect private runtime log")
        time.sleep(0.25)
    raise SystemExit("Startup not confirmed; inspect private runtime log")


if __name__ == "__main__":
    main()
