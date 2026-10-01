import asyncio
import os
import shlex
import time
from typing import Protocol

from pydantic import BaseModel

from sciteam.kernel.ids import new_id


class ExecResult(BaseModel):
    exit_code: int | None
    output: str
    timed_out: bool = False
    duration_s: float = 0.0
    shell_restarted: bool = False
    output_bytes: int = 0
    capture_truncated: bool = False


class Sandbox(Protocol):
    async def start(self) -> None: ...
    async def run(self, command: str, *, timeout_s: float) -> ExecResult: ...
    async def read_file(self, path: str) -> bytes: ...
    async def write_file(self, path: str, data: bytes) -> None: ...
    async def close(self) -> None: ...
    @property
    def workdir(self) -> str: ...


class ShellDied(Exception):
    pass


class PersistentShell:
    """One long-lived bash process speaking a sentinel protocol over stdin/stdout.

    Each command is written to a script file by the owner and executed with
    `{ source <file>; } < /dev/null 2>&1`, so the command cannot consume the protocol
    stream and cwd / env / activated environments persist between commands.
    """

    def __init__(self, argv: list[str], *, max_capture_bytes: int, env: dict | None = None,
                 start_new_session: bool = False):
        self.argv = argv
        self.max_capture_bytes = max_capture_bytes
        self.env = env
        self.start_new_session = start_new_session
        self.proc: asyncio.subprocess.Process | None = None
        self.cwd: str | None = None

    async def spawn(self) -> None:
        self.proc = await asyncio.create_subprocess_exec(
            *self.argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=self.env,
            start_new_session=self.start_new_session,
            limit=1 << 20,
        )
        self._send("exec 2>&1\n")

    def _send(self, text: str) -> None:
        assert self.proc and self.proc.stdin
        self.proc.stdin.write(text.encode())

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def execute(self, script_path: str) -> tuple[int | None, bytes, int, bool]:
        """Returns (exit_code, captured_output, total_bytes, truncated). Raises ShellDied."""
        nonce = new_id("done")
        marker = f"__SCITEAM_{nonce}__"
        q = shlex.quote(script_path)
        self._send(
            f"{{ source {q}; }} < /dev/null 2>&1; __sciteam_rc=$?; rm -f {q}; "
            f"printf '\\n{marker} %s %s\\n' \"$__sciteam_rc\" \"$(pwd)\"\n"
        )
        assert self.proc and self.proc.stdout
        await self.proc.stdin.drain()
        self._head = bytearray()
        self._tail = bytearray()
        self._total = 0
        self._pending = b""
        half = self.max_capture_bytes // 2
        mb = marker.encode()
        while True:
            chunk = await self.proc.stdout.read(65536)
            if not chunk:
                raise ShellDied(self.partial()[0].decode("utf-8", "replace"))
            self._pending += chunk
            idx = self._pending.find(mb)
            if idx >= 0:
                nl = self._pending.find(b"\n", idx)
                if nl < 0:
                    continue  # marker line not complete yet
                body = self._pending[:idx]
                if body.endswith(b"\n"):
                    body = body[:-1]
                line = self._pending[idx + len(mb):nl].decode("utf-8", "replace").strip()
                self._pending = b""
                code_s, _, cwd = line.partition(" ")
                self.cwd = cwd or self.cwd
                self._total += len(body)
                self._append(self._head, self._tail, body, half)
                try:
                    code = int(code_s)
                except ValueError:
                    code = None
                data, total, trunc = self.partial()
                return code, data, total, trunc
            # keep a margin so a marker split across reads is still found
            keep = len(mb) + 64
            if len(self._pending) > keep:
                flush, self._pending = self._pending[:-keep], self._pending[-keep:]
                self._total += len(flush)
                self._append(self._head, self._tail, flush, half)

    def partial(self) -> tuple[bytes, int, bool]:
        head = getattr(self, "_head", bytearray())
        tail = getattr(self, "_tail", bytearray())
        pending = getattr(self, "_pending", b"")
        total = getattr(self, "_total", 0) + len(pending)
        data = bytes(head) + bytes(tail) + pending
        return data, total, total > len(data)

    @staticmethod
    def _append(head: bytearray, tail: bytearray, data: bytes, half: int) -> None:
        room = half - len(head)
        if room > 0:
            head += data[:room]
            data = data[room:]
        if data:
            tail += data
            if len(tail) > half:
                del tail[: len(tail) - half]

    async def terminate(self) -> None:
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self.proc.wait(), 10)
            except TimeoutError:
                pass


def mark_truncation(data: bytes, total: int, truncated: bool, max_capture: int) -> str:
    text = data.decode("utf-8", "replace")
    if not truncated:
        return text
    half = max_capture // 2
    h = data[:half].decode("utf-8", "replace")
    t = data[half:].decode("utf-8", "replace")
    return f"{h}\n[... {total - len(data)} bytes dropped by sandbox capture limit ...]\n{t}"


class ShellSandboxBase:
    """Shared run() logic; subclasses provide spawn argv, file IO and kill-all."""

    def __init__(self, *, init_commands: list[str], max_capture_bytes: int, workdir: str):
        self.init_commands = init_commands
        self.max_capture_bytes = max_capture_bytes
        self._workdir = workdir
        self.shell: PersistentShell | None = None
        self._n = 0

    @property
    def workdir(self) -> str:
        return self._workdir

    def _shell_argv(self) -> list[str]:
        raise NotImplementedError

    def _shell_kwargs(self) -> dict:
        return {}

    async def _kill_all(self) -> None:
        raise NotImplementedError

    def _command_prelude(self) -> str:
        return ""

    async def write_file(self, path: str, data: bytes) -> None:
        raise NotImplementedError

    async def _spawn_shell(self, cwd: str | None) -> None:
        self.shell = PersistentShell(self._shell_argv(), max_capture_bytes=self.max_capture_bytes,
                                     **self._shell_kwargs())
        await self.shell.spawn()
        prelude = "\n".join(self.init_commands + [f"cd {shlex.quote(cwd or self._workdir)}"])
        try:
            res = await self._run_script(prelude, timeout_s=600)
        except ShellDied as e:
            raise RuntimeError(f"shell died during init: {str(e)[-2000:]}") from e
        if res.exit_code != 0:
            raise RuntimeError(f"shell init failed ({res.exit_code}): {res.output[-2000:]}")

    async def _run_script(self, command: str, *, timeout_s: float) -> ExecResult:
        self._n += 1
        path = f"/tmp/.sciteam_cmd_{os.getpid()}_{id(self):x}_{self._n}.sh"
        await self.write_file(path, (self._command_prelude() + command).encode())
        t0 = time.monotonic()
        assert self.shell
        try:
            code, data, total, trunc = await asyncio.wait_for(
                self.shell.execute(path), timeout=timeout_s
            )
        except TimeoutError:
            data, total, trunc = self.shell.partial()
            return ExecResult(exit_code=None, timed_out=True, duration_s=time.monotonic() - t0,
                              output=mark_truncation(data, total, trunc, self.max_capture_bytes),
                              output_bytes=total, capture_truncated=trunc)
        return ExecResult(
            exit_code=code,
            output=mark_truncation(data, total, trunc, self.max_capture_bytes),
            duration_s=time.monotonic() - t0,
            output_bytes=total,
            capture_truncated=trunc,
        )

    async def run(self, command: str, *, timeout_s: float) -> ExecResult:
        if self.shell is None or not self.shell.alive:
            await self._spawn_shell(self.shell.cwd if self.shell else None)
        cwd = self.shell.cwd
        try:
            res = await self._run_script(command, timeout_s=timeout_s)
        except ShellDied as e:
            await self._spawn_shell(cwd)
            return ExecResult(exit_code=None, output=str(e)[-self.max_capture_bytes:],
                              shell_restarted=True)
        if res.timed_out:
            await self._kill_all()
            if self.shell:
                await self.shell.terminate()
            await self._spawn_shell(cwd)
            res.shell_restarted = True
        return res
