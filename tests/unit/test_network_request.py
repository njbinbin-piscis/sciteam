import asyncio
import json

from sciteam.harness.tools.base import ToolContext
from sciteam.harness.tools.network import ALLOW, REQUESTS, RequestNetworkTool, approved_domains
from sciteam.runtime.egress import EgressProxy, allowed


def ctx(run_dir, egress=True):
    return ToolContext(sandbox=None, blobs=None, state=None, config=None, clock=None,
                       extras={"run_dir": run_dir, "agent": "engineer", "egress": lambda: egress})


def call(run_dir, **kw):
    return asyncio.run(RequestNetworkTool().run({"domain": "files.pythonhosted.org", "reason": "pip", **kw},
                                                ctx(run_dir)))


def test_request_waits_then_is_idempotent(tmp_path):
    r = call(tmp_path)
    assert not r.is_error and "sent to the owner" in r.content
    assert "waiting" in call(tmp_path).content
    reqs = json.loads((tmp_path / REQUESTS).read_text())
    assert len(reqs) == 1 and reqs[0]["by"] == "engineer" and reqs[0]["status"] == "pending"


def test_denied_and_approved(tmp_path):
    call(tmp_path)
    reqs = json.loads((tmp_path / REQUESTS).read_text()); reqs[0]["status"] = "denied"
    (tmp_path / REQUESTS).write_text(json.dumps(reqs))
    assert call(tmp_path).is_error
    (tmp_path / ALLOW).write_text(json.dumps(["files.pythonhosted.org"]))
    assert "already allowed" in call(tmp_path).content


def test_bad_domain_and_no_network(tmp_path):
    assert call(tmp_path, domain="*.evil.com").is_error and call(tmp_path, domain="a b").is_error
    r = asyncio.run(RequestNetworkTool().run({"domain": "x.org", "reason": "y"}, ctx(tmp_path, egress=False)))
    assert r.is_error and "no network" in r.content


def test_proxy_sees_approvals_without_restart(tmp_path):
    seen = []
    proxy = EgressProxy(["pypi.org"], tmp_path / "e.jsonl", extra_allow=lambda: approved_domains(tmp_path))
    assert not allowed("files.pythonhosted.org", proxy.allow + proxy.extra_allow())
    (tmp_path / ALLOW).write_text(json.dumps(["files.pythonhosted.org"]))
    assert allowed("files.pythonhosted.org", proxy.allow + proxy.extra_allow())
