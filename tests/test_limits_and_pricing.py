"""Declared Gen-2 limits and token→USD pricing."""

from __future__ import annotations

from sciteam import limits, pricing
from sciteam.llm import LlmUsage
from sciteam.runtime_identity import describe_local_runtime, runtime_version


def test_snapshot_defaults_are_unified_window():
    snap = limits.snapshot()
    assert snap["defaults"]["context_window"] == 1_000_000
    assert snap["defaults"]["max_tokens"] == 32_768
    assert snap["tool_result_visible_chars"] >= 24_000


def test_write_engine_limits(tmp_path):
    path = tmp_path / "engine_limits.json"
    data = limits.write_engine_limits(path)
    assert path.is_file()
    assert data["context_window"] == limits.context_window()


def test_cost_usd_zero_without_rates(monkeypatch):
    monkeypatch.delenv("SCITEAM_LLM_INPUT_USD_PER_MTOK", raising=False)
    monkeypatch.delenv("SCITEAM_LLM_OUTPUT_USD_PER_MTOK", raising=False)
    assert pricing.cost_usd(prompt_tokens=1000, completion_tokens=1000) == 0.0
    usage = LlmUsage()
    usage.add({"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500})
    assert usage.cost_usd == 0.0


def test_cost_usd_computes_when_rates_set(monkeypatch):
    monkeypatch.setenv("SCITEAM_LLM_INPUT_USD_PER_MTOK", "1.0")
    monkeypatch.setenv("SCITEAM_LLM_OUTPUT_USD_PER_MTOK", "2.0")
    assert pricing.cost_usd(prompt_tokens=1_000_000, completion_tokens=500_000) == 2.0
    usage = LlmUsage()
    usage.add({"prompt_tokens": 1_000_000, "completion_tokens": 0, "total_tokens": 1_000_000})
    assert usage.cost_usd == 1.0


def test_local_runtime_version_is_pinnable():
    ver = describe_local_runtime()
    assert ver.startswith("local ")
    assert ver != "local"


def test_image_digest_pins_an_installed_deployment(monkeypatch):
    monkeypatch.setenv("SCITEAM_IMAGE_DIGEST", "sha256:abc123")
    assert describe_local_runtime() == "local image:sha256:abc123"


def test_a_backend_that_knows_its_build_reports_it():
    class _Versioned:
        def version(self) -> str:
            return "my-agent-cli 2.3.1"

    assert runtime_version(_Versioned()) == "my-agent-cli 2.3.1"


def test_a_backend_without_a_version_falls_back_to_the_local_runtime():
    assert runtime_version(object()).startswith("local ")


def test_a_backend_whose_version_raises_is_recorded_as_unknown():
    class _Broken:
        def version(self) -> str:
            raise RuntimeError("no build info")

    # Visibly missing in the ledger, never silently attributed to the local build.
    assert runtime_version(_Broken()) == "unknown"
