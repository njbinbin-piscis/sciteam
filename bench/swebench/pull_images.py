"""Pre-pull SWE-bench Verified evaluation images.

Resumable: images already present locally are skipped. Stops when free disk
space drops below --min-free-gb so the host never fills up mid-run.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent


def image_name(instance_id: str) -> str:
    return f"swebench/sweb.eval.x86_64.{instance_id.replace('__', '_1776_')}:latest"


def present(image: str) -> bool:
    return subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode == 0


def free_gb(path: str = "/var/lib/docker") -> float:
    return shutil.disk_usage(path).free / 1e9


def pull(instance_id: str, retries: int, log: Path, min_free_gb: float) -> None:
    image = image_name(instance_id)
    if present(image):
        record = {"instance_id": instance_id, "image": image, "status": "present"}
    elif free_gb() < min_free_gb:
        record = {"instance_id": instance_id, "image": image, "status": "skipped_low_disk"}
    else:
        status = "error"
        start = time.time()
        for attempt in range(1, retries + 1):
            proc = subprocess.run(["docker", "pull", "-q", image], capture_output=True, text=True)
            if proc.returncode == 0:
                status = "ok"
                break
            status = f"error(attempt {attempt}): {proc.stderr.strip()[-300:]}"
            time.sleep(10 * attempt)
        record = {
            "instance_id": instance_id,
            "image": image,
            "status": status,
            "seconds": round(time.time() - start, 1),
            "free_gb_after": round(free_gb(), 1),
        }
    with log.open("a") as fh:
        fh.write(json.dumps(record) + "\n")
    print(json.dumps(record), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ids", default=str(HERE / "verified_ids.txt"))
    parser.add_argument("--log", default=str(HERE / "pull_log.jsonl"))
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--min-free-gb", type=float, default=60.0)
    args = parser.parse_args()
    ids = [line.strip() for line in Path(args.ids).read_text().splitlines() if line.strip()]
    log = Path(args.log)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for instance_id in ids:
            pool.submit(pull, instance_id, args.retries, log, args.min_free_gb)
    return 0


if __name__ == "__main__":
    sys.exit(main())
