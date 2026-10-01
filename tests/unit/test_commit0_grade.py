import importlib.util
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("c0grade", ROOT / "bench/commit0/grade.py")
grade = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(grade)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
                           *args], check=True, capture_output=True, text=True).stdout


def test_binary_changes_never_reach_the_patch(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "mod.py").write_text("def f():\n    pass\n")
    (repo / "logo.bin").write_bytes(b"\x00\x01base")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    (repo / "mod.py").write_text("def f():\n    return 1\n")      # the team's work
    (repo / ".coverage").write_bytes(b"\x00SQLite format 3\x00")     # new binary artifact
    (repo / "logo.bin").write_bytes(b"\x00\x02changed")              # changed binary
    _git(repo, "add", "-A")
    dropped = grade.drop_binary_changes(repo)
    assert sorted(dropped) == [".coverage", "logo.bin"]
    staged = _git(repo, "diff", "--cached", "--name-only", "HEAD").split()
    assert staged == ["mod.py"]
    assert "Binary files" not in _git(repo, "diff", "--cached", "HEAD")
