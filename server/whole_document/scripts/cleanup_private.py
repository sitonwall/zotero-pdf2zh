"""Preview private debug-retention cleanup; keep results, usage and checkpoints."""

import argparse
import time
from pathlib import Path


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--runs", type=Path, required=True)
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--apply", action="store_true")
    a = p.parse_args()
    root = a.runs.resolve(strict=True)
    if a.days < 1:
        p.error("--days must be positive")
    for pattern in ("worker.log", "worker-error.txt", "channel.private.json"):
        for path in root.rglob(pattern):
            resolved = path.resolve()
            if not resolved.is_relative_to(root):
                continue
            if time.time() - resolved.stat().st_mtime < a.days * 86400:
                continue
            # Raw credentials/config may be needed for explicit resume; operators choose --apply.
            print(resolved)
            if a.apply:
                resolved.unlink()


if __name__ == "__main__":
    main()
