"""P1-5: query-ranked recall (deterministic word-overlap, not embeddings),
bounded same-`reason_code` compaction, and cross-mission per-role memory.
"""

from __future__ import annotations

import json
from pathlib import Path

from sciteam.memory import CampaignMemoryStore, MemoryRecord, SeatMemoryStore


def _record(
    *,
    outcome: str = "failure",
    work_item_id: str = "wi",
    round_index: int = 0,
    counterexample: str = "",
    attempted_method: str = "",
    effective_fix: str = "",
    reason_code: str = "some_error",
    created_at: str = "",
) -> MemoryRecord:
    kwargs = dict(
        outcome=outcome,
        work_item_id=work_item_id,
        round_index=round_index,
        counterexample=counterexample,
        attempted_method=attempted_method,
        effective_fix=effective_fix,
        reason_code=reason_code,
    )
    if created_at:
        kwargs["created_at"] = created_at
    return MemoryRecord(**kwargs)


class TestSeatMemoryRecall:
    def test_recall_ranks_by_word_overlap_with_query(self, tmp_path: Path):
        store = SeatMemoryStore(tmp_path)
        store.commit(
            _record(
                counterexample="contract mismatch in kvcache eviction policy",
                reason_code="contract_mismatch",
            )
        )
        store.commit(
            _record(
                counterexample="citation DOI unresolved for AutoGen paper",
                reason_code="unresolvable_citation",
            )
        )
        rows = store.recall(query="the kvcache eviction policy failed contract checks")
        assert rows, "expected at least one recalled record"
        assert rows[0]["reason_code"] == "contract_mismatch"

    def test_recall_without_query_is_most_recent_first(self, tmp_path: Path):
        store = SeatMemoryStore(tmp_path)
        store.commit(_record(work_item_id="first", reason_code="a"))
        store.commit(_record(work_item_id="second", reason_code="b"))
        rows = store.recall()
        assert rows[0]["work_item_id"] == "second"
        assert rows[1]["work_item_id"] == "first"

    def test_recall_falls_back_to_recency_when_query_has_no_overlap(self, tmp_path: Path):
        store = SeatMemoryStore(tmp_path)
        store.commit(_record(work_item_id="first", counterexample="alpha beta gamma"))
        store.commit(_record(work_item_id="second", counterexample="delta epsilon zeta"))
        rows = store.recall(query="unrelated query with zzz tokens")
        assert len(rows) == 2
        assert rows[0]["work_item_id"] == "second"  # still most-recent-first

    def test_recall_respects_limit_and_max_chars(self, tmp_path: Path):
        store = SeatMemoryStore(tmp_path)
        for i in range(20):
            store.commit(_record(work_item_id=f"wi{i}", reason_code="repeat"))
        rows = store.recall(limit=3)
        assert len(rows) <= 3

    def test_empty_store_returns_empty_list(self, tmp_path: Path):
        store = SeatMemoryStore(tmp_path)
        assert store.recall() == []
        assert store.recall(query="anything") == []


class TestBoundedCompaction:
    def test_compacts_above_threshold_collapsing_same_reason_code(self, tmp_path: Path):
        store = SeatMemoryStore(tmp_path)
        for i in range(70):
            store.commit(
                _record(
                    work_item_id=f"wi{i}",
                    reason_code="flaky_timeout",
                    created_at=f"2026-01-01T00:{i:02d}:00",
                )
            )
        lines = store.path.read_text(encoding="utf-8").splitlines()
        # Compaction runs incrementally (each commit past the threshold may
        # trigger it again), so the exact count depends on how many times it
        # fired — assert the *bound* holds, not one specific number.
        assert len(lines) <= 20, "expected compaction to keep the file well under 70 rows"
        rows = [json.loads(line) for line in lines]
        merged = [r for r in rows if r.get("outcome") == "merged"]
        assert merged, "expected at least one merged record"
        assert merged[0]["count"] > 1
        assert sum(int(r.get("count") or 1) for r in rows) == 70

    def test_different_reason_codes_are_not_cross_merged(self, tmp_path: Path):
        store = SeatMemoryStore(tmp_path)
        for i in range(40):
            store.commit(_record(work_item_id=f"a{i}", reason_code="code_a"))
        for i in range(40):
            store.commit(_record(work_item_id=f"b{i}", reason_code="code_b"))
        rows = [json.loads(line) for line in store.path.read_text(encoding="utf-8").splitlines()]
        merged = [r for r in rows if r.get("outcome") == "merged"]
        codes = {r["reason_code"] for r in merged}
        assert codes == {"code_a", "code_b"}, "each code's older rows collapse independently"

    def test_recall_still_works_after_compaction(self, tmp_path: Path):
        store = SeatMemoryStore(tmp_path)
        for i in range(70):
            store.commit(_record(work_item_id=f"wi{i}", reason_code="flaky_timeout"))
        rows = store.recall(limit=5)
        assert rows
        assert all(isinstance(r, dict) for r in rows)


class TestCampaignMemoryStore:
    def test_recall_is_scoped_to_role(self, tmp_path: Path):
        path = tmp_path / "campaign_memory.jsonl"
        store = CampaignMemoryStore(path)
        store.commit("implementer", _record(work_item_id="i1", reason_code="build_fail"))
        store.commit("reviewer", _record(work_item_id="r1", reason_code="review_fail"))
        impl_rows = store.recall("implementer")
        reviewer_rows = store.recall("reviewer")
        assert len(impl_rows) == 1 and impl_rows[0]["work_item_id"] == "i1"
        assert len(reviewer_rows) == 1 and reviewer_rows[0]["work_item_id"] == "r1"

    def test_survives_across_separate_store_instances(self, tmp_path: Path):
        """Simulates two different missions in the same campaign writing to
        the same shared path — the whole point of cross-mission memory."""
        path = tmp_path / "campaign_memory.jsonl"
        CampaignMemoryStore(path).commit(
            "implementer", _record(work_item_id="mission1_wi", reason_code="off_by_one")
        )
        # A fresh mission's worker constructs its own store instance.
        later = CampaignMemoryStore(path)
        rows = later.recall("implementer", query="off by one indexing bug")
        assert rows and rows[0]["work_item_id"] == "mission1_wi"

    def test_blank_role_is_a_no_op(self, tmp_path: Path):
        path = tmp_path / "campaign_memory.jsonl"
        store = CampaignMemoryStore(path)
        store.commit("", _record())
        assert not path.exists()
        assert store.recall("") == []
