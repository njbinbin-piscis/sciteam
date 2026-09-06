"""LLM worker: envelope parsing, prompt assembly, artifact duty, compaction."""

from __future__ import annotations

import json

from sciteam import LlmWorkerRuntime, PromptAssets, build_working_pack
from sciteam.compact import DEFAULT_PROFILE
from sciteam.llm import LlmResponse
from sciteam.llm_worker import _extract_json, _role_key, sanitize_artifact_payload

from tests.conftest import LAB_ROOT


class FakeLlmClient:
    """Returns queued responses; records every prompt it received."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self._last_response = ""
        self.requests: list[list[dict]] = []
        self.request_kwargs: list[dict] = []
        from sciteam.llm import LlmUsage

        self.usage = LlmUsage()

    async def acomplete(self, messages, *, temperature=0.7, max_tokens=4096, **kwargs):
        del temperature, max_tokens
        self.requests.append(messages)
        self.request_kwargs.append(kwargs)
        if kwargs.get("response_format") and self._last_response:
            content = self._last_response
        elif self._responses:
            content = self._responses.pop(0)
            self._last_response = content
        else:
            raise AssertionError("no queued responses left")
        self.usage.add({"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
        return LlmResponse(content=content, usage={"total_tokens": 15})


def _assets() -> PromptAssets:
    return PromptAssets(
        prompts_dir=LAB_ROOT / "assets" / "prompts",
        skills_dir=LAB_ROOT / "assets" / "skills",
    )


def _runtime(responses: list[str]) -> tuple[LlmWorkerRuntime, FakeLlmClient]:
    client = FakeLlmClient(responses)
    runtime = LlmWorkerRuntime(
        client=client,
        assets=_assets(),
        schemas_dir=LAB_ROOT / "schemas",
    )
    return runtime, client


def _context(tmp_path, agent_key: str, *, emit_roles: list[str] | None = None) -> dict:
    return {
        "team_run_id": "run1",
        "team_agent_key": agent_key,
        "team_metadata": {
            "mission_id": "m_test",
            "exit_contract": "candidate_hypotheses",
            "skills_allowlist": ["hypothesis.formulate", "lit.citation_guard"],
            "artifact_path": str(tmp_path / "artifact.json"),
            # Duty comes from paradigm metadata, never a worker-side role list.
            "artifact_emit_roles": list(emit_roles or []),
        },
        "round_index": 1,
    }


class TestEnvelopeParsing:
    def test_plain_json(self):
        assert _extract_json('{"a": 1}') == {"a": 1}

    def test_fenced_json(self):
        assert _extract_json('```json\n{"a": 1}\n```') == {"a": 1}

    def test_embedded_json(self):
        assert _extract_json('noise {"a": 1} trailing') == {"a": 1}

    def test_garbage_returns_none(self):
        assert _extract_json("not json at all") is None

    def test_role_key_strips_replica_suffix(self):
        assert _role_key("debater_2") == "debater"
        assert _role_key("assembler_2") == "assembler"
        assert _role_key("judge") == "judge"


class TestWorker:
    def test_implementer_delivery_protocol_has_one_source_of_truth(self):
        prompt = _assets().role_prompt("implementer")
        assert "workspace `write`/`edit`" in prompt
        assert "set `files` to `null`" in prompt
        assert "through `files`" not in prompt

    async def test_final_envelope_requests_json_object_transport(self, tmp_path):
        runtime, client = _runtime([json.dumps({"summary": "ok", "facts": [], "artifact": None})])
        await runtime._complete_with_tools(
            [{"role": "user", "content": "return an envelope"}],
            tool_definitions=[],
            tool_context={"work_dir": str(tmp_path)},
            tool_trace=[],
            inner_loop=[],
        )
        assert client.request_kwargs == [{"response_format": {"type": "json_object"}}]

    async def test_prompt_carries_constitution_role_skills_contract(self, tmp_path):
        runtime, client = _runtime([json.dumps({"summary": "ok", "facts": [], "artifact": None})])
        await runtime.run_subagent(
            agent_id="worker",
            task="mission brief text",
            work_dir=str(tmp_path),
            context=_context(tmp_path, "debater_2"),
        )
        messages = client.requests[0]
        system = messages[0]["content"]
        user = messages[1]["content"]
        assert "Research team constitution" in system
        assert "Role: debater" in system  # replica resolves to base role prompt
        assert "hypothesis.formulate" in system  # skill pack injected
        assert "mission brief text" in user
        # Non-emit seats see the contract name, not a full inlined schema dump.
        assert "candidate_hypotheses" in user

    async def test_artifact_written_only_by_artifact_role(self, tmp_path):
        artifact = {
            "problem_id": "p",
            "candidates": [
                {
                    "hypothesis_id": "h1",
                    "statement": "x" * 25,
                    "falsification_criterion": "z" * 15,
                    "rationale": "y" * 15,
                }
            ],
        }
        envelope = json.dumps({"summary": "s", "facts": [], "artifact": artifact})
        # debater is not an artifact role — must REJECT, not silently drop
        runtime, _ = _runtime([envelope])
        denied = await runtime.run_subagent(
            agent_id="worker",
            task="t",
            work_dir=str(tmp_path),
            context=_context(tmp_path, "debater_1"),
        )
        assert not (tmp_path / "artifact.json").exists()
        assert "REJECTED" in denied.output
        # judge writes only when paradigm metadata declares the seat
        runtime, _ = _runtime([envelope])
        result = await runtime.run_subagent(
            agent_id="worker",
            task="t",
            work_dir=str(tmp_path),
            context=_context(tmp_path, "judge", emit_roles=["judge"]),
        )
        written = json.loads((tmp_path / "artifact.json").read_text())
        assert written["problem_id"] == "p"
        assert written["mission_id"] == "m_test"  # injected provenance
        assert "artifact written" in result.output

    async def test_paradigm_declared_seat_writes_and_sanitizes(self, tmp_path):
        artifact = {
            "problem_id": "p1",
            "candidates": [
                {
                    "hypothesis_id": "H1",
                    "statement": "x" * 25,
                    "falsification_criterion": "fail if y is not observed",
                    "rationale": "because evidence",
                    "smoke_check": None,
                    "_meta_review_notes": {"secret": True},
                }
            ],
        }
        envelope = json.dumps({"summary": "ready", "facts": [], "artifact": artifact})
        runtime, _ = _runtime([envelope])
        result = await runtime.run_subagent(
            agent_id="worker",
            task="t",
            work_dir=str(tmp_path),
            context=_context(tmp_path, "meta_review", emit_roles=["meta_review"]),
        )
        written = json.loads((tmp_path / "artifact.json").read_text())
        assert written["mission_id"] == "m_test"
        assert "_meta_review_notes" not in written["candidates"][0]
        assert "smoke_check" not in written["candidates"][0]
        assert "artifact written" in result.output

    async def test_undeclared_seat_cannot_write_even_if_named_judge(self, tmp_path):
        """No worker-side whitelist: without metadata duty, judge cannot emit."""
        artifact = {"problem_id": "p", "candidates": []}
        envelope = json.dumps({"summary": "s", "facts": [], "artifact": artifact})
        runtime, _ = _runtime([envelope])
        result = await runtime.run_subagent(
            agent_id="worker",
            task="t",
            work_dir=str(tmp_path),
            context=_context(tmp_path, "judge", emit_roles=[]),
        )
        assert not (tmp_path / "artifact.json").exists()
        assert "REJECTED" in result.output

    def test_sanitize_drops_private_and_nulls(self):
        cleaned = sanitize_artifact_payload(
            {"a": 1, "_note": "x", "b": None, "c": {"d": None, "_e": 2, "f": 3}}
        )
        assert cleaned == {"a": 1, "c": {"f": 3}}

    async def test_json_repair_retry(self, tmp_path):
        good = json.dumps({"summary": "repaired", "facts": [], "artifact": None})
        runtime, client = _runtime(["definitely not json", good])
        result = await runtime.run_subagent(
            agent_id="worker",
            task="t",
            work_dir=str(tmp_path),
            context=_context(tmp_path, "judge"),
        )
        assert result.output.startswith("repaired")
        assert len(client.requests) == 3  # final JSON transport + repair round-trip happened

    async def test_unrepairable_returns_recoverable(self, tmp_path):
        runtime, _ = _runtime(["garbage", "still garbage"])
        result = await runtime.run_subagent(
            agent_id="worker",
            task="t",
            work_dir=str(tmp_path),
            context=_context(tmp_path, "judge"),
        )
        assert result.outcome.value == "recoverable"
        assert result.reason_code == "invalid_json_envelope"

    async def test_verifier_owned_artifact_rejected(self, tmp_path):
        artifact = {"problem_id": "p", "success": True}
        envelope = json.dumps({"summary": "s", "facts": [], "artifact": artifact})
        runtime, _ = _runtime([envelope])
        ctx = _context(tmp_path, "evaluator")
        ctx["team_metadata"]["artifact_source"] = "verifier"
        result = await runtime.run_subagent(
            agent_id="worker", task="t", work_dir=str(tmp_path), context=ctx
        )
        assert not (tmp_path / "artifact.json").exists()
        assert "REJECTED" in result.output

    async def test_files_written_to_mission_dir(self, tmp_path):
        envelope = json.dumps(
            {
                "summary": "code ready",
                "facts": [],
                "artifact": None,
                "files": {"candidate.py": "NAME = 'x'\n", "../escape.py": "bad"},
            }
        )
        runtime, _ = _runtime([envelope])
        result = await runtime.run_subagent(
            agent_id="worker", task="t", work_dir=str(tmp_path), context=_context(tmp_path, "implementer")
        )
        # artifact_path parent is the mission dir (tmp_path here)
        assert (tmp_path / "candidate.py").read_text() == "NAME = 'x'\n"
        assert not (tmp_path.parent / "escape.py").exists()  # traversal flattened
        assert "files written" in result.output

    async def test_run_eval_invokes_injected_verifier(self, tmp_path):
        calls = []

        def fake_eval(problem_id, candidate, out, seed):
            calls.append((problem_id, candidate, out, seed))
            out.write_text(json.dumps({"success": False}), encoding="utf-8")

        envelope = json.dumps(
            {
                "summary": "probing",
                "facts": [],
                "artifact": None,
                "files": {"candidate.py": "NAME='x'\n"},
                "run_eval": {"problem_id": "p2", "candidate_file": "candidate.py", "out": "probe.json", "seed": 7},
            }
        )
        client = FakeLlmClient([envelope])
        runtime = LlmWorkerRuntime(
            client=client,
            assets=_assets(),
            schemas_dir=LAB_ROOT / "schemas",
            eval_runner=fake_eval,
        )
        result = await runtime.run_subagent(
            agent_id="worker", task="t", work_dir=str(tmp_path), context=_context(tmp_path, "evaluator")
        )
        assert calls and calls[0][0] == "p2" and calls[0][3] == 7
        assert "frozen eval ran" in result.output

    async def test_run_eval_required_seed_overrides_model_choice(self, tmp_path):
        """2026-08-13 audit finding: held_in/held_out instance separation for
        build_fix_loop tasks was enforced only by a natural-language "use
        seed=N" instruction — the model is free to ignore or misstate it.
        When the mission declares `required_seed`, it must win regardless of
        what the model puts in run_eval, and the mismatch must be logged."""
        calls = []

        def fake_eval(problem_id, candidate, out, seed):
            calls.append((problem_id, candidate, out, seed))
            out.write_text(json.dumps({"success": True, "seed": seed}), encoding="utf-8")

        envelope = json.dumps(
            {
                "summary": "probing",
                "facts": [],
                "artifact": None,
                "files": {"candidate.py": "NAME='x'\n"},
                "run_eval": {
                    "problem_id": "p2",
                    "candidate_file": "candidate.py",
                    "out": "probe.json",
                    "seed": 20260711,  # model used the held_in seed by mistake
                },
            }
        )
        client = FakeLlmClient([envelope])
        runtime = LlmWorkerRuntime(
            client=client,
            assets=_assets(),
            schemas_dir=LAB_ROOT / "schemas",
            eval_runner=fake_eval,
        )
        ctx = _context(tmp_path, "evaluator")
        ctx["team_metadata"]["required_seed"] = 20260812
        result = await runtime.run_subagent(
            agent_id="worker", task="t", work_dir=str(tmp_path), context=ctx
        )
        assert calls and calls[0][3] == 20260812  # required_seed won, not the model's 20260711
        assert "overriding to the required seed" in result.output

    async def test_run_eval_required_seed_used_when_model_omits_seed(self, tmp_path):
        calls = []

        def fake_eval(problem_id, candidate, out, seed):
            calls.append((problem_id, candidate, out, seed))
            out.write_text(json.dumps({"success": True, "seed": seed}), encoding="utf-8")

        envelope = json.dumps(
            {
                "summary": "probing",
                "facts": [],
                "artifact": None,
                "files": {"candidate.py": "NAME='x'\n"},
                "run_eval": {"problem_id": "p2", "candidate_file": "candidate.py", "out": "probe.json"},
            }
        )
        client = FakeLlmClient([envelope])
        runtime = LlmWorkerRuntime(
            client=client,
            assets=_assets(),
            schemas_dir=LAB_ROOT / "schemas",
            eval_runner=fake_eval,
        )
        ctx = _context(tmp_path, "evaluator")
        ctx["team_metadata"]["required_seed"] = 20260812
        await runtime.run_subagent(agent_id="worker", task="t", work_dir=str(tmp_path), context=ctx)
        assert calls and calls[0][3] == 20260812

    async def test_assess_role_widens_mission_workspace_to_campaign_run_dir(self, tmp_path):
        """2026-08-15 audit fix (E4(ii) follow-up): an assess-duty seat (e.g.
        round_assessor auditing package_manifest.json's contents[]) must be
        able to resolve campaign-root-relative paths, not just its own
        mission subdirectory — confirmed here by round-tripping a real
        `read` tool call through the LLM worker's tool loop and asserting
        it finds a file that lives at campaign_run_dir, not at the mission
        artifact's own parent directory (which contains nothing of the
        sort, exactly like the m_package case that blocked e4ii_pi_*)."""
        campaign_root = tmp_path / "campaign_root"
        campaign_root.mkdir()
        (campaign_root / "protocol.yaml").write_text("id: fixture\n", encoding="utf-8")
        mission_dir = campaign_root / "missions" / "m_package"
        mission_dir.mkdir(parents=True)

        # LlmResponse.tool_calls expects OpenAI-shape {"function": {"name", "arguments"}}.
        tool_call = {
            "id": "call_1",
            "function": {"name": "read", "arguments": json.dumps({"path": "protocol.yaml"})},
        }
        first = LlmResponse(content="", usage={"total_tokens": 5}, tool_calls=[tool_call])
        second_payload = json.dumps(
            {
                "decision": "completed",
                "reason_code": "ok_complete",
                "summary": "read protocol.yaml via widened cwd",
                "assessor_key": "round_assessor",
            }
        )

        class ToolAwareFakeClient(FakeLlmClient):
            def __init__(self, second_content: str) -> None:
                super().__init__([])
                self._second_content = second_content
                self._n = 0

            async def acomplete(self, messages, *, temperature=0.7, max_tokens=4096, **kwargs):
                del temperature, max_tokens, kwargs
                self.requests.append(messages)
                self._n += 1
                self.usage.add({"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
                if self._n == 1:
                    return first
                return LlmResponse(content=self._second_content, usage={"total_tokens": 15})

        client = ToolAwareFakeClient(second_payload)
        runtime = LlmWorkerRuntime(
            client=client, assets=_assets(), schemas_dir=LAB_ROOT / "schemas"
        )
        ctx = {
            "team_run_id": "run1",
            "team_agent_key": "round_assessor",
            "team_metadata": {
                "mission_id": "m_package",
                "exit_contract": "package_manifest",
                "skills_allowlist": [],
                "assess_roles": ["round_assessor"],
                "artifact_path": str(mission_dir / "package_manifest.json"),
                "campaign_run_dir": str(campaign_root),
            },
            "round_index": 1,
        }
        result = await runtime.run_subagent(
            agent_id="worker", task="assess m_package", work_dir=str(mission_dir), context=ctx
        )
        tool_result_messages = [m for m in client.requests[-1] if m.get("role") == "tool"]
        assert tool_result_messages, (
            "expected the read tool call's result to be replayed to the model"
        )
        tool_result = json.loads(tool_result_messages[0]["content"])
        assert tool_result["ok"] is True
        # actually read campaign_root/protocol.yaml's content, not the mission dir's (empty).
        assert "fixture" in tool_result["text"]
        assert json.loads(result.output)["decision"] == "completed"

    async def test_history_feeds_next_prompt(self, tmp_path):
        first = json.dumps({"summary": "idea alpha from muse", "facts": ["fact-x"], "artifact": None})
        second = json.dumps({"summary": "done", "facts": [], "artifact": None})
        runtime, client = _runtime([first, second])
        ctx = _context(tmp_path, "muse_1")
        await runtime.run_subagent(agent_id="worker", task="t", work_dir=str(tmp_path), context=ctx)
        ctx2 = _context(tmp_path, "judge")
        await runtime.run_subagent(agent_id="worker", task="t", work_dir=str(tmp_path), context=ctx2)
        user2 = client.requests[2][1]["content"]
        assert "idea alpha from muse" in user2  # compacted team context propagated


class TestCompaction:
    def test_pins_never_dropped_and_history_capped(self):
        history = [
            {"agent_key": "muse_1", "round_index": i, "state": "done", "result": f"r{i}" * 100}
            for i in range(20)
        ]
        pack = build_working_pack(agent_key="judge", pins=["protocol: frozen"], task_history=history)
        text = pack.render()
        assert "protocol: frozen" in text
        assert len(pack.recent) <= DEFAULT_PROFILE.keep_last_results  # default profile cap
        assert len(pack.recent) < len(history)  # history was capped, not dumped whole

    def test_implementer_keeps_failures(self):
        history = [
            {"agent_key": "implementer", "round_index": 1, "state": "failed", "error": "boom"},
            {"agent_key": "implementer", "round_index": 2, "state": "done", "result": "fixed"},
        ]
        pack = build_working_pack(agent_key="implementer", task_history=history)
        assert pack.failures and "boom" in pack.render()
        neutral = build_working_pack(agent_key="judge", task_history=history)
        assert not neutral.failures
