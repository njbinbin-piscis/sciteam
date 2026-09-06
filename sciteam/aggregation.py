"""Deterministic aggregation helpers.

2026-08-13 audit finding: MAJORITY/ELO_TOURNAMENT/PAIRWISE_JUDGE do not judge
semantic content at all — they rank free-text task results by exact-match
vote counting or lexicographic (``>=``) string comparison. On genuinely
free-text output every "vote"/"comparison" is close to a coin flip dressed
up as a score (confirmed against real debate_elo runs: a purely-procedural
seat that never produces hypothesis content was ranked #1 by ELO_TOURNAMENT
purely because its result string happened to sort last). This is disclosed
in the Line A draft's limitations section for the paradigms it exercises
there ("Elo/pairwise judges are stubs"); any Line E/F material that cites a
`ranking`/`winner` value from this module as evidence of *quality* rather
than *process completion* must carry the same disclosure, and ideally should
be re-derived from an actual qualitative judge (e.g. round_assessor) instead.
`apply_aggregation()`'s output is decorative — written only to
`run.metadata["aggregation"]` for display/archival — round progression and
task completion are decided independently by `Coordinator.assess_round()`,
so this stub does not itself gate anything.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from sciteam.coordination import AggregationKind
from sciteam.models import Task, TaskState, TeamRun


def _tasks_for_round(run: TeamRun) -> list[Task]:
    return [t for t in run.tasks if t.round_index == run.active_round and t.state == TaskState.DONE]


def _token(task: Task) -> str:
    text = (task.result or "").strip()
    if not text and task.facts:
        text = " | ".join(str(x) for x in task.facts)
    return text or f"(empty:{task.agent_key})"


def apply_aggregation(run: TeamRun, kind: AggregationKind) -> dict[str, Any] | None:
    if kind == AggregationKind.NONE:
        return None
    tasks = _tasks_for_round(run)
    if not tasks:
        return None
    if kind == AggregationKind.MAJORITY:
        counts = Counter(_token(t) for t in tasks)
        winner, votes = counts.most_common(1)[0]
        result: dict[str, Any] = {
            "kind": kind.value,
            "winner": winner,
            "votes": votes,
            "tally": dict(counts),
            "inputs": len(tasks),
            # exact-string-match voting: on free-text results with no repeat
            "semantic": False,
        }
    elif kind == AggregationKind.ELO_TOURNAMENT:
        ratings = {t.agent_key: 1200.0 for t in tasks}
        ordered = sorted(tasks, key=lambda t: t.agent_key)
        for i, left in enumerate(ordered):
            for right in ordered[i + 1 :]:
                ra, rb = ratings[left.agent_key], ratings[right.agent_key]
                ea = 1.0 / (1.0 + 10 ** ((rb - ra) / 400.0))
                left_wins = _token(left) >= _token(right)
                sa = 1.0 if left_wins else 0.0
                ratings[left.agent_key] = ra + 24.0 * (sa - ea)
                ratings[right.agent_key] = rb + 24.0 * ((1.0 - sa) - (1.0 - ea))
        ranking = sorted(ratings.items(), key=lambda x: (-x[1], x[0]))
        result = {
            "kind": kind.value,
            "ratings": {k: round(v, 2) for k, v in ratings.items()},
            "ranking": [k for k, _ in ranking],
            "inputs": len(tasks),
            # "win" = lexicographic string comparison of result text, not a
            # judged quality comparison — see module docstring.
            "semantic": False,
        }
    elif kind == AggregationKind.META_ANALYSIS:
        by_agent = {
            t.agent_key: {"result": (t.result or "")[:500], "facts": list(t.facts)} for t in tasks
        }
        facts: list[str] = []
        for t in tasks:
            facts.extend(t.facts or ([_token(t)] if t.result else []))
        result = {"kind": kind.value, "facts": facts, "by_agent": by_agent, "inputs": len(tasks)}
    else:
        # pairwise stub
        scores = {t.agent_key: 0.0 for t in tasks}
        ranked = sorted(tasks, key=lambda t: (_token(t), t.agent_key))
        for i, left in enumerate(ranked):
            for right in ranked[i + 1 :]:
                if _token(left) >= _token(right):
                    scores[left.agent_key] += 1.0
                else:
                    scores[right.agent_key] += 1.0
        ordered = sorted(scores.items(), key=lambda x: (-x[1], x[0]))
        result = {
            "kind": AggregationKind.PAIRWISE_JUDGE.value,
            "scores": scores,
            "ranking": [k for k, _ in ordered],
            "inputs": len(tasks),
            # pairwise stub: lexicographic string comparison, not an LLM judge.
            "semantic": False,
        }
    result["round_index"] = run.active_round
    run.metadata["aggregation"] = result
    return result
