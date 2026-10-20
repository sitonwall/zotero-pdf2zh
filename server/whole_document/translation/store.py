import json
import os
from contextlib import contextmanager
from pathlib import Path


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temporary, path)


class Store:
    def __init__(self, root, task_id):
        self.path = Path(root) / task_id
        self.path.mkdir(parents=True, exist_ok=True)

    def read(self):
        p = self.path / "checkpoint.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    def save(self, value):
        atomic_json(self.path / "checkpoint.json", value)

    def ledger(self, event):
        with (self.path / "ledger.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def events(self):
        p = self.path / "ledger.jsonl"
        if not p.exists():
            return []
        result = []
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                result.append(json.loads(line))
            except ValueError:
                # An interrupted dispatch remains reserved by its previous durable event.
                continue
        return result

    @contextmanager
    def locked(self, filename="task.lock"):
        # OS lock is released on process exit; stale files do not block explicit resume.
        lock = (self.path / filename).open("a+b")
        lock.seek(0)
        if lock.read(1) == b"":
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock.close()
            raise RuntimeError("task_already_running") from None
        try:
            yield
        finally:
            lock.close()
