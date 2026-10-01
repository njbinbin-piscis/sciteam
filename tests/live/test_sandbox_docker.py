import subprocess

import pytest

from sciteam.providers.sandbox import DockerSandbox, DockerSandboxConfig
from sciteam.providers.sandbox.docker import _LIVE_CONTAINERS

IMAGE = "swebench/sweb.eval.x86_64.astropy_1776_astropy-12907:latest"


def _have_image() -> bool:
    try:
        return subprocess.run(["docker", "image", "inspect", IMAGE],
                              capture_output=True).returncode == 0
    except OSError:
        return False


pytestmark = [pytest.mark.live, pytest.mark.skipif(not _have_image(), reason="image missing")]


def _cfg(**kw):
    base = dict(image=IMAGE, workdir="/testbed",
                init_commands=["source /opt/miniconda3/bin/activate testbed"])
    base.update(kw)
    return DockerSandboxConfig(**base)


async def test_swebench_shell():
    sb = DockerSandbox(_cfg())
    await sb.start()
    try:
        r = await sb.run("python -c 'import astropy,sys; print(sys.prefix)'; git status --short",
                         timeout_s=120)
        assert r.exit_code == 0 and "testbed" in r.output
        await sb.run("cd astropy && export X=1", timeout_s=10)
        r = await sb.run("pwd; echo $X", timeout_s=10)
        assert r.output.split() == ["/testbed/astropy", "1"]
        r = await sb.run("sleep 60", timeout_s=2)
        assert r.timed_out and r.shell_restarted
        r = await sb.run("pwd; which python", timeout_s=10)
        assert r.output.split()[0] == "/testbed/astropy" and "testbed" in r.output
        await sb.write_file("/tmp/x.bin", b"\x00\x01\xff")
        assert await sb.read_file("/tmp/x.bin") == b"\x00\x01\xff"
        r = await sb.run("curl -sS -m 5 https://github.com >/dev/null; echo rc=$?", timeout_s=30)
        assert "rc=0" not in r.output  # network none
    finally:
        cid = sb.container_id
        await sb.close()
    assert cid not in _LIVE_CONTAINERS
    assert subprocess.run(["docker", "inspect", cid], capture_output=True).returncode != 0


async def test_attached_timeout_kills_only_that_command():
    cid = subprocess.run(["docker", "run", "-d", "--network", "none", IMAGE, "sleep", "infinity"],
                         capture_output=True, text=True, check=True).stdout.strip()
    try:
        sb = DockerSandbox(DockerSandboxConfig(image="", workdir="/testbed", attach_container=cid,
                                               env={"SCITEAM_T": "7"}))
        await sb.start()
        r = await sb.run("sleep 1001 & echo $SCITEAM_T", timeout_s=10)
        assert r.exit_code == 0 and r.output.strip() == "7"
        r = await sb.run("setsid sleep 1002 & sleep 1003", timeout_s=2)
        assert r.timed_out and r.shell_restarted
        r = await sb.run("ps -eo args | grep '^sleep 100' | sort", timeout_s=10)
        assert r.output.split("\n")[:2] == ["sleep 1001", "sleep 1002"]
        assert "sleep 1003" not in r.output
        await sb.close()
        assert subprocess.run(["docker", "inspect", cid], capture_output=True).returncode == 0
        out = subprocess.run(["docker", "exec", cid, "sh", "-c", "ps -eo args"],
                             capture_output=True, text=True).stdout
        assert "sleep 1001" in out and "bash --noprofile" not in out
    finally:
        subprocess.run(["docker", "rm", "-f", cid], capture_output=True)


async def test_failed_start_leaves_no_container():
    sb = DockerSandbox(_cfg(init_commands=["exit 7"]))
    with pytest.raises(RuntimeError):
        await sb.start()
    assert sb.container_id is None
