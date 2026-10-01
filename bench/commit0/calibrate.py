"""Grader calibration (docs/60 §10 step 2): for every prepared library, the reference
implementation (upstream tests removed, as the agents would leave it) should score about 1 and
the seed about 0. Writes calibration.json next to this file.

Usage (from the repo root, venv active): python bench/commit0/calibrate.py [lib ...]
"""

import json
import subprocess
import sys
import tarfile
import tempfile
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from prepare import SEEDS, instances  # noqa: E402


def grade(lib: str, ws: Path, out: Path) -> dict:
    subprocess.run([sys.executable, str(HERE / "grade.py"), lib, str(ws), "--json", str(out)],
                   capture_output=True, text=True)
    return json.loads(out.read_text()) if out.exists() else {"score": None}


def one(lib: str, inst: dict) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        ref = Path(tmp) / "ref"
        ref.mkdir()
        data = subprocess.run(["git", "-C", str(HERE / "repos" / lib), "archive",
                               inst["reference_commit"]], check=True, capture_output=True).stdout
        with tarfile.open(fileobj=BytesIO(data)) as tf:
            tf.extractall(ref, filter="data")
        r = grade(lib, ref, Path(tmp) / "ref.json")
        s = grade(lib, SEEDS / lib, Path(tmp) / "seed.json")
    row = {"lib": lib, "total": r.get("total"), "reference": r.get("score"),
           "seed": s.get("score")}
    print(json.dumps(row), flush=True)
    return row


def main() -> None:
    insts = instances()
    libs = sys.argv[1:] or sorted(p.stem for p in SEEDS.glob("*.json"))
    with ThreadPoolExecutor(max_workers=8) as ex:
        rows = list(ex.map(lambda lib: one(lib, insts[lib]), libs))
    (HERE / "calibration.json").write_text(json.dumps(rows, indent=2))


if __name__ == "__main__":
    main()
