"""Declarative coordination templates (policy layer)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from sciteam.models import DepEdge, DepKind


class TopologyKind(StrEnum):
    CHAIN = "chain"
    ROUTE = "route"
    PARALLEL = "parallel"
    ORCHESTRATE = "orchestrate"
    LOOP = "loop"
    HIERARCHY = "hierarchy"


class SchedulingMode(StrEnum):
    BARRIER = "barrier"
    EAGER = "eager"


class BusyPolicy(StrEnum):
    WAIT = "wait"
    CLONE = "clone"


class UrgentPolicy(StrEnum):
    FORK = "fork"
    PREEMPT = "preempt"


class CommunicationMode(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class StoppingKind(StrEnum):
    JUDGMENT = "judgment"
    VERIFICATION = "verification"


class ReflectionBias(StrEnum):
    NEUTRAL = "neutral"
    CONSERVATIVE = "conservative"
    AGGRESSIVE = "aggressive"


class AggregationKind(StrEnum):
    NONE = ""
    MAJORITY = "majority"
    PAIRWISE_JUDGE = "pairwise_judge"
    ELO_TOURNAMENT = "elo_tournament"
    META_ANALYSIS = "meta_analysis"


MANDATORY_INSTITUTIONS = (
    "I_DESIGN_FEEDBACK",
    "I_PROCESS_AUDIT",
    "I_STAGNATION_HALT",
)

# 2026-08-13 audit finding: MANDATORY_INSTITUTIONS is unconditionally injected
# into every charter (see CoordinationSpec.from_dict below) and serialized
# into every run's team_run.json as "institutions enabled for this run" —
# but only I_STAGNATION_HALT has a real enforcement path (round_assessor's
# stagnation-round check). I_PROCESS_AUDIT (non-production oversight seat +
# veto) and I_DESIGN_FEEDBACK (auto REOPEN_UPSTREAM on downstream failure)
# have no wired trigger anywhere in sciteam/ (ENGINE_ISA.md §... already
# marks both ❌ at L1). Keeping the three names together in `institutions`
# for backward compatibility with existing run archives/analysis scripts,
# but also exposing which ones are actually backed by code so future
# consumers do not mistake the label for delivered governance.
IMPLEMENTED_INSTITUTIONS = ("I_STAGNATION_HALT",)


@dataclass(frozen=True)
class WorkGraphSpec:
    """Institutional work graph.

    - ``edges``: concrete item-id edges (rare at template load time).
    - ``role_pipeline``: ordered role keys compiled to FS edges **within each
      wave** after the coordinator posts that wave's cards. This is how a
      tournament stage graph is expressed as policy, not as an engine enum.
    """

    edges: tuple[DepEdge, ...] = ()
    role_pipeline: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"edges": [e.to_dict() for e in self.edges]}
        if self.role_pipeline:
            out["role_pipeline"] = list(self.role_pipeline)
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> WorkGraphSpec:
        if not data:
            return cls()
        raw_edges = data.get("edges") or []
        if not isinstance(raw_edges, list):
            raise ValueError("coordination.work_graph.edges must be a list")
        edges: list[DepEdge] = []
        for item in raw_edges:
            if not isinstance(item, dict):
                raise ValueError("work_graph edge must be an object")
            edges.append(
                DepEdge(
                    pred=str(item.get("pred") or item.get("from") or ""),
                    succ=str(item.get("succ") or item.get("to") or ""),
                    kind=DepKind(str(item.get("kind") or DepKind.FS.value)),
                )
            )
        raw_pipe = data.get("role_pipeline") or ()
        if isinstance(raw_pipe, str):
            pipe = tuple(p.strip() for p in raw_pipe.split(">") if p.strip())
        elif isinstance(raw_pipe, (list, tuple)):
            pipe = tuple(str(p).strip() for p in raw_pipe if str(p).strip())
        else:
            raise ValueError("coordination.work_graph.role_pipeline must be a list or string")
        return cls(
            edges=tuple(e for e in edges if e.pred and e.succ),
            role_pipeline=pipe,
        )


@dataclass(frozen=True)
class SchedulingSpec:
    mode: SchedulingMode = SchedulingMode.EAGER
    max_parallel: int = 0
    busy_policy: BusyPolicy = BusyPolicy.CLONE
    employment: bool = True
    urgent_policy: UrgentPolicy | None = None

    def resolved_urgent_policy(self) -> UrgentPolicy:
        if self.urgent_policy is not None:
            return self.urgent_policy
        if self.busy_policy == BusyPolicy.CLONE:
            return UrgentPolicy.FORK
        return UrgentPolicy.PREEMPT

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "mode": self.mode.value,
            "max_parallel": self.max_parallel,
            "busy_policy": self.busy_policy.value,
            "employment": self.employment,
        }
        if self.urgent_policy is not None:
            out["urgent_policy"] = self.urgent_policy.value
        return out


@dataclass(frozen=True)
class StoppingSpec:
    kind: StoppingKind = StoppingKind.JUDGMENT
    max_iterations: int = 0
    verifier: str = ""
    max_wall_clock_seconds: int = 0
    max_total_rounds: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "max_iterations": self.max_iterations,
            "verifier": self.verifier,
            "max_wall_clock_seconds": self.max_wall_clock_seconds,
            "max_total_rounds": self.max_total_rounds,
        }


@dataclass(frozen=True)
class CharterSpec:
    standing_goal: str = ""
    wake_on: tuple[str, ...] = ()
    wake_interval_seconds: int = 0
    # Opaque threshold for assessor seats (facts only; engine must not decide on it).
    stagnation_max_rounds: int = 0
    institutions: tuple[str, ...] = MANDATORY_INSTITUTIONS

    def to_dict(self) -> dict[str, Any]:
        return {
            "standing_goal": self.standing_goal,
            "wake_on": list(self.wake_on),
            "wake_interval_seconds": self.wake_interval_seconds,
            "stagnation_max_rounds": self.stagnation_max_rounds,
            "institutions": list(self.institutions),
            # 2026-08-13 audit finding: `institutions` historically listed
            # I_DESIGN_FEEDBACK/I_PROCESS_AUDIT as if "enabled" alongside the
            # only one with real enforcement (I_STAGNATION_HALT). Field kept
            # unchanged for archive compatibility; this new field is the
            # honest subset so downstream readers can tell which labels are
            # backed by code versus aspirational.
            "institutions_implemented": [
                name for name in self.institutions if name in IMPLEMENTED_INSTITUTIONS
            ],
        }

    @property
    def enabled(self) -> bool:
        return bool(self.standing_goal.strip()) or bool(self.wake_on)


@dataclass(frozen=True)
class CoordinationSpec:
    topology: TopologyKind = TopologyKind.ORCHESTRATE
    scheduling: SchedulingSpec = field(default_factory=SchedulingSpec)
    stopping: StoppingSpec = field(default_factory=StoppingSpec)
    reflection_bias: ReflectionBias = ReflectionBias.NEUTRAL
    aggregation: AggregationKind = AggregationKind.NONE
    replicas: dict[str, int] = field(default_factory=dict)
    human_gate: bool = False
    charter: CharterSpec = field(default_factory=CharterSpec)
    communication: CommunicationMode = CommunicationMode.CLOSED
    work_graph: WorkGraphSpec = field(default_factory=WorkGraphSpec)
    max_replicas: int = 4

    def to_dict(self) -> dict[str, Any]:
        return {
            "topology": self.topology.value,
            "scheduling": self.scheduling.to_dict(),
            "stopping": self.stopping.to_dict(),
            "reflection_bias": self.reflection_bias.value,
            "aggregation": self.aggregation.value,
            "replicas": dict(self.replicas),
            "human_gate": self.human_gate,
            "charter": self.charter.to_dict(),
            "communication": self.communication.value,
            "work_graph": self.work_graph.to_dict(),
            "max_replicas": self.max_replicas,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> CoordinationSpec:
        if not data:
            return cls()
        if not isinstance(data, dict):
            raise ValueError("coordination must be an object")
        scheduling_raw = data.get("scheduling") or {}
        stopping_raw = data.get("stopping") or {}
        if not isinstance(scheduling_raw, dict):
            raise ValueError("coordination.scheduling must be an object")
        if not isinstance(stopping_raw, dict):
            raise ValueError("coordination.stopping must be an object")
        replicas_raw = data.get("replicas") or {}
        if not isinstance(replicas_raw, dict):
            raise ValueError("coordination.replicas must be an object")
        replicas: dict[str, int] = {}
        for key, value in replicas_raw.items():
            count = _coerce_int(value, name=f"replicas[{key}]")
            if count < 1:
                raise ValueError(f"coordination.replicas[{key}] must be >= 1")
            replicas[str(key)] = count
        charter_raw = data.get("charter") or {}
        if charter_raw and not isinstance(charter_raw, dict):
            raise ValueError("coordination.charter must be an object")
        wake_raw = (charter_raw or {}).get("wake_on") or ()
        if isinstance(wake_raw, str):
            wake_on = (wake_raw,)
        elif isinstance(wake_raw, (list, tuple)):
            wake_on = tuple(str(item) for item in wake_raw if str(item).strip())
        else:
            raise ValueError("coordination.charter.wake_on must be a string or list")
        declared_institutions = tuple(
            str(item) for item in ((charter_raw or {}).get("institutions") or ()) if str(item)
        )
        institutions = tuple(dict.fromkeys((*MANDATORY_INSTITUTIONS, *declared_institutions)))
        urgent_raw = scheduling_raw.get("urgent_policy")
        urgent_policy = (
            None
            if urgent_raw in (None, "")
            else _parse_enum(
                UrgentPolicy, urgent_raw, "scheduling.urgent_policy", UrgentPolicy.FORK
            )
        )
        employment = scheduling_raw.get("employment")
        if employment is None:
            employment = True
        work_graph_raw = data.get("work_graph")
        if work_graph_raw is not None and not isinstance(work_graph_raw, dict):
            raise ValueError("coordination.work_graph must be an object")
        return cls(
            topology=_parse_enum(
                TopologyKind, data.get("topology"), "topology", TopologyKind.ORCHESTRATE
            ),
            scheduling=SchedulingSpec(
                mode=_parse_enum(
                    SchedulingMode,
                    scheduling_raw.get("mode"),
                    "scheduling.mode",
                    SchedulingMode.EAGER,
                ),
                max_parallel=max(
                    0, _coerce_int(scheduling_raw.get("max_parallel"), name="max_parallel")
                ),
                busy_policy=_parse_enum(
                    BusyPolicy,
                    scheduling_raw.get("busy_policy"),
                    "scheduling.busy_policy",
                    BusyPolicy.CLONE,
                ),
                employment=bool(employment),
                urgent_policy=urgent_policy,
            ),
            stopping=StoppingSpec(
                kind=_parse_enum(
                    StoppingKind, stopping_raw.get("kind"), "stopping.kind", StoppingKind.JUDGMENT
                ),
                max_iterations=max(
                    0, _coerce_int(stopping_raw.get("max_iterations"), name="max_iterations")
                ),
                verifier=str(stopping_raw.get("verifier") or ""),
                max_wall_clock_seconds=max(
                    0,
                    _coerce_int(
                        stopping_raw.get("max_wall_clock_seconds"),
                        name="max_wall_clock_seconds",
                    ),
                ),
                max_total_rounds=max(
                    0,
                    _coerce_int(stopping_raw.get("max_total_rounds"), name="max_total_rounds"),
                ),
            ),
            reflection_bias=_parse_enum(
                ReflectionBias,
                data.get("reflection_bias"),
                "reflection_bias",
                ReflectionBias.NEUTRAL,
            ),
            aggregation=_parse_enum(
                AggregationKind, data.get("aggregation"), "aggregation", AggregationKind.NONE
            ),
            replicas=replicas,
            human_gate=bool(data.get("human_gate", False)),
            charter=CharterSpec(
                standing_goal=str((charter_raw or {}).get("standing_goal") or ""),
                wake_on=wake_on,
                wake_interval_seconds=max(
                    0,
                    _coerce_int(
                        (charter_raw or {}).get("wake_interval_seconds"),
                        name="wake_interval_seconds",
                    ),
                ),
                stagnation_max_rounds=max(
                    0,
                    _coerce_int(
                        (charter_raw or {}).get("stagnation_max_rounds"),
                        name="stagnation_max_rounds",
                    ),
                ),
                institutions=institutions,
            ),
            communication=_parse_enum(
                CommunicationMode,
                data.get("communication"),
                "communication",
                CommunicationMode.CLOSED,
            ),
            work_graph=WorkGraphSpec.from_dict(
                work_graph_raw if isinstance(work_graph_raw, dict) else None
            ),
            max_replicas=max(0, _coerce_int(data.get("max_replicas"), name="max_replicas") or 4),
        )

    @classmethod
    def from_run_metadata(cls, metadata: dict[str, Any] | None) -> CoordinationSpec:
        raw = (metadata or {}).get("coordination")
        try:
            return cls.from_dict(raw if isinstance(raw, dict) else None)
        except ValueError:
            return cls()


def _parse_enum(enum_cls: type[StrEnum], value: Any, name: str, default: Any) -> Any:
    if value is None or value == "":
        return default
    try:
        return enum_cls(str(value))
    except ValueError as exc:
        allowed = ", ".join(item.value or "(empty)" for item in enum_cls)
        raise ValueError(f"invalid coordination.{name}: {value!r} (allowed: {allowed})") from exc


def _coerce_int(value: Any, *, name: str) -> int:
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"coordination.{name} must be an integer") from exc
