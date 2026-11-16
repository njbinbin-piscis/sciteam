import asyncio
import json

from sciteam.harness.tools.base import ToolContext
from sciteam.harness.tools.compute import RerunJobTool, RunOnNodeTool, JobResult, sha


class MemSandbox:
    def __init__(self, files=None):
        self.files = dict(files or {})
    async def read_file(self, path): return self.files[path]
    async def write_file(self, path, data): self.files[path] = data


class Backend:
    def __init__(self, out=b"42\n", rc=0):
        self.specs, self.out, self.rc = [], out, rc
    def available(self): return [{"id": "gpu88"}]
    async def run(self, spec):
        self.specs.append(spec)
        outs = {p: self.out for p in spec.outputs}
        return JobResult(self.rc, "done\n", outs, 3.0, False, "gpu88", "GPU 机", "abc123", [0])


def ctx(sb, backend=None):
    return ToolContext(sandbox=sb, blobs=None, state=None, config=None, clock=None,
                       extras={"compute": backend} if backend else {})


def test_job_record_holds_the_evidence():
    sb, be = MemSandbox({"data/x.csv": b"1,2\n"}), Backend()
    r = asyncio.run(RunOnNodeTool().run({"command": "python f.py", "inputs": ["data/x.csv"],
                                         "outputs": ["res.txt"], "gpus": 1}, ctx(sb, be)))
    assert not r.is_error and "GPU 机" in r.content
    job = r.meta["job"]
    m = json.loads(sb.files[f"jobs/{job}/job.json"])
    assert m["inputs"] == {"data/x.csv": sha(b"1,2\n")} and m["outputs"] == {"res.txt": sha(b"42\n")}
    assert m["env_hash"] == "abc123" and m["gpu_indices"] == [0] and m["exit_code"] == 0
    assert sb.files[f"jobs/{job}/out/res.txt"] == b"42\n" and sb.files[f"jobs/{job}/in/data/x.csv"]
    assert be.specs[0].gpus == 1


def test_failures_and_paths_are_reported_not_raised():
    sb = MemSandbox()
    assert asyncio.run(RunOnNodeTool().run({"command": "x"}, ctx(sb))).is_error      # no backend
    r = asyncio.run(RunOnNodeTool().run({"command": "x", "inputs": ["../etc/passwd"]}, ctx(sb, Backend())))
    assert r.is_error and "inside the workspace" in r.content
    r = asyncio.run(RunOnNodeTool().run({"command": "x"}, ctx(sb, Backend(rc=2))))
    assert r.is_error and "exit 2" in r.content


def test_rerun_reproduces_or_differs():
    sb, be = MemSandbox({"a": b"1"}), Backend()
    first = asyncio.run(RunOnNodeTool().run({"command": "c", "inputs": ["a"], "outputs": ["o"]}, ctx(sb, be)))
    r = asyncio.run(RerunJobTool().run({"job": first.meta["job"]}, ctx(sb, be)))
    assert not r.is_error and r.content.startswith("reproduced")
    r = asyncio.run(RerunJobTool().run({"job": first.meta["job"]}, ctx(sb, Backend(out=b"43\n"))))
    assert r.is_error and r.content.startswith("differs") and "['o']" in r.content
    sb.files[f"jobs/{first.meta['job']}/in/a"] = b"changed"
    assert "inputs changed" in asyncio.run(RerunJobTool().run({"job": first.meta["job"]}, ctx(sb, be))).content
