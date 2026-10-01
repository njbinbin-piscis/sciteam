import pytest

from sciteam.providers.sandbox import LocalSandbox


@pytest.fixture
async def sb(tmp_path):
    s = LocalSandbox(tmp_path / "w", max_capture_bytes=64 * 1024)
    await s.start()
    yield s
    await s.close()


async def test_state_persists(sb):
    await sb.run("mkdir -p sub && cd sub && export FOO=bar", timeout_s=10)
    r = await sb.run("pwd; echo $FOO", timeout_s=10)
    assert r.exit_code == 0
    assert r.output.splitlines()[0].endswith("/w/sub") and r.output.splitlines()[1] == "bar"


async def test_exit_code_and_stderr(sb):
    r = await sb.run("echo out; echo err >&2; false", timeout_s=10)
    assert r.exit_code == 1 and "out" in r.output and "err" in r.output


async def test_stdin_cannot_eat_protocol(sb):
    r = await sb.run("cat; read x; echo after", timeout_s=10)
    assert r.exit_code == 0 and r.output.strip() == "after"


async def test_timeout_restarts_shell_and_keeps_cwd(sb):
    await sb.run("mkdir -p d && cd d", timeout_s=10)
    r = await sb.run("echo started; sleep 30", timeout_s=1)
    assert r.timed_out and r.shell_restarted and "started" in r.output
    r2 = await sb.run("pwd", timeout_s=10)
    assert r2.exit_code == 0 and r2.output.strip().endswith("/w/d")


async def test_exit_kills_shell_then_recovers(sb):
    r = await sb.run("exit 3", timeout_s=10)
    assert r.shell_restarted
    r2 = await sb.run("echo alive", timeout_s=10)
    assert r2.output.strip() == "alive"


async def test_capture_limit(sb):
    r = await sb.run("yes abcdefgh | head -c 1000000; echo; echo END", timeout_s=20)
    assert r.capture_truncated and r.output_bytes > 1_000_000 - 10
    assert "bytes dropped" in r.output and r.output.rstrip().endswith("END")


async def test_many_commands_no_crosstalk(sb):
    for i in range(100):
        r = await sb.run(f"echo line-{i}", timeout_s=10)
        assert r.output.strip() == f"line-{i}"


async def test_file_roundtrip(sb):
    data = bytes(range(256)) * 10
    await sb.write_file("a/b.bin", data)
    assert await sb.read_file("a/b.bin") == data


async def test_no_trailing_newline_output(sb):
    r = await sb.run("printf abc", timeout_s=10)
    assert r.output == "abc"


async def test_concurrent_sandboxes_do_not_share_command_scripts(tmp_path):
    import asyncio

    boxes = [LocalSandbox(tmp_path) for _ in range(4)]
    for b in boxes:
        await b.start()
    try:
        for _ in range(5):
            results = await asyncio.gather(*(b.run(f"sleep 0.01; echo {i}", timeout_s=30)
                                             for i, b in enumerate(boxes)))
            assert [(r.exit_code, r.output.strip()) for r in results] == \
                [(0, str(i)) for i in range(4)]
    finally:
        for b in boxes:
            await b.close()
