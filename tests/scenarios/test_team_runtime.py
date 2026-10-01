import asyncio
import inspect
import shutil
from pathlib import Path

import yaml

from sciteam.comm import Act
from sciteam.kernel.ids import new_id
from sciteam.providers.llm.fakes import TEST_MODEL, call, estimate_tokens
from sciteam.providers.llm.types import AssistantTurn, Usage
from sciteam.runtime.team import RunLocked, TeamRun, create_run, run_team

REPO_CONFIGS = Path(__file__).resolve().parents[2] / "configs"
TEAM_TOOLS = ["bash", "finish", "send_message", "read_conversation", "list_conversations",
              "directory", "create_room", "task_create", "task_transition", "task_update",
              "task_board", "task_show", "schedule_wakeup"]


def tc(name, **args):
    return AssistantTurn(tool_calls=[call(name, **args)], finish_reason="tool_calls")


def fin(outcome="completed", summary="done"):
    return tc("finish", outcome=outcome, summary=summary)


class FnLlm:
    """Steps are turns, exceptions, or (a)sync callables(messages) -> turn; `loop` repeats
    a callable forever once the steps run out."""

    def __init__(self, steps=(), loop=None):
        self.model_info = TEST_MODEL
        self.steps = list(steps)
        self.loop = loop
        self.requests: list[list] = []

    async def complete(self, messages, tools, *, params):
        self.requests.append(list(messages))
        if self.steps:
            step = self.steps.pop(0)
        elif self.loop is not None:
            step = self.loop
        else:
            raise AssertionError("FnLlm ran out of steps")
        if isinstance(step, BaseException):
            raise step
        if callable(step):
            step = step(messages)
            if inspect.isawaitable(step):
                step = await step
        turn = step.model_copy(deep=True)
        pt = estimate_tokens(messages)
        turn.usage = Usage(prompt_tokens=pt, completion_tokens=5, cache_miss_tokens=pt)
        turn.model = "scripted"
        return turn


def write_configs(tmp: Path, *, policy="flat", runtime=None, members=None, tasks=None) -> Path:
    cfg = tmp / "configs"
    shutil.copytree(REPO_CONFIGS / "comm", cfg / "comm")
    (cfg / "prompts").mkdir(parents=True)
    (cfg / "prompts/team.md").write_text("You are a team member.")
    (cfg / "harness").mkdir()
    (cfg / "harness/test.yaml").write_text(yaml.safe_dump({
        "name": "t", "model": "scripted", "system_prompt_ref": "prompts/team.md",
        "tools": TEAM_TOOLS, "recovery": {"empty_turn_retries": 1, "provider_retries": 0},
        "sandbox": {"kind": "local"}}))
    rt = {"max_concurrent_activations": 8, "debounce_s": 0, "memory_recent_activations": 5,
          "briefing_messages_per_conv": 20, "deliver_during_activation": True,
          "rate": {"activations_per_agent_per_hour": 100, "max_consecutive_handoffs": 5,
                   "max_timers_per_agent": 3,
                   "storm": {"window_s": 600, "max_messages": 500,
                             "max_messages_without_progress": 400}},
          "budget": {"cost_usd": 100, "wall_clock_s": 600}, "on_quiescent": "stop",
          "error_retries": 1, "error_backoff_s": [0.1], "sandbox": {"kind": "local"}}
    for k, v in (runtime or {}).items():
        if isinstance(v, dict):
            rt[k] = {**rt[k], **v}
        else:
            rt[k] = v
    (cfg / "runtime").mkdir()
    (cfg / "runtime/test.yaml").write_text(yaml.safe_dump(rt))
    members = members or [
        {"id": "lead", "role": "lead", "team": "core", "harness": "harness/test.yaml"},
        {"id": "dev1", "role": "worker", "team": "core", "manager": "lead",
         "harness": "harness/test.yaml"},
        {"id": "dev2", "role": "worker", "team": "core", "manager": "lead",
         "harness": "harness/test.yaml"},
        {"id": "owner", "kind": "human", "role": "owner"},
    ]
    (cfg / "teams").mkdir()
    (cfg / "teams/t.yaml").write_text(yaml.safe_dump({
        "name": "t", "comm_policy": f"comm/{policy}.yaml", "runtime": "runtime/test.yaml",
        "members": members, "tasks": tasks or []}))
    return cfg


def setup(tmp: Path, **kw) -> Path:
    cfg = write_configs(tmp, **kw)
    run = tmp / "run"
    create_run(run, team="teams/t.yaml", configs_dir=cfg, goal="test goal")
    return run


def human_says(run: Path, to: str, text: str, actor: str = "owner"):
    tr = TeamRun(run, models={"scripted": TEST_MODEL})
    try:
        r = tr.comm.submit(Act(act_id=new_id("act"), actor=actor, kind="post_message",
                               params={"to": to, "text": text}))
        assert r.ok, r.decision
    finally:
        tr.close()


async def go(run: Path, llms: dict[str, FnLlm], timeout: float = 20) -> str:
    def factory(agent, info):
        return llms.setdefault(agent, FnLlm())
    return await asyncio.wait_for(
        run_team(run, llm_factory=factory, models={"scripted": TEST_MODEL}), timeout)


def events(run: Path, type: str | None = None, prefix: str = ""):
    tr = TeamRun(run, models={"scripted": TEST_MODEL})
    try:
        return [e for e in tr.store.read_global(stream_prefix=prefix or None)
                if type is None or e.type == type]
    finally:
        tr.close()


def activations(run: Path, agent: str):
    return events(run, "activation_started", f"agent/{agent}/")


def briefing(run: Path, ev) -> str:
    tr = TeamRun(run, models={"scripted": TEST_MODEL})
    try:
        return tr.blobs.get_text(ev.payload["task_ref"])
    finally:
        tr.close()


# ---- C3: mention wakes exactly the mentioned ------------------------------------------------

async def test_mention_wakes_only_mentioned(tmp_path):
    run = setup(tmp_path)
    human_says(run, "#general", "@dev1 please say hi")
    llms = {"dev1": FnLlm([tc("send_message", to="#general", text="hi all"), fin()])}
    assert await go(run, llms) == "quiescent"
    assert len(activations(run, "dev1")) == 1
    assert activations(run, "dev2") == [] and activations(run, "lead") == []
    b = briefing(run, activations(run, "dev1")[0])
    assert "@dev1 please say hi" in b and "#general" in b
    assert events(run, "run_quiescent")


async def test_resume_after_quiescent_stop_handles_new_messages(tmp_path):
    run = setup(tmp_path)
    human_says(run, "@dev1", "first")
    llms = {"dev1": FnLlm([fin(), fin()])}
    assert await go(run, llms) == "quiescent"
    assert await go(run, llms) == "quiescent"              # nothing new: stops again at once
    assert len(activations(run, "dev1")) == 1
    human_says(run, "@dev1", "second")
    assert await go(run, llms) == "quiescent"
    assert len(activations(run, "dev1")) == 2
    assert len(events(run, "run_resumed")) == 2


async def test_debounce_batches_messages(tmp_path):
    run = setup(tmp_path, runtime={"debounce_s": 0.5})
    for i in range(3):
        human_says(run, "@dev1", f"note {i}")
    llms = {"dev1": FnLlm([fin()])}
    await go(run, llms)
    acts = activations(run, "dev1")
    assert len(acts) == 1
    b = briefing(run, acts[0])
    assert all(f"note {i}" in b for i in range(3))


async def test_message_during_activation_is_delivered_in_place(tmp_path):
    run = setup(tmp_path)
    human_says(run, "@dev1", "start working")

    def interrupt(messages):
        human_says(run, "@dev1", "also check the README")
        return tc("bash", command="echo working")

    def check(messages):
        assert messages[-1].role == "user" and "[Incoming]" in messages[-1].content
        assert "also check the README" in messages[-1].content
        return fin()

    llms = {"dev1": FnLlm([interrupt, check])}
    await go(run, llms)
    assert len(activations(run, "dev1")) == 1
    assert len(events(run, "incoming_added", "agent/dev1/")) == 1


async def test_long_activation_does_not_block_others(tmp_path):
    run = setup(tmp_path)
    human_says(run, "@dev1", "long job")
    human_says(run, "@dev2", "quick job")

    async def slow(messages):
        await asyncio.sleep(1.5)
        return tc("bash", command="echo done")

    def again(messages):
        human_says(run, "@dev2", "one more quick job")
        return fin()

    llms = {"dev1": FnLlm([slow, fin()]), "dev2": FnLlm([again, fin()])}
    await go(run, llms)
    fins = {a: [e.ts for e in events(run, "activation_finished", f"agent/{a}/")]
            for a in ("dev1", "dev2")}
    assert len(fins["dev2"]) == 2 and len(fins["dev1"]) == 1
    assert max(fins["dev2"]) < fins["dev1"][0]


async def test_concurrency_cap(tmp_path):
    members = [{"id": f"a{i}", "harness": "harness/test.yaml"} for i in range(4)]
    members.append({"id": "owner", "kind": "human"})
    run = setup(tmp_path, runtime={"max_concurrent_activations": 2}, members=members)
    live, peak = [0], [0]

    async def work(messages):
        live[0] += 1
        peak[0] = max(peak[0], live[0])
        await asyncio.sleep(0.3)
        live[0] -= 1
        return fin()

    for i in range(4):
        human_says(run, f"@a{i}", "go")
    llms = {f"a{i}": FnLlm([work]) for i in range(4)}
    await go(run, llms)
    assert peak[0] == 2
    assert all(len(activations(run, f"a{i}")) == 1 for i in range(4))


# ---- tasks and dependencies wake people ---------------------------------------------------

async def test_dependency_readiness_wakes_assignee(tmp_path):
    tasks = [{"key": "a", "by": "owner", "title": "A", "assignee": "dev1"},
             {"key": "b", "by": "owner", "title": "B", "assignee": "dev2", "deps": ["a"]}]
    run = setup(tmp_path, policy="workflow", tasks=tasks)

    llms = {
        "dev1": FnLlm([tc("task_transition", task="T1", transition="start"),
                       tc("task_transition", task="T1", transition="submit", note="A done"),
                       fin()]),
        "dev2": FnLlm([tc("task_transition", task="T2", transition="start"),
                       tc("task_transition", task="T2", transition="submit"), fin()]),
    }
    assert await go(run, llms) == "quiescent"
    tr = TeamRun(run, models={"scripted": TEST_MODEL})
    try:
        assert {t.id: t.status for t in tr.comm.view.tasks.values()} == \
            {"T1": "done", "T2": "done"}
    finally:
        tr.close()
    acts = activations(run, "dev2")
    assert len(acts) == 1  # the unready assignment does not wake; readiness does
    b = briefing(run, acts[0])
    assert "was assigned to you" in b and "unblocked" in b


async def test_workflow_policy_denies_peer_messages(tmp_path):
    tasks = [{"key": "a", "by": "owner", "title": "A", "assignee": "dev1"}]
    run = setup(tmp_path, policy="workflow", tasks=tasks)

    def check(messages):
        assert "denied" in messages[-1].content
        return fin()

    llms = {"dev1": FnLlm([tc("send_message", to="@dev2", text="hey"), check])}
    await go(run, llms)
    assert activations(run, "dev2") == []
    decided = [e.payload for e in events(run, "act_decided")]
    assert decided[-1]["decision"] == "deny"


# ---- outcomes -----------------------------------------------------------------------------

async def test_handoff_continues_and_error_faults(tmp_path):
    run = setup(tmp_path)
    human_says(run, "@dev1", "big job")
    human_says(run, "@dev2", "doomed job")
    text = AssistantTurn(content="thinking out loud")
    llms = {"dev1": FnLlm([fin("handoff", "half done"), fin()]),
            "dev2": FnLlm([text, text, text, text])}
    assert await go(run, llms) == "quiescent"
    acts1 = activations(run, "dev1")
    assert len(acts1) == 2 and "half done" in briefing(run, acts1[1])
    outcomes = [e.payload["reason"] for e in events(run, "activation_finished", "agent/dev2/")]
    assert outcomes == ["no_finish", "no_finish"]  # one retry, then faulted
    assert events(run, "run_quiescent")[-1].payload["faulted"] == ["dev2"]


async def test_timer_keeps_run_alive_and_wakes(tmp_path):
    run = setup(tmp_path, tasks=[{"key": "a", "by": "owner", "title": "A",
                                  "assignee": "dev1"}])
    llms = {"dev1": FnLlm([tc("schedule_wakeup", after_s=1, note="recheck"), fin(),
                           fin()])}
    await go(run, llms)
    acts = activations(run, "dev1")
    assert len(acts) == 2 and "wake-up fired" in briefing(run, acts[1])


async def test_stale_timer_does_not_hold_finished_run(tmp_path):
    run = setup(tmp_path, tasks=[{"key": "a", "by": "owner", "title": "A",
                                  "assignee": "dev1"}])
    llms = {"dev1": FnLlm([tc("schedule_wakeup", after_s=300, note="recheck A"),
                           tc("task_transition", task="T1", transition="start"),
                           tc("task_transition", task="T1", transition="submit"), fin()])}
    assert await go(run, llms, timeout=10) == "quiescent"
    assert len(activations(run, "dev1")) == 1 and not events(run, "timer_fired")


async def test_second_scheduler_on_same_run_is_refused(tmp_path):
    run = setup(tmp_path)
    human_says(run, "@dev1", "work")
    gate = asyncio.Event()

    async def slow(messages):
        await gate.wait()
        return fin()

    first = asyncio.create_task(go(run, {"dev1": FnLlm([slow])}))
    while not activations(run, "dev1"):
        await asyncio.sleep(0.05)
    try:
        await go(run, {})
        raise AssertionError("second scheduler was allowed")
    except RunLocked:
        pass
    gate.set()
    assert await first == "quiescent"


async def test_storm_breaker(tmp_path):
    # The preset task is progress at the start of the window; chatter after it still counts.
    run = setup(tmp_path, runtime={"rate": {"storm": {
        "window_s": 600, "max_messages": 500, "max_messages_without_progress": 6}}},
        tasks=[{"key": "a", "by": "owner", "title": "A"}])
    human_says(run, "@dev1", "talk to dev2")

    def pingpong(peer):
        def step(messages):
            if messages[-1].role == "tool":
                return fin()
            return tc("send_message", to=f"@{peer}", text="ping")
        return step

    llms = {"dev1": FnLlm(loop=pingpong("dev2")), "dev2": FnLlm(loop=pingpong("dev1"))}
    assert await go(run, llms) == "breaker:storm"
    assert events(run, "breaker_tripped")[0].payload["kind"] == "storm"


# ---- crash recovery -----------------------------------------------------------------------

async def test_resume_unfinished_activation_after_crash(tmp_path):
    run = setup(tmp_path)
    human_says(run, "@dev1", "work")

    class Crash(BaseException):
        """Stands in for the process dying: nothing records the end of the activation."""

    crashing = FnLlm([tc("bash", command="echo step1 > s.txt"), Crash()])
    tr = TeamRun(run, llm_factory=lambda a, i: crashing, models={"scripted": TEST_MODEL})
    sched = tr.scheduler()
    sched.catch_up()
    seqs = [i.seq for i in sched.agents["dev1"].pending]
    try:
        await sched.runner.run("dev1", "act_crashed", "work", seqs, sched._poll_for("dev1"))
    except Crash:
        pass
    await sched.runner.close()
    tr.close()
    assert activations(run, "dev1") and not events(run, "activation_finished", "agent/dev1/")

    llms = {"dev1": FnLlm([fin()])}
    assert await go(run, llms) == "quiescent"
    assert len(activations(run, "dev1")) == 1          # resumed, not re-delivered
    assert events(run, "activation_resumed", "agent/dev1/")
    fin_ev = events(run, "activation_finished", "agent/dev1/")
    assert [e.payload["outcome"] for e in fin_ev] == ["completed"]
    assert (run / "workspace/s.txt").read_text().strip() == "step1"
