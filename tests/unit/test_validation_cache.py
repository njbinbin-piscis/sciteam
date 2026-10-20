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
