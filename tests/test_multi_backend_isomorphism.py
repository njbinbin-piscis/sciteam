"""Multi-backend dual-run institutional isomorphism test.

Proves the core architectural thesis:
  Physical Action != Institutional Effect.
  Graph Engineering governs execution topology;
  Institution Engineering governs authority and validity.

Runs the exact same canonical institution manifest under two distinct runtime
backends (Anthropic Claude and DeepSeek R1). Proves that while physical outputs,
tokens, model IDs, and reasoning traces diverge, the sequence of institutional
validity events (seats, authorities, effects, vetoes, reopens, and closures)
remains strictly isomorphic and verifiable.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sciteam.canonical_demo import run_full
from sciteam.event_log import LEDGER_FILENAME, read_events, replay, verify_chain

sciteam_runtime_anthropic = pytest.importorskip("sciteam_runtime_anthropic")
sciteam_runtime_deepseek = pytest.importorskip("sciteam_runtime_deepseek")


def _fake_anthropic_http_response() -> MagicMock:
    payload = {
        "id": "msg_ant_123",
        "type": "message",
        "role": "assistant",
        "model": "claude-3-5-sonnet-20241022",
        "content": [{"type": "text", "text": "Proposed fix executed."}],
        "usage": {"input_tokens": 125, "output_tokens": 42},
    }
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(payload).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp
    return mock_resp


def _fake_deepseek_http_response() -> MagicMock:
    payload = {
        "id": "chatcmpl_ds_456",
        "model": "deepseek-reasoner",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "DeepSeek fix executed.",
                    "reasoning_content": "Detailed step-by-step institutional verification.",
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 150, "completion_tokens": 85},
    }
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(payload).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp
    return mock_resp


def test_canonical_run_multi_backend_isomorphism(tmp_path: Path) -> None:
    """Run identical institution under Anthropic Claude and DeepSeek R1."""
    out_ant = tmp_path / "anthropic_run"
    out_ds = tmp_path / "deepseek_run"

    # 1. Initialize runtime ports
    from sciteam_runtime_anthropic.runtime import AnthropicRuntimePort
    from sciteam_runtime_deepseek.runtime import DeepSeekRuntimePort

    port_ant = AnthropicRuntimePort(api_key="test-key-ant")
    port_ds = DeepSeekRuntimePort(api_key="test-key-ds", model="deepseek-reasoner")

    # 2. Run canonical institution under Anthropic backend
    with patch("urllib.request.urlopen", return_value=_fake_anthropic_http_response()):
        res_ant = run_full(out_ant, config_swap=False, log=[], runtime=port_ant)

    # 3. Run canonical institution under DeepSeek backend
    with patch("urllib.request.urlopen", return_value=_fake_deepseek_http_response()):
        res_ds = run_full(out_ds, config_swap=False, log=[], runtime=port_ds)

    # 4. Verify verdicts
    assert res_ant["verdict"]["closed_legally"] is True
    assert res_ds["verdict"]["closed_legally"] is True

    # 5. Read ledgers and verify cryptographic hash chains
    path_ant = out_ant / LEDGER_FILENAME
    path_ds = out_ds / LEDGER_FILENAME

    events_ant = read_events(path_ant)
    events_ds = read_events(path_ds)

    chain_ant = verify_chain(path_ant)
    chain_ds = verify_chain(path_ds)

    assert chain_ant.ok is True
    assert chain_ds.ok is True

    # 6. Assert Structural Isomorphism on institutional control plane
    # Filter out physical telemetry (llm_call) to inspect institutional lifecycle
    inst_ant = [e for e in events_ant if e.kind != "llm_call"]
    inst_ds = [e for e in events_ds if e.kind != "llm_call"]

    assert len(inst_ant) == len(inst_ds)

    expected_sequence = [
        "run_started",
        "rule_version_set",
        "artifact_submitted",  # bystander rejected
        "artifact_accepted",  # implementer attempt 1 accepted
        "audit_reported",  # auditor attempt 1 fails
        "veto_issued",  # auditor issues authorized veto
        "reopened",  # planner mechanical reopen
        "artifact_accepted",  # implementer attempt 2 accepted
        "audit_reported",  # auditor attempt 2 passes
        "closed",  # chair legal close
        "run_finished",
    ]
    assert [e.kind for e in inst_ant] == expected_sequence
    assert [e.kind for e in inst_ds] == expected_sequence

    for i in range(len(inst_ant)):
        ea = inst_ant[i]
        ed = inst_ds[i]
        assert ea.kind == ed.kind
        assert ea.seat == ed.seat
        assert ea.authority == ed.authority
        assert ea.effect == ed.effect
        assert ea.rule_version == ed.rule_version

    # 7. Assert LedgerState equivalence via replay
    state_ant = replay(events_ant)
    state_ds = replay(events_ds)

    assert state_ant.finished is True
    assert state_ds.finished is True
    assert state_ant.effective_artifacts == state_ds.effective_artifacts
    assert [a["seat"] for a in state_ant.refused_acts] == ["bystander"]
    assert [a["authority"] for a in state_ant.refused_acts] == ["may_emit_artifact"]
    assert [a["seat"] for a in state_ds.refused_acts] == ["bystander"]
    assert [a["authority"] for a in state_ds.refused_acts] == ["may_emit_artifact"]
    assert len(state_ant.vetoes) == len(state_ds.vetoes) == 1
    assert len(state_ant.reopenings) == len(state_ds.reopenings) == 1

    # 8. Assert Physical Divergence: different models, tokens, and reasoning metadata
    llm_calls_ant = [e for e in events_ant if e.kind == "llm_call"]
    llm_calls_ds = [e for e in events_ds if e.kind == "llm_call"]

    assert len(llm_calls_ant) == 4  # implementer(1) + auditor(1) + implementer(2) + auditor(2)
    assert len(llm_calls_ds) == 4

    assert llm_calls_ant[0].payload["model"] == "claude-3-5-sonnet-20241022"
    assert llm_calls_ds[0].payload["model"] == "deepseek-reasoner"

    # DeepSeek isolated reasoning_content preserved in payload metadata
    assert "reasoning_content" in llm_calls_ds[0].payload["metadata"]
    assert "step-by-step" in llm_calls_ds[0].payload["metadata"]["reasoning_content"]

    # Final cryptographic hashes diverge because physical payloads differed
    assert events_ant[-1].hash != events_ds[-1].hash


def test_cli_runtime_option(tmp_path: Path) -> None:
    """Test sciteam demo CLI integration with --runtime flag."""
    from sciteam import canonical_demo

    # 1. Unknown runtime exits with code 1
    parser = canonical_demo.build_parser()
    args_bad = parser.parse_args(["--runtime", "no_such_provider", "--out", str(tmp_path / "bad")])
    assert canonical_demo.run(args_bad) == 1

    # 2. Registered runtime plugin loads dynamically and executes
    out_cli = tmp_path / "cli_ant"
    args_good = parser.parse_args(["--runtime", "anthropic", "--out", str(out_cli)])
    with patch("urllib.request.urlopen", return_value=_fake_anthropic_http_response()):
        assert canonical_demo.run(args_good) == 0

    events = read_events(out_cli / LEDGER_FILENAME)
    llm_calls = [e for e in events if e.kind == "llm_call"]
    assert len(llm_calls) == 4
    assert (out_cli / "summary.json").is_file()
