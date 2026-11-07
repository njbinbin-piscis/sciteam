"""A lesson one member records is in force for the rest of the project: in every later
briefing, and next to the failing call of the tool it is about."""

from pathlib import Path

import yaml

from sciteam.harness.memory import MemoryBook
from tests.scenarios.test_team_runtime import (TEAM_TOOLS, FnLlm, activations, briefing, fin,
                                               go, human_says, tc, write_configs)
from sciteam.runtime.team import create_run

PLATFORM_NOTE = """---
id: memory_platform1
title: Units
scope: all
---
Report concentrations in micromolar.
"""


def setup(tmp: Path) -> Path:
    cfg = write_configs(tmp)
    h = yaml.safe_load((cfg / "harness/test.yaml").read_text())
    h["tools"] = [*TEAM_TOOLS, "remember", "forget"]
    (cfg / "harness/test.yaml").write_text(yaml.safe_dump(h))
    (cfg / "memory").mkdir()
    (cfg / "memory/memory_platform1.md").write_text(PLATFORM_NOTE)
    run = tmp / "run"
    create_run(run, team="teams/t.yaml", configs_dir=cfg, goal="test goal")
    return run


async def test_a_remembered_lesson_reaches_teammates_and_the_failing_tool(tmp_path):
    run = setup(tmp_path)
    human_says(run, "@dev1", "start")

    seen: list[str] = []

    def again(messages):
        seen.append(messages[-1].content)
        return tc("bash", command="bash -c 'exit 3'")

    def done(messages):
        seen.append(messages[-1].content)
        return fin()

    llms = {"dev1": FnLlm([
        tc("remember", text="The solver converges only with --fast.", title="solver flag"),
        tc("remember", text="Exit 3 from the solver means no input: pass -i with the input file.",
           scope="tool:bash", when=r"exit code: 3"),
        tc("bash", command="bash -c 'exit 3'"), again, done])}
    await go(run, llms)
    first, second = seen
    assert "[exit code: 3]" in first and "[memory] Notes about bash" in first
    assert "pass -i with the input file" in first
    assert "[exit code: 3]" in second and "[memory]" not in second

    book = MemoryBook(run / "memory", run / "configs" / "memory")
    assert {n.author for n in book.notes() if n.origin == "project"} == {"dev1"}

    human_says(run, "@dev2", "your turn")
    mine = next(n for n in book.notes() if n.title == "solver flag")
    llms["dev2"] = FnLlm([tc("forget", id="memory_platform1", reason="this project uses mM"),
                          tc("forget", id=mine.id, reason="fixed upstream"), fin()])
    await go(run, llms)
    b = briefing(run, activations(run, "dev2")[0])
    assert "The solver converges only with --fast." in b
    assert "Report concentrations in micromolar." in b
    assert "notes about bash appear when that tool fails" in b

    ids = {n.id for n in book.notes()}
    assert "memory_platform1" not in ids and mine.id not in ids
    assert {n.id for n in book.forgotten()} == {"memory_platform1", mine.id}
    assert (run / "configs/memory/memory_platform1.md").exists()


async def test_the_owners_correction_is_a_note_from_the_user(tmp_path):
    run = setup(tmp_path)
    book = MemoryBook(run / "memory", run / "configs" / "memory")
    note = book.remember("Plot residuals against substrate, never against time.",
                         author="owner", from_user=True, share=True)
    human_says(run, "@dev1", "go")
    await go(run, {"dev1": FnLlm([fin()])})
    b = briefing(run, activations(run, "dev1")[0])
    assert f"[{note.id} · owner]" in b and "never against time" in b
