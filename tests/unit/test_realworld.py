import asyncio
import json

from sciteam.harness.tools.literature import LiteratureSearchTool
from sciteam.providers.sandbox.base import ExecResult
from sciteam.runtime.realworld import (PIP_LOCK, declared_env, freeze_env, restore_commands,
                                       scrub)


class FakeSandbox:
    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.workdir = "/workspace"

    async def run(self, command, *, timeout_s):
        out = "numpy==2.0\nscipy==1.14\n" if "pip freeze" in command else (
            "git\ncurl\n" if "apt-mark" in command else "")
        return ExecResult(exit_code=0, output=out)

    async def read_file(self, path):
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    async def write_file(self, path, data):
        self.files[path] = data


def test_freeze_and_restore_roundtrip():
    sb = FakeSandbox()
    sums = asyncio.run(freeze_env(sb))
    assert PIP_LOCK in sums and sb.files[PIP_LOCK].decode().startswith("numpy==2.0")
    cmds = restore_commands(sb.files[PIP_LOCK].decode(), sb.files["env/apt.lock"].decode())
    assert len(cmds) == 1 and "--no-index" in cmds[0] and f"/workspace/{PIP_LOCK}" in cmds[0]
    assert restore_commands("", "") == []


def test_only_declared_secrets_are_injected_and_scrubbed():
    host = {"OPENAI_API_KEY": "sk-abcdef123456", "OTHER": "shh-should-not-pass"}
    env = declared_env(["OPENAI_API_KEY", "MISSING"], host)
    assert env == {"OPENAI_API_KEY": "sk-abcdef123456"}
    assert "OTHER" not in env
    log = "calling with sk-abcdef123456 now"
    assert "sk-abcdef" not in scrub(log, env) and "[secret:OPENAI_API_KEY]" in scrub(log, env)


CROSSREF = json.dumps({"message": {"items": [{
    "DOI": "10.1/x", "title": ["A paper"], "URL": "https://doi.org/10.1/x",
    "author": [{"given": "A", "family": "B"}], "issued": {"date-parts": [[2021]]}}]}}).encode()
ARXIV = b"""<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>http://arxiv.org/abs/1</id>
<title>An  arXiv  paper</title><published>2022-01-02T00:00:00Z</published>
<author><name>C D</name></author></entry></feed>"""


def test_literature_search_returns_sources_and_records_citations():
    def fetch(url):
        return CROSSREF if "crossref" in url else ARXIV
    sb = FakeSandbox()
    from sciteam.harness.tools.base import ToolContext
    ctx = ToolContext(sandbox=sb, blobs=None, state=None, config=None, clock=None)
    r = asyncio.run(LiteratureSearchTool(fetch).run({"query": "kinetics"}, ctx))
    hits = json.loads(r.content)
    assert {h["source"] for h in hits} == {"crossref", "arxiv"}
    assert hits[0]["doi"] == "10.1/x" and hits[1]["title"] == "An arXiv paper"
    lines = sb.files["refs/citations.jsonl"].decode().splitlines()
    assert len(lines) == 2 and json.loads(lines[1])["url"] == "http://arxiv.org/abs/1"


def test_literature_failure_is_reported_not_raised():
    def fetch(url):
        raise OSError("offline")
    from sciteam.harness.tools.base import ToolContext
    ctx = ToolContext(sandbox=FakeSandbox(), blobs=None, state=None, config=None, clock=None)
    r = asyncio.run(LiteratureSearchTool(fetch).run({"query": "x"}, ctx))
    assert r.is_error and "unavailable" in r.content


def test_validator_rebuilds_from_lock_only_in_realworld(tmp_path):
    from sciteam.institution.team import _rebuild_commands_for
    (tmp_path / "env").mkdir()
    (tmp_path / PIP_LOCK).write_text("numpy==2.0\n")
    assert len(_rebuild_commands_for("realworld", tmp_path)) == 1
    assert _rebuild_commands_for("benchmark", tmp_path) == []
    assert _rebuild_commands_for("realworld", tmp_path / "none") == []
