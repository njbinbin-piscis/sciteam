from sciteam.experiment.protocol_metrics import arm_table, interval, run_summary
from sciteam.experiment.rounds import (ARMS, GateConfig, RoundCallbacks, Update, gate,
                                       run_protocol, snapshot_hash)

SPLITS = {"source": [f"s{i}" for i in range(6)], "validation": ["v0", "v1", "v2", "v3"],
          "replay": ["s0", "s1"], "heldout": ["h0", "h1", "h2", "h3"], "ood": ["o0", "o1"]}


def _cb(good=True, used=True):
    """A snapshot containing 'good.md' lifts every non-source task by 1."""
    def score(task, snap):
        return 1.0 if "good.md" in snap or "rule.md" in snap else 0.0

    def runner(task, snap):
        return {"score": score(task, snap), "used": ["u"] if used else [], "cost": 2.0}

    def propose(task, result, snap):
        if task != "s0" and "u" in (snap.get("have") or ""):
            return None
        return Update("u", "skill", {"good.md": "x"} if good else {"bad.md": "x"})

    return RoundCallbacks(runner=runner, propose=propose,
                          replay=lambda u, t, s: {"reproduced": good, "used": used}, score=score)


def test_frozen_arm_never_changes_snapshot():
    r = run_protocol(SPLITS, ARMS["frozen"], 1, _cb())
    assert r.final_snapshot == {} and all(e["decision"] == "none" for e in r.ledger)


def test_good_update_is_promoted_and_hash_chain_links():
    r = run_protocol(SPLITS, ARMS["skills"], 1, _cb())
    assert any(e["decision"] == "promoted" for e in r.ledger)
    for a, b in zip(r.ledger, r.ledger[1:], strict=False):
        assert a["snapshot_after"] == b["snapshot"]
    assert r.heldout_last["h0"] == 1.0 and r.heldout_first["h0"] == 0.0
    assert r.final_snapshot and snapshot_hash(r.final_snapshot) == r.ledger[-1]["snapshot_after"]


def test_same_seed_same_snapshot_sequence_and_other_seed_other_order():
    a = run_protocol(SPLITS, ARMS["skills"], 7, _cb())
    b = run_protocol(SPLITS, ARMS["skills"], 7, _cb())
    c = run_protocol(SPLITS, ARMS["skills"], 8, _cb())
    assert [e["snapshot_after"] for e in a.ledger] == [e["snapshot_after"] for e in b.ledger]
    assert [e["task"] for e in a.ledger] != [e["task"] for e in c.ledger]


def test_arm_filters_update_kind():
    r = run_protocol(SPLITS, ARMS["rules"], 1, _cb())
    assert r.final_snapshot == {}


def _g(**kw):
    return gate(Update("u", "skill", {}),
                replay=kw.pop("replay", lambda: {"reproduced": True, "used": True}),
                before=kw.pop("before", lambda: [0, 0, 0, 0]),
                after=kw.pop("after", lambda: [1, 1, 0, 0]), cfg=kw.pop("cfg", GateConfig()))


def test_gate_boundaries():
    assert _g() == (True, "promoted")
    assert _g(replay=lambda: {"reproduced": True, "used": False}) == (False, "not_used")
    assert _g(replay=lambda: {"reproduced": False, "used": True}) == \
        (False, "replay_not_reproduced")
    assert _g(after=lambda: [1, 0, 0, 0]) == (False, "validation_not_improved")
    assert _g(before=lambda: [1, 1, 1, 1], after=lambda: [2, 2, 0, 0])[1] == "validation_regressed"
    assert _g(before=lambda: [1, 1, 1, 0], after=lambda: [2, 2, 0, 0])[0] is True


def test_ablations_switch_checks_off():
    assert _g(replay=lambda: {"reproduced": True, "used": False},
              cfg=GateConfig(usage=False))[0] is True
    assert _g(replay=lambda: {"reproduced": False, "used": True},
              cfg=GateConfig(replay=False))[0] is True
    assert _g(after=lambda: [0, 0, 0, 0], cfg=GateConfig(validation=False))[0] is True


def test_metrics_match_hand_computation():
    r = run_protocol(SPLITS, ARMS["skills"], 1, _cb())
    s = run_summary(r)
    assert s["gain"] == 1.0 and s["heldout_macro"] == 1.0 and s["negative_transfer"] == 0.0
    assert s["cost"] == 12.0 and s["cost_per_gain"] == 12.0 and s["forgetting"] == 0.0


def test_interval_needs_three_orders():
    assert interval([1.0, 2.0]) is None
    lo, hi = interval([1.0, 2.0, 3.0])
    assert 1.0 <= lo <= hi <= 3.0
    rs = [run_protocol(SPLITS, ARMS["skills"], s, _cb()) for s in (1, 2, 3)]
    assert arm_table(rs)["skills"]["heldout_macro"]["ci"] is not None


def test_report_table_is_byte_stable(tmp_path):
    from sciteam.experiment.protocol_report import load, table
    for arm in ("frozen", "skills"):
        for s in (1, 2, 3):
            run_protocol(SPLITS, ARMS[arm], s, _cb(), out=tmp_path)
    a, b = table(load(tmp_path)), table(load(tmp_path))
    assert a == b and a.index("frozen") < a.index("skills") and "[" in a
