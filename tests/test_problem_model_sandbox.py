"""Problem models run as bounded, offline workspace artifacts."""

from __future__ import annotations

import json
from pathlib import Path

from sciteam.capabilities import RuntimeCapabilities, preflight_mission
from sciteam.sandbox import LocalPythonSandbox, SandboxRequest


def test_model_backtest_mismatch_revision_and_certify(tmp_path: Path) -> None:
    cases = {"events": [{"x": 1, "y": 3}, {"x": 2, "y": 5}]}
    (tmp_path / "cases.json").write_text(json.dumps(cases), encoding="utf-8")
    bad_model = """
import json
cases = json.load(open("cases.json"))
rows = [{"expected": e["y"], "predicted": 2 * e["x"]} for e in cases["events"]]
result = {
    "rows": rows,
    "mismatches": [r for r in rows if r["expected"] != r["predicted"]],
}
json.dump(result, open("report.json", "w"))
"""
    (tmp_path / "model.py").write_text(bad_model, encoding="utf-8")
    sandbox = LocalPythonSandbox(tmp_path)
    request = SandboxRequest(
        program="model.py",
        inputs=("cases.json",),
        outputs=("report.json",),
    )
    first = sandbox.run(request)
    assert first.ok
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert len(report["mismatches"]) == 2

    fixed_model = bad_model.replace('2 * e["x"]', '2 * e["x"] + 1')
    (tmp_path / "model.py").write_text(fixed_model, encoding="utf-8")
    certified = sandbox.certify(request)
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert certified.ok and report["mismatches"] == []
    assert certified.output_hashes["report.json"]
    assert certified.environment_fingerprint


def test_sandbox_rejects_escape_timeout_and_network(tmp_path: Path) -> None:
    sandbox = LocalPythonSandbox(tmp_path)
    escaped = sandbox.run(SandboxRequest(program="../outside.py"))
    assert not escaped.ok and escaped.reason_code == "sandbox_path_denied"

    (tmp_path / "loop.py").write_text("while True: pass\n", encoding="utf-8")
    timed = sandbox.run(SandboxRequest(program="loop.py", timeout_seconds=1))
    assert not timed.ok
    assert timed.timed_out or timed.exit_code not in {0, None}

    (tmp_path / "network.py").write_text(
        "import socket\nsocket.create_connection(('example.com', 80))\n",
        encoding="utf-8",
    )
    denied = sandbox.run(SandboxRequest(program="network.py"))
    assert not denied.ok
    assert "sandbox import denied" in denied.stderr


def test_model_skill_capability_preflight(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "model.as_program"
    skill_dir.mkdir(parents=True)
    source = Path(__file__).parents[1] / "assets" / "skills" / "model.as_program" / "SKILL.md"
    (skill_dir / "SKILL.md").write_text(source.read_text(encoding="utf-8"))
    missing = preflight_mission(
        skills_dir=tmp_path / "skills",
        allowlist=["model.as_program"],
        available=frozenset(),
    )
    assert not missing.ok
    available = RuntimeCapabilities.lab(eval_runner=False)
    assert preflight_mission(
        skills_dir=tmp_path / "skills",
        allowlist=["model.as_program"],
        available=available,
    ).ok
