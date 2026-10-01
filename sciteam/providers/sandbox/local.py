import os
import signal
from pathlib import Path

from sciteam.providers.sandbox.base import ShellSandboxBase


class LocalSandbox(ShellSandboxBase):
    """Runs in a host directory. For unit tests and trusted dev tasks only; no isolation."""

    def __init__(self, workdir: str | Path, *, init_commands: list[str] | None = None,
                 max_capture_bytes: int = 8 << 20):
        super().__init__(init_commands=init_commands or [], max_capture_bytes=max_capture_bytes,
                         workdir=str(workdir))

    def _shell_argv(self) -> list[str]:
        return ["bash", "--noprofile", "--norc"]

    def _shell_kwargs(self) -> dict:
        return {"start_new_session": True}

    async def start(self) -> None:
        Path(self._workdir).mkdir(parents=True, exist_ok=True)
        await self._spawn_shell(None)

    async def _kill_all(self) -> None:
        if self.shell and self.shell.proc and self.shell.proc.returncode is None:
            try:
                os.killpg(self.shell.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def _abs(self, path: str) -> Path:
        p = Path(path)
        return p if p.is_absolute() else Path(self._workdir) / p

    async def read_file(self, path: str) -> bytes:
        return self._abs(path).read_bytes()

    async def write_file(self, path: str, data: bytes) -> None:
        p = self._abs(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    async def close(self) -> None:
        await self._kill_all()
        if self.shell:
            await self.shell.terminate()
