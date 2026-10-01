import json

from sciteam.bench.swebench import is_valid, latest_results, write_predictions


def _rec(iid, outcome, reason=None):
    return json.dumps({"instance_id": iid, "outcome": outcome, "reason": reason}) + "\n"


def test_latest_record_wins_and_infra_excluded(tmp_path):
    (tmp_path / "patches").mkdir()
    for iid in ("a", "b", "c", "d"):
        (tmp_path / "patches" / f"{iid}.diff").write_text(f"diff {iid}\n")
    (tmp_path / "results.jsonl").write_text(
        _rec("a", "completed") + _rec("b", "error", "bad_request") + _rec("b", "error", "provider_quota")
        + _rec("c", "driver_error", "TimeoutError") + _rec("d", "error", "provider_quota")
        + _rec("d", "completed"))
    latest = latest_results(tmp_path / "results.jsonl")
    assert {k for k, r in latest.items() if is_valid(r)} == {"a", "d"}
    preds = [json.loads(x)["instance_id"]
             for x in write_predictions(tmp_path, "m").read_text().splitlines()]
    assert preds == ["a", "d"]


def _section(path, new, body="@@ -0,0 +1 @@\n+x\n"):
    head = f"diff --git a/{path} b/{path}\n"
    return head + ("new file mode 100644\n" if new else "index 1..2 100644\n") + body


def test_drop_new_artifacts_keeps_source_edits():
    from sciteam.bench.swebench import drop_new_artifacts

    edit = _section("requests/models.py", False)
    new_src = _section("pkg/new.py", True)
    junk = _section("build/lib/requests/api.py", True)
    egg = _section("foo.egg-info/PKG-INFO", True)
    tracked_build = _section("build/tool.py", False)
    out = drop_new_artifacts(junk + edit + egg + new_src + tracked_build)
    assert out == edit + new_src + tracked_build


def test_history_scrub_hides_commits_after_base(tmp_path):
    import shutil
    import subprocess

    import pytest

    from sciteam.bench.swebench import HISTORY_SCRUB

    if shutil.which("git") is None:
        pytest.skip("git not installed")
    repo = tmp_path / "testbed"
    repo.mkdir()

    def git(*a: str) -> str:
        return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=repo,
                              check=True, capture_output=True, text=True).stdout.strip()

    git("init", "-q", "-b", "main")
    (repo / "f.py").write_text("a = 1\n")
    git("add", "-A")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    (repo / "f.py").write_text("a = 2\n")
    git("commit", "-qam", "Fixed #1 -- the reference fix")
    fix = git("rev-parse", "HEAD")
    git("tag", "v2")
    git("branch", "stable")
    git("reset", "-q", "--hard", base)
    script = HISTORY_SCRUB.replace("/testbed", str(repo))
    subprocess.run(["bash", "-c", script], check=True, capture_output=True)
    assert git("rev-list", "--all", "--reflog") == base
    assert subprocess.run(["git", "cat-file", "-e", fix], cwd=repo).returncode != 0
    assert (repo / "f.py").read_text() == "a = 1\n"
