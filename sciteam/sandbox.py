"""Restricted, workspace-scoped Python execution port."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

#  B4: raised from the original 120s/64 fds/64KB — those bounds were tight
# enough to make realistic build/eval steps look like sandbox failures
# rather than program failures. Still bounded, just no longer punitive.
MAX_TIMEOUT_SECONDS = 600
MAX_NOFILE = 256


@dataclass(frozen=True)
class SandboxRequest:
    program: str
    inputs: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()
    arguments: tuple[str, ...] = ()
    timeout_seconds: int = 30
    memory_mb: int = 512
    max_output_bytes: int = 200_000


@dataclass(frozen=True)
class SandboxResult:
    ok: bool
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    output_hashes: dict[str, str] = field(default_factory=dict)
    environment_fingerprint: str = ""
    reason_code: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@runtime_checkable
class SandboxPort(Protocol):
    def run(self, request: SandboxRequest) -> SandboxResult: ...

    def certify(self, request: SandboxRequest) -> SandboxResult: ...


_WRAPPER = r"""
import builtins, runpy, sys
blocked = {"socket", "subprocess", "urllib", "http", "requests", "httpx", "ctypes"}
real_import = builtins.__import__
def guarded_import(name, *args, **kwargs):
    if name.split(".", 1)[0] in blocked:
        raise PermissionError("sandbox import denied: " + name)
    return real_import(name, *args, **kwargs)
builtins.__import__ = guarded_import
program, *argv = sys.argv[1:]
sys.argv = [program, *argv]
runpy.run_path(program, run_name="__main__")
"""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class LocalPythonSandbox:
    """Subprocess sandbox with no shell, bounded resources and path confinement."""

    def __init__(self, workspace: Path | str) -> None:
        self.workspace = Path(workspace).resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)

    def _path(self, raw: str, *, must_exist: bool = False) -> Path:
        candidate = (self.workspace / str(raw)).resolve()
        try:
            candidate.relative_to(self.workspace)
        except ValueError as exc:
            raise PermissionError(f"sandbox path escapes workspace: {raw}") from exc
        if must_exist and not candidate.is_file():
            raise FileNotFoundError(f"sandbox input missing: {raw}")
        return candidate

    @staticmethod
    def _limits(memory_mb: int, timeout_seconds: int):
        def apply() -> None:
            import resource

            mem = max(32, int(memory_mb)) * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
            cpu = max(1, int(timeout_seconds))
            resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 1))
            resource.setrlimit(resource.RLIMIT_NOFILE, (MAX_NOFILE, MAX_NOFILE))

        return apply

    def run(self, request: SandboxRequest) -> SandboxResult:
        try:
            program = self._path(request.program, must_exist=True)
            for raw in request.inputs:
                self._path(raw, must_exist=True)
            output_paths = [self._path(raw) for raw in request.outputs]
        except (OSError, PermissionError) as exc:
            return SandboxResult(
                ok=False,
                exit_code=None,
                stdout="",
                stderr=str(exc),
                reason_code="sandbox_path_denied",
            )

        env = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONHASHSEED": "0",
            "SCITEAM_SANDBOX": "1",
            "HOME": str(self.workspace),
        }
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "python": sys.version,
                    "platform": platform.platform(),
                    "program_sha256": _sha256(program),
                    "network": "denied",
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        command = [
            sys.executable,
            "-I",
            "-c",
            _WRAPPER,
            str(program),
            *[str(x) for x in request.arguments],
        ]
        try:
            completed = subprocess.run(
                command,
                cwd=self.workspace,
                env=env,
                capture_output=True,
                text=False,
                timeout=max(1, min(int(request.timeout_seconds), MAX_TIMEOUT_SECONDS)),
                preexec_fn=self._limits(request.memory_mb, request.timeout_seconds),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return SandboxResult(
                ok=False,
                exit_code=None,
                stdout=(exc.stdout or b"")[: request.max_output_bytes].decode(errors="replace"),
                stderr=(exc.stderr or b"")[: request.max_output_bytes].decode(errors="replace"),
                timed_out=True,
                environment_fingerprint=fingerprint,
                reason_code="sandbox_timeout",
            )

        stdout = completed.stdout[: request.max_output_bytes].decode(errors="replace")
        stderr = completed.stderr[: request.max_output_bytes].decode(errors="replace")
        hashes = {
            str(path.relative_to(self.workspace)): _sha256(path)
            for path in output_paths
            if path.is_file()
        }
        return SandboxResult(
            ok=completed.returncode == 0,
            exit_code=completed.returncode,
            stdout=stdout,
            stderr=stderr,
            output_hashes=hashes,
            environment_fingerprint=fingerprint,
            reason_code="" if completed.returncode == 0 else "sandbox_program_failed",
        )

    def certify(self, request: SandboxRequest) -> SandboxResult:
        """Run the model; certification is provenance, not an official metric."""
        return self.run(request)


def request_from_arguments(arguments: dict) -> SandboxRequest:
    defaults = SandboxRequest(program="")
    return SandboxRequest(
        program=str(arguments.get("program") or ""),
        inputs=tuple(str(x) for x in (arguments.get("inputs") or [])),
        outputs=tuple(str(x) for x in (arguments.get("outputs") or [])),
        arguments=tuple(str(x) for x in (arguments.get("arguments") or [])),
        timeout_seconds=int(arguments.get("timeout_seconds") or defaults.timeout_seconds),
        memory_mb=int(arguments.get("memory_mb") or defaults.memory_mb),
        max_output_bytes=int(arguments.get("max_output_bytes") or defaults.max_output_bytes),
    )
