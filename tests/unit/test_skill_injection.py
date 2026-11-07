"""Unit tests for M3-1 Deterministic Skill Frontmatter Index Injection (sciteam/harness/context.py)."""

from pathlib import Path

from sciteam.harness.config import HarnessConfig
from sciteam.harness.context import harness_contract, skill_index
from sciteam.kernel.config_store import ConfigStore, interpreter_digest

ROOT = Path(__file__).resolve().parents[2]
CONFIGS = ROOT / "configs"


def test_no_skills_is_byte_identical_to_original_contract():
    store = ConfigStore(CONFIGS)
    cfg = store.load("harness/coder-default.yaml", HarnessConfig)

    # Without skills directory, skill_index returns empty string and harness_contract is byte-identical
    assert skill_index(store) == ""
    assert harness_contract(cfg, store) == harness_contract(cfg)

    digest = interpreter_digest()
    assert len(digest) == 64


def test_skill_frontmatter_injected_concisely_without_body_expansion(tmp_path):
    ov = tmp_path / "overlay_skills"
    skill_dir = ov / "skills" / "ode-stiff-solver"
    skill_dir.mkdir(parents=True)
    body_secret = "FULL_SKILL_BODY_DO_NOT_INLINE_IN_SYSTEM_PROMPT_123456789"
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: ode-stiff-solver\n"
        "description: Diagnose stiff ODE systems and switch to Radau/BDF with tight rtol/atol.\n"
        "when_to_use: When scipy.integrate.solve_ivp RK45 times out or exhibits step-size collapse.\n"
        "---\n\n"
        f"# Detailed Guide\n{body_secret}\n" + ("Very long body line.\n" * 50),
        encoding="utf-8",
    )

    store = ConfigStore(CONFIGS, overlays=[ov])
    cfg = store.load("harness/coder-default.yaml", HarnessConfig)

    idx = skill_index(store)
    assert "## Available skills" in idx
    assert "ode-stiff-solver" in idx
    assert "Diagnose stiff ODE systems" in idx
    # Full body must NOT be expanded into the system prompt
    assert body_secret not in idx

    # Every single skill index line must be < 200 characters
    skill_lines = [line for line in idx.splitlines() if line.startswith("- ")]
    assert len(skill_lines) == 1
    assert len(skill_lines[0]) < 200

    full_contract = harness_contract(cfg, store)
    assert full_contract.endswith(idx)
