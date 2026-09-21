"""Docker container sandbox implementing SciTeam's SandboxPort."""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from sciteam.sandbox import SandboxPort, SandboxRequest, SandboxResult

from sciteam_sandbox_docker.guard import ContainerGuardSpec

logger = logging.getLogger(__name__)

RunnerFunc = Callable[[list[str], int], tuple[int, bytes, bytes, bool]]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class DockerSandbox(SandboxPort):
    """Isolated, containerized Python sandbox driven by Docker."""

    def __init__(
        self,
        workspace: Path | str,
        *,
        guard: ContainerGuardSpec | None = None,
        runner: RunnerFunc | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.guard = guard or ContainerGuardSpec()
        self._runner = runner

    def _path(self, raw: str, *, must_exist: bool = False) -> Path:
        candidate = (self.workspace / str(raw)).resolve()
        try:
            candidate.relative_to(self.workspace)
        except ValueError as exc:
            raise PermissionError(f"sandbox path escapes workspace: {raw}") from exc
        if must_exist and not candidate.is_file():
            raise FileNotFoundError(f"sandbox input missing: {raw}")
        return candidate

    def build_docker_command(self, request: SandboxRequest) -> list[str]:
        """Construct the hermetic 'docker run' argument list."""
        program = self._path(request.program, must_exist=True)
        rel_program = program.relative_to(self.workspace)

        cmd = [
            "docker",
            "run",
            "--rm",
            f"--network={self.guard.network}",
            f"--memory={self.guard.memory_mb}m",
            f"--cpus={self.guard.cpus}",
            f"--pids-limit={self.guard.pids_limit}",
        ]
        for cap in self.guard.cap_drop:
            cmd.append(f"--cap-drop={cap}")

        if self.guard.read_only_root:
            cmd.append("--read-only")
            cmd.extend(["--tmpfs", "/tmp:rw,noexec,nosuid,size=64m"])

        cmd.extend(
            [
                "-v",
                f"{self.workspace}:/workspace:rw",
                "-w",
                "/workspace",
                self.guard.image,
                "python",
                "-I",
                f"/workspace/{rel_program}",
            ]
        )
        cmd.extend(str(arg) for arg in request.arguments)
        return cmd

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

        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "container_image": self.guard.image,
                    "network": self.guard.network,
                    "program_sha256": _sha256(program),
                    "memory_mb": self.guard.memory_mb,
                    "cpus": self.guard.cpus,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()

        timeout = max(1, min(int(request.timeout_seconds), self.guard.timeout_seconds))

        if self._runner is not None:
            cmd = self.build_docker_command(request)
            returncode, stdout_bytes, stderr_bytes, timed_out = self._runner(cmd, timeout)
        else:
            if shutil.which("docker") is None:
                return SandboxResult(
                    ok=False,
                    exit_code=None,
                    stdout="",
                    stderr="docker executable not found in PATH",
                    environment_fingerprint=fingerprint,
                    reason_code="docker_daemon_unavailable",
                )
            cmd = self.build_docker_command(request)
            try:
                completed = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=timeout,
                    check=False,
                )
                returncode = completed.returncode
                stdout_bytes = completed.stdout
                stderr_bytes = completed.stderr
                timed_out = False
            except subprocess.TimeoutExpired as exc:
                returncode = None
                stdout_bytes = exc.stdout or b""
                stderr_bytes = exc.stderr or b""
                timed_out = True

        stdout = stdout_bytes[: request.max_output_bytes].decode(errors="replace")
        stderr = stderr_bytes[: request.max_output_bytes].decode(errors="replace")

        if timed_out:
            return SandboxResult(
                ok=False,
                exit_code=None,
                stdout=stdout,
                stderr=stderr,
                timed_out=True,
                environment_fingerprint=fingerprint,
                reason_code="sandbox_timeout",
            )

        hashes = {
            str(path.relative_to(self.workspace)): _sha256(path)
            for path in output_paths
            if path.is_file()
        }

        return SandboxResult(
            ok=returncode == 0,
            exit_code=returncode,
            stdout=stdout,
            stderr=stderr,
            output_hashes=hashes,
            environment_fingerprint=fingerprint,
            reason_code="" if returncode == 0 else "sandbox_program_failed",
        )

    def certify(self, request: SandboxRequest) -> SandboxResult:
        """Run inside container; certification is provenance, not an official metric."""
        return self.run(request)
