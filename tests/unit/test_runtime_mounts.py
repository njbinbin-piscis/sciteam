from pathlib import Path

from sciteam.comm.model import Member
from sciteam.runtime.config import (
    TeamSandboxConfig,
    member_mounts,
    private_root,
    validator_private_mounts,
)
from sciteam.runtime.scheduler import memory_limit


def test_no_private_mount_keeps_only_the_shared_workspace(tmp_path: Path):
    ws = tmp_path / "workspace"
    sc = TeamSandboxConfig(kind="docker", image="x")
    assert member_mounts(sc, ws, "a") == [f"{ws.resolve()}:/workspace"]
    assert validator_private_mounts(sc, ws) == []
    assert not private_root(ws).exists()


def test_each_member_sees_only_its_own_private_dir(tmp_path: Path):
    ws = tmp_path / "workspace"
    sc = TeamSandboxConfig(kind="docker", image="x", private_mount="/private")
    a, b = member_mounts(sc, ws, "a"), member_mounts(sc, ws, "b")
    assert a[1] == f"{(private_root(ws) / 'a').resolve()}:/private/a"
    assert b[1] == f"{(private_root(ws) / 'b').resolve()}:/private/b"
    assert not any("/b:" in m for m in a) and not any("/a:" in m for m in b)
    assert validator_private_mounts(sc, ws) == []
    assert validator_private_mounts(sc, ws, "b") == [
        f"{(private_root(ws) / 'b').resolve()}:/private/b:ro"]


def test_memory_limit_member_override():
    assert memory_limit(None, 3) == 3
    assert memory_limit(Member(id="a"), 3) == 3
    assert memory_limit(Member(id="a", attrs={"memory_activations": "0"}), 3) == 0
    assert memory_limit(Member(id="a", attrs={"memory_activations": "1"}), 3) == 1


def test_read_only_workspace_and_peer_private_dirs(tmp_path: Path):
    ws = tmp_path / "workspace"
    sc = TeamSandboxConfig(kind="docker", image="x", private_mount="/private")
    p = member_mounts(sc, ws, "prover", {"workspace_access": "ro"})
    assert p[0] == f"{ws.resolve()}:/workspace:ro"
    v = member_mounts(sc, ws, "verifier", {"reads_private": "prover, other"})
    assert v[0] == f"{ws.resolve()}:/workspace"
    assert v[1] == f"{(private_root(ws) / 'verifier').resolve()}:/private/verifier"
    assert v[2:] == [f"{(private_root(ws) / 'prover').resolve()}:/private/prover:ro",
                     f"{(private_root(ws) / 'other').resolve()}:/private/other:ro"]
    # a member nobody granted reads only its own directory
    own = f"{(private_root(ws) / 'prover').resolve()}:/private/prover"
    assert [m for m in p if ":/private/" in m] == [own]


def test_mount_attribute_errors(tmp_path: Path):
    import pytest

    ws = tmp_path / "workspace"
    plain = TeamSandboxConfig(kind="docker", image="x")
    with pytest.raises(ValueError):
        member_mounts(plain, ws, "v", {"reads_private": "p"})
    with pytest.raises(ValueError):
        member_mounts(plain, ws, "v", {"workspace_access": "rwx"})
    priv = TeamSandboxConfig(kind="docker", image="x", private_mount="/private")
    with pytest.raises(ValueError):
        member_mounts(priv, ws, "v", {"reads_private": "v"})


def test_shared_tools_are_writable_for_the_engineer_only(tmp_path: Path):
    import pytest

    from sciteam.runtime.config import tools_root, validator_tools_mounts

    ws = tmp_path / "workspace"
    sc = TeamSandboxConfig(kind="docker", image="x", tools_mount="/opt/tools")
    tools = f"{tools_root(ws).resolve()}:/opt/tools"
    assert member_mounts(sc, ws, "r")[-1] == tools + ":ro"
    assert member_mounts(sc, ws, "eng", {"tools_access": "rw"})[-1] == tools
    assert validator_tools_mounts(sc, ws) == [tools + ":ro"]
    with pytest.raises(ValueError):
        member_mounts(sc, ws, "r", {"tools_access": "rwx"})
    plain = TeamSandboxConfig(kind="docker", image="x")
    assert member_mounts(plain, ws, "r") == [f"{ws.resolve()}:/workspace"]
    assert validator_tools_mounts(plain, ws) == []


def test_docker_run_gets_gpus_and_shm(monkeypatch):
    import asyncio

    from sciteam.providers.sandbox import docker as d

    calls = []

    async def fake(*args, **kw):
        calls.append(args)
        return 0, b"cid\n"

    monkeypatch.setattr(d, "_docker", fake)

    async def go(**kw):
        sb = d.DockerSandbox(d.DockerSandboxConfig(image="img", **kw))

        async def noop(*a, **k):
            return None
        sb._spawn_shell = noop
        await sb.start()

    asyncio.run(go(gpus="device=0,1", shm_size="16g"))
    run = next(c for c in calls if c[0] == "run")
    assert run[run.index("--gpus") + 1] == "device=0,1"
    assert run[run.index("--shm-size") + 1] == "16g"
    calls.clear()
    asyncio.run(go())
    assert "--gpus" not in next(c for c in calls if c[0] == "run")
