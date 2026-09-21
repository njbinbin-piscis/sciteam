"""`sciteam` CLI: thin shell over library functions, plus the packaging
invariant that makes `pip install sciteam && sciteam demo` work at all —
every runtime data directory resolves inside the package."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sciteam import __version__, _paths, cli
from sciteam.event_log import read_events
from sciteam.plugin_api import PLUGIN_API_VERSION

PARADIGMS = _paths.ASSETS_ROOT / "teams" / "paradigms"


# --- packaging invariant -----------------------------------------------------------


def test_bundled_data_lives_inside_the_package():
    # If any of these ever point outside sciteam/, a non-editable install breaks.
    for d in (_paths.DATA_ROOT, _paths.ASSETS_ROOT, _paths.SCHEMAS_DIR, _paths.CONTRACTS_DIR):
        assert d.is_dir(), d
        assert _paths.PACKAGE_ROOT in d.parents, d
    assert (PARADIGMS / "pipeline_chain.yaml").is_file()
    assert (_paths.SCHEMAS_DIR / "audit_veto.schema.json").is_file()
    assert (_paths.CONTRACTS_DIR / "ask_question.schema.json").is_file()


def test_no_module_assumes_a_repository_layout():
    # `Path(__file__).parents[1]` from inside sciteam/ is site-packages once
    # installed; the only sanctioned exception is runtime_identity's `git describe`
    # of a source checkout, which is explicitly about the checkout.
    offenders = []
    for py in _paths.PACKAGE_ROOT.glob("*.py"):
        if py.name in {"_paths.py", "runtime_identity.py"}:
            continue
        if "parents[1]" in py.read_text(encoding="utf-8"):
            offenders.append(py.name)
    assert offenders == []


def test_runs_root_defaults_to_cwd_and_honours_env(monkeypatch, tmp_path):
    monkeypatch.delenv(_paths.RUNS_ROOT_ENV, raising=False)
    monkeypatch.chdir(tmp_path)
    assert _paths.runs_root() == tmp_path / "runs"
    monkeypatch.setenv(_paths.RUNS_ROOT_ENV, str(tmp_path / "elsewhere"))
    assert _paths.runs_root() == tmp_path / "elsewhere"


# --- subcommands ---------------------------------------------------------------------


def test_version(capsys):
    assert cli.main(["version"]) == 0
    assert __version__ in capsys.readouterr().out
    assert cli.main(["version", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["sciteam"] == __version__
    assert payload["plugin_api_version"] == PLUGIN_API_VERSION


def test_validate_accepts_every_bundled_paradigm(capsys):
    assert cli.main(["validate", str(PARADIGMS)]) == 0
    out = capsys.readouterr().out
    assert out.count("ok    ") == len(list(PARADIGMS.glob("*.yaml")))
    assert "FAIL" not in out


def test_validate_rejects_manifest_violating_authority_invariant(tmp_path: Path, capsys):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        # one seat both emits the exit artifact and assesses the round: the
        # mutual-exclusion invariant team_loader enforces at load time
        "id: bad\nmembers:\n"
        "  - key: implementer\n    role: implementer\n    emits_exit_artifact: true\n"
        "    authority: [may_assess_round]\n"
        "coordination: {topology: chain, stopping: {kind: judgment}}\n",
        encoding="utf-8",
    )
    assert cli.main(["validate", str(bad)]) == 1
    out = capsys.readouterr().out
    assert "FAIL" in out and "may_assess_round" in out


def test_validate_with_no_files_is_a_usage_error(tmp_path: Path):
    assert cli.main(["validate", str(tmp_path)]) == 2


def test_paradigms_list_shows_every_bundled_template(capsys):
    assert cli.main(["paradigms", "list"]) == 0
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert len(lines) == len(list(PARADIGMS.glob("*.yaml")))
    assert any(ln.startswith("pipeline_chain") for ln in lines)


def test_plugins_list_runs_without_any_plugin_installed(capsys):
    assert cli.main(["plugins", "list", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["plugin_api_version"] == PLUGIN_API_VERSION
    assert isinstance(payload["plugins"], list)


@pytest.mark.parametrize(
    ("mode", "closed", "extra"),
    [
        ("full", True, []),
        ("full", True, ["--config-swap"]),
        ("prompt_only", True, []),
        ("no_recovery", False, []),
    ],
)
def test_demo_subcommand_writes_summary(tmp_path: Path, capsys, mode, closed, extra):
    out = tmp_path / "demo"
    assert cli.main(["demo", "--mode", mode, "--out", str(out), *extra]) == 0
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["mode"] == mode
    assert summary["interpreter_hash_unchanged"] is True
    assert summary["verdict"]["closed_legally"] is closed
    printed = json.loads(capsys.readouterr().out)
    assert printed["mode"] == mode


def test_demo_default_out_dir_is_under_cwd_runs(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(_paths.RUNS_ROOT_ENV, raising=False)
    assert cli.main(["demo", "--mode", "no_separation"]) == 0
    assert (
        tmp_path / "runs" / "canonical_institution_demo" / "no_separation" / "summary.json"
    ).is_file()


# --- ledger ------------------------------------------------------------------------


@pytest.fixture
def demo_run(tmp_path: Path, capsys) -> Path:
    out = tmp_path / "demo"
    assert cli.main(["demo", "--mode", "full", "--out", str(out)]) == 0
    capsys.readouterr()
    return out


def test_ledger_verify_accepts_a_run_dir_or_a_file(demo_run: Path, capsys):
    assert cli.main(["ledger", "verify", str(demo_run)]) == 0
    assert "ok" in capsys.readouterr().out
    assert cli.main(["ledger", "verify", str(demo_run / "ledger.jsonl")]) == 0


def test_ledger_verify_exits_nonzero_on_a_tampered_chain(demo_run: Path, capsys):
    path = demo_run / "ledger.jsonl"
    lines = path.read_text().splitlines()
    path.write_text("\n".join(lines[:3] + lines[4:]) + "\n")
    assert cli.main(["ledger", "verify", str(path)]) == 1
    out = capsys.readouterr().out
    assert "BROKEN" in out and "does not link" in out


def test_ledger_verify_json_reports_every_problem(demo_run: Path, capsys):
    path = demo_run / "ledger.jsonl"
    path.write_text(path.read_text().replace('"rejected"', '"accepted"', 1))
    assert cli.main(["ledger", "verify", str(path), "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["problems"]


def test_ledger_show_marks_what_counted(demo_run: Path, capsys):
    assert cli.main(["ledger", "show", str(demo_run)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert any(" x " in ln and "bystander" in ln for ln in lines)
    assert any(" + " in ln and "veto_issued" in ln for ln in lines)


def test_ledger_replay_reports_derived_state(demo_run: Path, capsys):
    assert cli.main(["ledger", "replay", str(demo_run)]) == 0
    state = json.loads(capsys.readouterr().out)
    assert state["finished"] is True
    assert state["effective_artifacts"]["m_implement"]["attempt"] == 2
    assert len(state["reopenings"]) == 1


def test_ledger_cost_is_empty_for_a_scripted_run(demo_run: Path, capsys):
    assert cli.main(["ledger", "cost", str(demo_run)]) == 0
    assert "no llm_call entries" in capsys.readouterr().out


def test_ledger_show_on_a_corrupt_file_fails_cleanly(tmp_path: Path, capsys):
    path = tmp_path / "ledger.jsonl"
    path.write_text("{not json\n")
    assert cli.main(["ledger", "show", str(path)]) == 2
    assert "not valid JSON" in capsys.readouterr().err


# --- env ---------------------------------------------------------------------------


def test_env_prints_the_fingerprint_and_its_gaps(capsys):
    assert cli.main(["env"]) == 0
    out = capsys.readouterr().out
    assert "digest" in out
    assert "reproducible" in out  # either "fully pinned" or the NOT-reproducible list


def test_env_json_carries_the_unpinnable_list(capsys):
    assert cli.main(["env", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert {"pinned", "pinned_digest", "context", "unpinnable", "reproducible"} <= set(payload)


def test_env_against_a_run_reports_no_drift_for_the_same_environment(demo_run: Path, capsys):
    assert cli.main(["env", "--against", str(demo_run)]) == 0
    assert "identical pinned environment" in capsys.readouterr().out


def test_env_against_a_run_names_the_covariate_that_moved(demo_run: Path, monkeypatch, capsys):
    monkeypatch.setenv("SCITEAM_MAX_ITERATIONS", "9")
    assert cli.main(["env", "--against", str(demo_run)]) == 1
    assert "SCITEAM_MAX_ITERATIONS" in capsys.readouterr().out


def test_env_against_a_run_without_provenance_fails_cleanly(tmp_path: Path, capsys):
    (tmp_path / "summary.json").write_text("{}")
    assert cli.main(["env", "--against", str(tmp_path)]) == 2
    assert "records no provenance" in capsys.readouterr().err


def test_a_run_records_its_own_provenance(demo_run: Path):
    summary = json.loads((demo_run / "summary.json").read_text())
    assert summary["provenance"]["pinned"]["sciteam_version"] == __version__
    # And the ledger carries it too, so it is inside the hash chain.
    started = read_events(demo_run / "ledger.jsonl")[0]
    assert started.kind == "run_started"
    assert started.payload["provenance"]["pinned_digest"]


# --- mutate ------------------------------------------------------------------------


def test_mutate_passes_on_the_bundled_paradigms(capsys):
    assert cli.main(["mutate", str(PARADIGMS)]) == 0
    out = capsys.readouterr().out
    assert "score=1.00" in out
    assert "SURVIVED" not in out


def test_mutate_verbose_lists_every_operator(capsys):
    assert cli.main(["mutate", str(PARADIGMS / "pipeline_chain.yaml"), "-v"]) == 0
    out = capsys.readouterr().out
    assert "M2/flip-separation" in out and "EQ/reorder-seats" in out


def test_mutate_reports_an_unreadable_manifest(tmp_path: Path, capsys):
    weak = tmp_path / "weak.yaml"
    weak.write_text("- this manifest is a list, not a mapping\n")
    assert cli.main(["mutate", str(weak)]) == 2
    assert "cannot read" in capsys.readouterr().err


def test_mutate_json_carries_scores(capsys):
    assert cli.main(["mutate", str(PARADIGMS / "pipeline_chain.yaml"), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["score"] == 1.0
    assert any(r["operator"] == "M1/drop-flag" for r in payload[0]["results"])


def test_ledger_resume_state_on_a_finished_run(demo_run: Path, capsys):
    assert cli.main(["ledger", "resume-state", str(demo_run)]) == 0
    out = capsys.readouterr().out
    # The canonical demo is not a campaign, so it records no mission
    # decisions — but it did finish, and resuming would run nothing.
    assert "finished" in out


def test_ledger_resume_state_json(demo_run: Path, capsys):
    assert cli.main(["ledger", "resume-state", str(demo_run), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["finished"] is True
    assert set(payload) == {
        "run_id",
        "finished",
        "decided_missions",
        "succeeded",
        "tokens_charged",
    }


def test_ledger_resume_state_on_an_empty_ledger(tmp_path: Path, capsys):
    (tmp_path / "ledger.jsonl").write_text("")
    assert cli.main(["ledger", "resume-state", str(tmp_path)]) == 0
    assert "no run recorded" in capsys.readouterr().out


def test_init_list_shows_paradigms(capsys):
    assert cli.main(["init", "--list"]) == 0
    out = capsys.readouterr().out
    assert "pipeline_chain" in out
    assert "approval_gate" in out
    assert "debate_elo" in out


def test_init_list_json(capsys):
    assert cli.main(["init", "--list", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload) >= 21
    ids = {t["id"] for t in payload}
    assert "pipeline_chain" in ids
    assert "approval_gate" in ids


def test_init_scaffold_project_and_run_its_tests(tmp_path: Path, capsys):
    target = tmp_path / "my_project"
    assert cli.main(["init", "approval_gate", "--target", str(target)]) == 0
    out = capsys.readouterr().out
    assert "Initialized institutional project from 'approval_gate'" in out
    assert (target / "institution.yaml").is_file()
    assert (target / "test_institution.py").is_file()
    assert (target / "README.md").is_file()
    assert (target / "prompts").is_dir()

    # Validate generated manifest using sciteam CLI validate
    assert cli.main(["validate", str(target / "institution.yaml")]) == 0

    # Validate mutation testing using sciteam CLI mutate
    assert cli.main(["mutate", str(target / "institution.yaml")]) == 0


def test_init_refuses_overwrite_without_flag(tmp_path: Path, capsys):
    target = tmp_path / "busy_dir"
    target.mkdir()
    (target / "some_file.txt").write_text("hello")
    # Fails without --overwrite
    assert cli.main(["init", "pipeline_chain", "--target", str(target)]) == 1
    assert "not empty" in capsys.readouterr().err

    # Succeeds with --overwrite
    assert cli.main(["init", "pipeline_chain", "--target", str(target), "--overwrite"]) == 0
    assert (target / "institution.yaml").is_file()


def test_ledger_view_exports_standalone_html(demo_run: Path, tmp_path: Path, capsys):
    html_out = tmp_path / "view.html"
    assert cli.main(["ledger", "view", str(demo_run), "--out", str(html_out)]) == 0
    assert "exported interactive ledger view" in capsys.readouterr().out
    assert html_out.is_file()

    content = html_out.read_text(encoding="utf-8")
    assert "<!DOCTYPE html>" in content
    assert "CHAIN VERIFIED" in content
    assert "artifact_submitted" in content
    assert "veto_issued" in content
    assert "reopened" in content
    # Assert zero external CDN stylesheet/script references
    assert "http://" not in content
    assert "https://" not in content


def test_plugins_add_and_remove_subcommands(monkeypatch, capsys):
    commands_run = []

    class FakeCompletedProcess:
        returncode = 0
        stdout = "Successfully installed"
        stderr = ""

    def fake_subprocess_run(cmd, capture_output=True, text=True):
        commands_run.append(cmd)
        return FakeCompletedProcess()

    monkeypatch.setattr(cli.subprocess, "run", fake_subprocess_run)

    # Test plugins add
    assert cli.main(["plugins", "add", "sciteam-runtime-anthropic"]) == 0
    assert any(
        "pip" in c and "install" in c and "sciteam-runtime-anthropic" in c for c in commands_run
    )
    out = capsys.readouterr().out
    assert "successfully installed" in out

    # Test plugins remove
    commands_run.clear()
    assert cli.main(["plugins", "remove", "sciteam-runtime-anthropic"]) == 0
    assert any(
        "pip" in c and "uninstall" in c and "sciteam-runtime-anthropic" in c for c in commands_run
    )
    out = capsys.readouterr().out
    assert "successfully uninstalled" in out
