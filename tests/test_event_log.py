"""The ledger's value rests entirely on it being hard to forge.

These tests therefore spend most of their effort on the adversarial side:
edit a field, drop a line, reorder two entries, splice in a well-formed
record — each must be caught, and caught at the right position.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sciteam.event_log import (
    CORE_KINDS,
    GENESIS_HASH,
    EventLog,
    LedgerError,
    cost_report,
    entry_hash,
    read_events,
    replay,
    verify_chain,
)


def _lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _rewrite(path: Path, records: list[dict]) -> None:
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records))


@pytest.fixture
def ledger(tmp_path: Path) -> EventLog:
    log = EventLog(tmp_path / "ledger.jsonl", run_id="run_1")
    log.set_rule_version("inst@v1")
    log.append(
        "artifact_submitted",
        effect="rejected",
        seat="bystander",
        authority="may_submit_artifact",
        payload={"subject": "patch_1", "reason": "seat lacks may_submit_artifact"},
    )
    log.append(
        "artifact_accepted",
        effect="accepted",
        seat="implementer",
        authority="may_submit_artifact",
        payload={"subject": "patch_1"},
    )
    return log


class TestAppendAndChain:
    def test_first_entry_links_to_genesis(self, tmp_path: Path) -> None:
        log = EventLog(tmp_path / "l.jsonl", run_id="run_1")
        event = log.append("run_started")
        assert event.seq == 1
        assert event.prev_hash == GENESIS_HASH
        assert event.hash == entry_hash(event.to_dict())

    def test_each_entry_commits_to_its_predecessor(self, ledger: EventLog) -> None:
        events = ledger.events()
        assert [e.seq for e in events] == [1, 2, 3]
        for earlier, later in zip(events[:-1], events[1:], strict=True):
            assert later.prev_hash == earlier.hash
        assert ledger.head_hash == events[-1].hash

    def test_rule_version_is_stamped_onto_later_entries(self, ledger: EventLog) -> None:
        assert [e.rule_version for e in ledger.events()] == ["inst@v1"] * 3

    def test_rule_version_change_records_the_previous_one(self, ledger: EventLog) -> None:
        event = ledger.set_rule_version("inst@v2")
        assert event.payload["previous"] == "inst@v1"
        assert ledger.rule_version == "inst@v2"

    def test_field_order_on_disk_is_stable(self, ledger: EventLog) -> None:
        for record in _lines(ledger.path):
            assert list(record) == [
                "seq",
                "ts",
                "run_id",
                "kind",
                "seat",
                "authority",
                "effect",
                "rule_version",
                "payload",
                "prev_hash",
                "hash",
            ]

    def test_unknown_kind_is_refused(self, ledger: EventLog) -> None:
        with pytest.raises(LedgerError, match="unknown event kind"):
            ledger.append("artifact_blessed")

    def test_plugin_kinds_are_allowed_under_the_x_prefix(self, ledger: EventLog) -> None:
        event = ledger.append("x_lint_findings", payload={"count": 3})
        assert event.kind == "x_lint_findings"
        assert verify_chain(ledger.path).ok

    def test_unknown_effect_is_refused(self, ledger: EventLog) -> None:
        with pytest.raises(LedgerError, match="effect must be one of"):
            ledger.append("closed", effect="maybe")

    def test_core_kinds_match_the_schema(self) -> None:
        from sciteam._paths import SCHEMAS_DIR

        schema = json.loads((SCHEMAS_DIR / "institution_event.schema.json").read_text())
        pattern = schema["properties"]["kind"]["pattern"]
        declared = set(pattern.removeprefix("^(").removesuffix(")$").split("|"))
        assert declared - {"x_[a-z0-9_]+"} == set(CORE_KINDS)


class TestResume:
    def test_reopening_extends_the_same_chain(self, ledger: EventLog, tmp_path: Path) -> None:
        reopened = EventLog(tmp_path / "ledger.jsonl")
        assert reopened.run_id == "run_1"
        assert reopened.length == 3
        assert reopened.rule_version == "inst@v1"
        event = reopened.append("closed", effect="accepted", seat="chair")
        assert event.seq == 4
        assert event.prev_hash == ledger.head_hash
        assert verify_chain(tmp_path / "ledger.jsonl").ok

    def test_a_new_ledger_needs_a_run_id(self, tmp_path: Path) -> None:
        with pytest.raises(LedgerError, match="needs an explicit run_id"):
            EventLog(tmp_path / "fresh.jsonl")

    def test_a_second_run_cannot_share_one_chain(self, ledger: EventLog, tmp_path: Path) -> None:
        with pytest.raises(LedgerError, match="one chain per run"):
            EventLog(tmp_path / "ledger.jsonl", run_id="run_2")


class TestTamperDetection:
    def test_a_clean_chain_verifies(self, ledger: EventLog) -> None:
        result = verify_chain(ledger.path)
        assert result.ok and bool(result) is True
        assert result.length == 3
        assert result.problems == ()

    def test_editing_a_payload_is_caught_at_that_entry(self, ledger: EventLog) -> None:
        records = _lines(ledger.path)
        records[1]["effect"] = "accepted"
        _rewrite(ledger.path, records)
        result = verify_chain(ledger.path)
        assert not result.ok
        assert any("entry 2: hash mismatch" in p for p in result.problems)

    def test_rehashing_an_edit_still_breaks_the_link(self, ledger: EventLog) -> None:
        # The strongest realistic attack: edit an entry *and* fix its own hash.
        # The chain still fails because the successor commits to the old hash.
        records = _lines(ledger.path)
        records[1]["effect"] = "accepted"
        records[1]["hash"] = entry_hash(records[1])
        _rewrite(ledger.path, records)
        result = verify_chain(ledger.path)
        assert not result.ok
        assert any("entry 3" in p and "does not link" in p for p in result.problems)

    def test_deleting_an_entry_is_caught(self, ledger: EventLog) -> None:
        records = _lines(ledger.path)
        _rewrite(ledger.path, [records[0], records[2]])
        result = verify_chain(ledger.path)
        assert not result.ok
        assert any("seq is 3, expected 2" in p for p in result.problems)

    def test_reordering_entries_is_caught(self, ledger: EventLog) -> None:
        records = _lines(ledger.path)
        _rewrite(ledger.path, [records[0], records[2], records[1]])
        result = verify_chain(ledger.path)
        assert not result.ok

    def test_appending_a_forged_entry_is_caught(self, ledger: EventLog) -> None:
        records = _lines(ledger.path)
        forged = dict(records[-1])
        forged.update(seq=4, kind="closed", effect="accepted", seat="implementer")
        forged["hash"] = entry_hash(forged)
        _rewrite(ledger.path, [*records, forged])
        result = verify_chain(ledger.path)
        assert not result.ok
        assert any("entry 4" in p and "does not link" in p for p in result.problems)

    def test_corrupt_json_is_reported_not_raised(self, ledger: EventLog) -> None:
        ledger.path.write_text(ledger.path.read_text() + "{not json\n")
        result = verify_chain(ledger.path)
        assert not result.ok
        assert "not valid JSON" in result.problems[0]

    def test_missing_file_verifies_as_an_empty_chain(self, tmp_path: Path) -> None:
        result = verify_chain(tmp_path / "absent.jsonl")
        assert result.ok
        assert result.length == 0
        assert result.head == GENESIS_HASH

    def test_reading_an_entry_without_required_fields_fails(self, tmp_path: Path) -> None:
        path = tmp_path / "l.jsonl"
        path.write_text(json.dumps({"seq": 1, "kind": "closed"}) + "\n")
        with pytest.raises(LedgerError, match="missing required fields"):
            read_events(path)


class TestReplay:
    def test_state_is_derived_from_the_record(self, ledger: EventLog) -> None:
        state = replay(ledger.path)
        assert state.run_id == "run_1"
        assert state.rule_version == "inst@v1"
        assert "patch_1" in state.effective_artifacts
        assert state.effective_artifacts["patch_1"]["seat"] == "implementer"
        assert [act["seat"] for act in state.refused_acts] == ["bystander"]

    def test_a_veto_withdraws_institutional_effect(self, ledger: EventLog) -> None:
        ledger.append(
            "veto_issued",
            effect="accepted",
            seat="auditor",
            authority="may_veto",
            payload={"subject": "patch_1", "reason": "frozen tests fail"},
        )
        state = replay(ledger.path)
        assert "patch_1" not in state.effective_artifacts
        assert state.vetoes[0]["reason"] == "frozen tests fail"

    def test_reopening_undoes_a_close(self, ledger: EventLog) -> None:
        ledger.append("closed", effect="accepted", seat="chair", payload={"subject": "patch_1"})
        ledger.append("reopened", effect="accepted", seat="chair", payload={"subject": "patch_1"})
        state = replay(ledger.path)
        assert state.closed == []
        assert len(state.reopenings) == 1

    def test_repair_after_reopen_takes_effect_again(self, ledger: EventLog) -> None:
        ledger.append("reopened", effect="accepted", seat="chair", payload={"subject": "patch_1"})
        ledger.append(
            "artifact_accepted",
            effect="accepted",
            seat="implementer",
            payload={"subject": "patch_1", "attempt": 2},
        )
        ledger.append("closed", effect="accepted", seat="chair", payload={"subject": "patch_1"})
        ledger.append("run_finished")
        state = replay(ledger.path)
        assert state.effective_artifacts["patch_1"]["attempt"] == 2
        assert [c["subject"] for c in state.closed] == ["patch_1"]
        assert state.finished

    def test_amendment_status_tracks_the_last_decision(self, ledger: EventLog) -> None:
        ledger.append(
            "amendment_decided",
            effect="accepted",
            seat="chair",
            payload={"amendment_id": "amd_1", "status": "approved"},
        )
        ledger.append("amendment_applied", effect="accepted", payload={"amendment_id": "amd_1"})
        assert replay(ledger.path).amendments == {"amd_1": "applied"}

    def test_rule_version_history_is_ordered(self, ledger: EventLog) -> None:
        ledger.set_rule_version("inst@v2")
        assert replay(ledger.path).rule_versions == ["inst@v1", "inst@v2"]


class TestCostReport:
    def test_spend_is_attributed_to_the_seat_that_incurred_it(self, ledger: EventLog) -> None:
        for seat, usd in (("auditor", 0.7), ("auditor", 0.4), ("implementer", 0.2)):
            ledger.append(
                "llm_call",
                seat=seat,
                payload={
                    "model": "m1",
                    "usage": {"prompt_tokens": 100, "completion_tokens": 10},
                    "usd": usd,
                },
            )
        lines = cost_report(ledger.path)
        assert [(line.seat, line.calls, line.usd) for line in lines] == [
            ("auditor", 2, 1.1),
            ("implementer", 1, 0.2),
        ]
        assert lines[0].prompt_tokens == 200

    def test_calls_without_usage_still_count(self, ledger: EventLog) -> None:
        ledger.append("llm_call", seat="chair", payload={"model": "m1"})
        line = cost_report(ledger.path)[0]
        assert line.calls == 1 and line.usd == 0.0

    def test_unattributed_calls_are_labelled_not_dropped(self, ledger: EventLog) -> None:
        ledger.append("llm_call", payload={"usd": 0.5})
        line = cost_report(ledger.path)[0]
        assert line.seat == "<unattributed>" and line.model == "<unknown>"

    def test_a_ledger_without_llm_calls_reports_nothing(self, ledger: EventLog) -> None:
        assert cost_report(ledger.path) == []
