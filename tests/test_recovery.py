"""docs/28 阶段4 — cold-start recovery for Team/Mission state.

`TeamOrchestrator` keeps `TeamRun`/`WorkBoard`/`DepGraph` in process memory.
A process kill + restart must be able to reconstruct that exact state from
`sciteam.persist.snapshot_team_run`'s on-disk snapshot, and must never
re-decide (re-claim, re-run) a work item the snapshot already recorded as
terminal.
"""

from __future__ import annotations

import uuid

import pytest
from sciteam.depgraph import DepEdge, DepKind
from sciteam.models import TeamRunState, WorkItem, WorkItemState
from sciteam.orchestrator import CreateSpec, TeamOrchestrator
from sciteam.persist import snapshot_team_run
from sciteam.recovery import RecoveryError, load_team_run_snapshot
from sciteam.runtime import AgentRunResult, Run, RunSpec, RunState


class _RT:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def run_subagent(
        self, *, agent_id, task, work_dir, parent_run_id=None, depth=0, context=None
    ):
        self.calls.append((context or {}).get("team_agent_key") or agent_id)
        run = Run(
            run_id=uuid.uuid4().hex[:8],
            spec=RunSpec(kind="sub", input=task, agent_id=agent_id),
            state=RunState.SUCCEEDED,
            output="ok",
        )
        return AgentRunResult(run=run, output=run.output)


def _seed_run(tmp_path):
    orch = TeamOrchestrator(runtime=_RT(), work_root=tmp_path)
    run = orch.create_run(
        CreateSpec(
            team_profile_id="t",
            goal="cold-start goal",
            agents=[{"agent_key": "a"}, {"agent_key": "b"}],
            coordination={"scheduling": {"mode": "eager", "max_parallel": 2}},
        )
    )
    board = orch.board_for(run.id)
    board.post(WorkItem(id="w1", prompt="finished work", role_tags=["a"]))
    board.post(WorkItem(id="w2", prompt="still pending work", role_tags=["b"]))
    board.claim("w1", "a")
    board.mark_running("w1")
    board.complete("w1", result="the answer")

    deps = orch.deps_for(run.id)
    deps.add_edge(DepEdge(pred="w1", succ="w2", kind=DepKind.FS))

    run.state = TeamRunState.RUNNING
    run.active_round = 3
    run.metadata["custom_flag"] = "kept-through-recovery"
    orch._store.save(run)
    snapshot_team_run(run, board=board, deps=deps)
    return orch, run


def test_cold_resume_restores_run_board_and_deps(tmp_path):
    orch1, run = _seed_run(tmp_path)

    orch2 = TeamOrchestrator(runtime=_RT(), work_root=tmp_path)
    recovered_run = orch2.cold_resume(run.id)

    assert recovered_run.state == TeamRunState.RUNNING
    assert recovered_run.active_round == 3
    assert recovered_run.metadata.get("custom_flag") == "kept-through-recovery"
    assert {a.agent_key for a in recovered_run.agents} == {"a", "b"}

    board2 = orch2.board_for(run.id)
    by_id = {i.id: i for i in board2.all_items()}
    assert by_id["w1"].state == WorkItemState.DONE
    assert by_id["w1"].result == "the answer"
    assert by_id["w2"].state == WorkItemState.READY

    deps2 = orch2.deps_for(run.id)
    assert any(e.pred == "w1" and e.succ == "w2" for e in deps2.edges)

    # `require()` on the fresh orchestrator now sees the recovered run
    # without ever having called `create_run` on it.
    assert orch2.require(run.id).id == run.id


def test_cold_resume_never_reclaims_a_terminal_item(tmp_path):
    orch1, run = _seed_run(tmp_path)
    rt2 = _RT()
    orch2 = TeamOrchestrator(runtime=rt2, work_root=tmp_path)
    orch2.cold_resume(run.id)

    board2 = orch2.board_for(run.id)
    # Attempting to claim the already-DONE item must fail exactly as it
    # would in the original live process (WorkBoard._transit only allows
    # READY -> CLAIMED).
    assert board2.claim("w1", "b") is None
    assert board2.get("w1").state == WorkItemState.DONE
    assert board2.get("w1").result == "the answer"


def test_resumable_reports_snapshot_presence(tmp_path):
    orch1, run = _seed_run(tmp_path)
    orch2 = TeamOrchestrator(runtime=_RT(), work_root=tmp_path)
    assert orch2.resumable(run.id) is True
    assert orch2.resumable("no-such-run") is False


def test_load_team_run_snapshot_missing_raises():
    with pytest.raises(RecoveryError):
        load_team_run_snapshot("/nonexistent/work_root", "missing-run")


def test_create_or_resume_run_resumes_when_snapshot_exists(tmp_path):
    """docs/28 阶段4 (E7 follow-up): a caller that supplies its own stable
    `team_run_id` and re-invokes after a crash must get the *same* run,
    with its prior board/deps progress intact — not a second, blank one."""
    orch1, run = _seed_run(tmp_path)

    orch2 = TeamOrchestrator(runtime=_RT(), work_root=tmp_path)
    resumed_run, resumed = orch2.create_or_resume_run(
        CreateSpec(
            team_profile_id="t",
            goal="a different goal string — must be ignored on resume",
            agents=[{"agent_key": "a"}, {"agent_key": "b"}],
            team_run_id=run.id,
        )
    )

    assert resumed is True
    assert resumed_run.id == run.id
    assert resumed_run.active_round == 3
    assert resumed_run.metadata.get("custom_flag") == "kept-through-recovery"
    by_id = {i.id: i for i in orch2.board_for(run.id).all_items()}
    assert by_id["w1"].state == WorkItemState.DONE


def test_create_or_resume_run_creates_when_no_snapshot(tmp_path):
    orch = TeamOrchestrator(runtime=_RT(), work_root=tmp_path)
    run, resumed = orch.create_or_resume_run(
        CreateSpec(
            team_profile_id="t",
            goal="fresh goal",
            agents=[{"agent_key": "a"}],
            team_run_id="stable-id-1",
        )
    )
    assert resumed is False
    assert run.id == "stable-id-1"
    assert run.state == TeamRunState.PENDING


def test_create_or_resume_run_requires_explicit_id():
    orch = TeamOrchestrator(runtime=_RT(), work_root="/tmp/unused")
    with pytest.raises(ValueError):
        orch.create_or_resume_run(
            CreateSpec(team_profile_id="t", goal="g", agents=[{"agent_key": "a"}])
        )


def test_create_run_rejects_colliding_explicit_id(tmp_path):
    orch = TeamOrchestrator(runtime=_RT(), work_root=tmp_path)
    spec = CreateSpec(
        team_profile_id="t",
        goal="g",
        agents=[{"agent_key": "a"}],
        team_run_id="dup-id",
    )
    orch.create_run(spec)
    with pytest.raises(ValueError):
        orch.create_run(spec)


def test_team_run_to_dict_from_dict_roundtrip(tmp_path):
    _, run = _seed_run(tmp_path)
    restored = type(run).from_dict(run.to_dict())
    assert restored.id == run.id
    assert restored.state == run.state
    assert restored.active_round == run.active_round
    assert restored.metadata == run.metadata
    assert [a.agent_key for a in restored.agents] == [a.agent_key for a in run.agents]
