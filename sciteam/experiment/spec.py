"""Experiment specs and task sets (docs/60-experiment.md §3.1, §4)."""

import re
from typing import Any

import yaml
from pydantic import BaseModel, Field

from sciteam.kernel.config_store import ConfigStore

VAR = re.compile(r"\{task\.(\w+)\}")


class Arm(BaseModel):
    name: str
    team: str                              # team file relative to configs, may use {task.*}
    overlay: str | None = None             # config overlay dir relative to configs
    # config path -> mapping deep-merged after the task set's patches, for this arm only
    patches: dict[str, dict[str, Any]] = Field(default_factory=dict)


class TaskSpec(BaseModel):
    id: str
    vars: dict[str, str] = Field(default_factory=dict)
    # config path -> mapping deep-merged into that file for this task (e.g. the sandbox image)
    patches: dict[str, dict[str, Any]] = Field(default_factory=dict)


class TaskSet(BaseModel):
    name: str
    grader: str                            # command; {workspace} {out} {task.*}; cwd = repo root
    vars: dict[str, str] = Field(default_factory=dict)            # defaults for every task
    patches: dict[str, dict[str, Any]] = Field(default_factory=dict)
    # name -> command ({task.*}; cwd = repo root) whose output identifies something the
    # interpreter digest cannot see: grader, environment, task seed (ExperimentIdentity)
    identity: dict[str, str] = Field(default_factory=dict)
    tasks: list[TaskSpec]


class OwnerPolicy(BaseModel):
    mode: str                              # "auto-approve": approve whatever awaits the owner
    nudges: int                            # replies to a quiescent run that has not met its goal
    nudge_text: str
    max_rounds: int                        # breaker on the drive loop


class ExperimentSpec(BaseModel):
    name: str
    hypothesis: str
    primary: str
    theta: float | dict[str, float] | None = None   # false_closure threshold; per task id (§5)
    # external success predicate: score >= success (task semantics, not a baseline; §5)
    success: float | dict[str, float] | None = None
    tasks: str                             # task set file relative to configs
    only_tasks: list[str] = Field(default_factory=list)
    repeats: int
    parallel: int
    arms: list[Arm]
    owner: OwnerPolicy
    audit: str                             # command; {run} {out}; cwd = repo root


def load_spec(configs: ConfigStore, path: str) -> tuple[ExperimentSpec, TaskSet]:
    spec = configs.load(path, ExperimentSpec)
    ts = configs.load(spec.tasks, TaskSet)
    if spec.only_tasks:
        known = {t.id for t in ts.tasks}
        missing = sorted(set(spec.only_tasks) - known)
        if missing:
            raise ValueError(f"only_tasks not in {spec.tasks}: {missing}")
        ts = ts.model_copy(update={"tasks": [t for t in ts.tasks if t.id in spec.only_tasks]})
    names = [a.name for a in spec.arms]
    if len(set(names)) != len(names):
        raise ValueError(f"duplicate arm names: {names}")
    return spec, ts


def task_vars(ts: TaskSet, task: TaskSpec) -> dict[str, str]:
    """Task variables; a value may refer to other variables (e.g. a brief naming {task.lib})."""
    tv = {**ts.vars, **task.vars, "id": task.id}
    for _ in range(len(tv) + 1):
        nxt = {k: render(v, tv) for k, v in tv.items()}
        if nxt == tv:
            return tv
        tv = nxt
    raise ValueError(f"task {task.id}: circular task variables")


def render(text: str, tvars: dict[str, str]) -> str:
    def sub(m: re.Match) -> str:
        if m.group(1) not in tvars:
            raise KeyError(f"unknown task variable {m.group(0)}")
        return tvars[m.group(1)]
    return VAR.sub(sub, text)


def render_obj(obj: Any, tvars: dict[str, str]) -> Any:
    """Fill {task.*} in every string of a parsed YAML document (values may hold any text)."""
    if isinstance(obj, str):
        return render(obj, tvars)
    if isinstance(obj, list):
        return [render_obj(x, tvars) for x in obj]
    if isinstance(obj, dict):
        return {k: render_obj(v, tvars) for k, v in obj.items()}
    return obj


def dump(obj: Any) -> str:
    return yaml.safe_dump(obj, sort_keys=False, allow_unicode=True, width=100)


def deep_merge(base: Any, patch: Any) -> Any:
    if isinstance(base, dict) and isinstance(patch, dict):
        out = dict(base)
        for k, v in patch.items():
            out[k] = deep_merge(base.get(k), v) if k in base else v
        return out
    return patch


def patched_text(text: str, patch: dict[str, Any]) -> str:
    return dump(deep_merge(yaml.safe_load(text) or {}, patch))
