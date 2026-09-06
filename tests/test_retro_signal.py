"""M-F1: structural tests for the line F O_RETRO_INVOKE signal convention
(sciteam/retro_signal.py, schemas/retro_request.schema.json).

No new tool/capability is exercised here — the signal is written with the
same generic `fs.write` semantics every mission seat already has (see
sciteam/fs_tools.write_file); these tests only cover the schema + the
harness-side scanner that decides whether to act on it.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
from sciteam import retro_signal

LAB_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = LAB_ROOT / "schemas" / "retro_request.schema.json"


def test_schema_is_valid_draft_2020_12():
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)


def test_write_retro_request_round_trips(tmp_path):
    workspace = tmp_path / "missions" / "m_build"
    path = retro_signal.write_retro_request(
        workspace,
        requested_by="agent_build_01",
        reason="results.json regressed on held-in task 3 vs the prior campaign",
        requested_at="2026-08-11T00:00:00+00:00",
    )
    assert path.name == retro_signal.SIGNAL_FILENAME
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["requested_by"] == "agent_build_01"
    assert "target_asset_hint" not in payload


def test_write_retro_request_rejects_missing_reason(tmp_path):
    with pytest.raises(jsonschema.ValidationError):
        retro_signal.write_retro_request(
            tmp_path, requested_by="a", reason="", requested_at="2026-08-11T00:00:00+00:00"
        )


def test_scan_finds_signals_across_missions(tmp_path):
    run_dir = tmp_path / "run_a"
    retro_signal.write_retro_request(
        run_dir / "missions" / "m_build",
        requested_by="agent_build_01",
        reason="stalled twice",
        requested_at="2026-08-11T00:00:00+00:00",
    )
    (run_dir / "missions" / "m_hyp").mkdir(parents=True)  # no signal here
    signals = retro_signal.scan_for_retro_requests(run_dir)
    assert [s.mission_id for s in signals] == ["m_build"]
    assert signals[0].payload["requested_by"] == "agent_build_01"


def test_scan_empty_run_dir_returns_empty(tmp_path):
    assert retro_signal.scan_for_retro_requests(tmp_path / "nope") == []


def test_scan_ignores_malformed_signal_fail_closed(tmp_path):
    run_dir = tmp_path / "run_b"
    mission_dir = run_dir / "missions" / "m_build"
    mission_dir.mkdir(parents=True)
    (mission_dir / retro_signal.SIGNAL_FILENAME).write_text("not json at all", encoding="utf-8")
    assert retro_signal.scan_for_retro_requests(run_dir) == []


def test_scan_ignores_signal_missing_required_field(tmp_path):
    run_dir = tmp_path / "run_c"
    mission_dir = run_dir / "missions" / "m_build"
    mission_dir.mkdir(parents=True)
    (mission_dir / retro_signal.SIGNAL_FILENAME).write_text(
        json.dumps({"requested_by": "a"}), encoding="utf-8"
    )
    assert retro_signal.scan_for_retro_requests(run_dir) == []


def test_should_invoke_retro(tmp_path):
    run_dir = tmp_path / "run_d"
    assert retro_signal.should_invoke_retro(run_dir) is False
    retro_signal.write_retro_request(
        run_dir / "missions" / "m_build",
        requested_by="a",
        reason="x",
        requested_at="2026-08-11T00:00:00+00:00",
    )
    assert retro_signal.should_invoke_retro(run_dir) is True


def test_first_request_delay_index(tmp_path):
    run_dir = tmp_path / "run_e"
    retro_signal.write_retro_request(
        run_dir / "missions" / "m_hyp",
        requested_by="a",
        reason="x",
        requested_at="2026-08-11T00:00:00+00:00",
    )
    order = ["m_protocol", "m_rules", "m_hyp", "m_build"]
    assert retro_signal.first_request_delay(run_dir, order) == 2


def test_first_request_delay_none_when_no_signal(tmp_path):
    run_dir = tmp_path / "run_f"
    (run_dir / "missions" / "m_protocol").mkdir(parents=True)
    assert retro_signal.first_request_delay(run_dir, ["m_protocol"]) is None
