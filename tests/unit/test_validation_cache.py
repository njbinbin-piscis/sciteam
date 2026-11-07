"""A cached verdict is valid only for the same files the validator can read outside the commit
(docs/40-institution.md §7). Found 2026-10-17: with private directories the verdict of the first
validation of a (validator, commit, command) was returned forever, whatever the members changed."""

from types import SimpleNamespace

from sciteam.institution.validation import ValidationWorker, digest_tree
from sciteam.institution.view import Validation


def _v(vid, inputs=None, status="requested"):
    return Validation(
        id=vid,
        validator="agree",
        commit="c1",
        command="cmd",
        act_id="a",
        timeout_s=60,
        status=status,
        inputs=inputs,
    )


def _worker(*validations):
    svc = SimpleNamespace(inst=SimpleNamespace(validations={v.id: v for v in validations}))
    return ValidationWorker(svc, workspace=None, blobs=None, sandbox=None)


def test_digest_follows_private_content_and_ignores_tool_caches(tmp_path):
    (tmp_path / "impl1").mkdir()
    (tmp_path / "impl1/solution.py").write_text("x = 1\n")
    first = digest_tree(tmp_path)
    (tmp_path / "impl1/__pycache__").mkdir()
    (tmp_path / "impl1/__pycache__/solution.pyc").write_bytes(b"\0")
    (tmp_path / "impl1/.pytest_cache").mkdir()
    (tmp_path / "impl1/.pytest_cache/v").write_text("noise")
    assert digest_tree(tmp_path) == first
    (tmp_path / "impl1/solution.py").write_text("x = 2\n")
    assert digest_tree(tmp_path) != first
    (tmp_path / "impl1/solution.py").write_text("x = 1\n")
    assert digest_tree(tmp_path) == first
    (tmp_path / "impl2").mkdir()
    (tmp_path / "impl2/solution.py").write_text("y = 1\n")
    assert digest_tree(tmp_path) != first  # the peer's file is also an input


def test_same_commit_and_command_hit_the_cache_only_for_the_same_inputs():
    done = _v("V1", inputs="d1", status="failed")
    worker = _worker(done, _v("V2"))
    new = worker.svc.inst.validations["V2"]
    assert worker._cached(new, "d1") is done
    assert worker._cached(new, "d2") is None  # a member changed a file: run again


def test_without_private_mounts_the_cache_behaves_as_before():
    done = _v("V1", inputs=None, status="passed")
    worker = _worker(done, _v("V2"))
    assert worker._cached(worker.svc.inst.validations["V2"], None) is done


def test_failed_validation_names_files_the_commit_lacks(tmp_path):
    import asyncio
    import dataclasses
    import subprocess

    from sciteam.providers.sandbox import LocalSandbox

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "seed").write_text("x\n")
    subprocess.run("git init -q && git add -A && git -c user.email=t@t -c user.name=t "
                   "commit -qm seed", shell=True, cwd=ws, check=True)
    sha = subprocess.run(["git", "-C", str(ws), "rev-parse", "HEAD"], capture_output=True,
                         text=True, check=True).stdout.strip()
    (ws / "REPORT.md").write_text("done\n")
    recorded = {}
    v = Validation(id="V1", validator="acceptance", commit=sha, command="test -s REPORT.md",
                   act_id="a", timeout_s=60, status="requested", inputs=None)
    svc = SimpleNamespace(inst=SimpleNamespace(validations={"V1": v}),
                          record_validation=lambda vid, **kw: recorded.update(kw))
    blobs = SimpleNamespace(put_text=lambda t: SimpleNamespace(sha256="b"))
    worker = ValidationWorker(svc, workspace=ws, blobs=blobs, sandbox=LocalSandbox)
    asyncio.run(worker._run(v))
    assert recorded["ok"] is False
    assert f"commit {sha[:10]} lacks REPORT.md" in recorded["tail"]

    # an amendment's trial names the script it runs that the recorded commit never held
    (ws / "seed").write_text("y\n")
    (ws / "check.py").write_text("import sys; sys.exit(1)\n")
    trial = dataclasses.replace(v, id="V2", act_id="amend:A1", status="requested",
                                command="python3 check.py && cat seed")
    svc.inst.validations["V2"] = trial
    asyncio.run(worker._run(trial))
    assert "the command runs check.py, which recorded commit" in recorded["tail"]
    assert "seed" not in recorded["tail"].split("[the command runs")[1].split(",")[0]
    assert "submit the new commit" not in recorded["tail"]
