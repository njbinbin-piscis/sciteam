import asyncio
import atexit
import subprocess

from pydantic import BaseModel, Field

from sciteam.kernel.ids import new_id
from sciteam.providers.sandbox.base import ShellSandboxBase

_LIVE_CONTAINERS: set[str] = set()


def _cleanup_at_exit() -> None:
    for cid in list(_LIVE_CONTAINERS):
        subprocess.run(["docker", "rm", "-f", cid], capture_output=True, timeout=60)


atexit.register(_cleanup_at_exit)


class DockerSandboxConfig(BaseModel):
    image: str
    workdir: str = "/"
    network: str = "none"
    memory: str | None = "16g"
    cpus: float | None = 4
    init_commands: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    pull_timeout_s: float = 3600
    max_capture_bytes: int = 8 << 20
    labels: dict[str, str] = Field(default_factory=dict)
    platform: str | None = "linux/amd64"
    # Attach to a container owned by someone else (e.g. a Harbor trial). The container is not
    # removed on close, and timeouts kill only processes our shell started during that command.
    attach_container: str | None = None
    user: str | None = None
    mounts: list[str] = Field(default_factory=list)  # docker -v host:container[:ro]


# Kills descendants of the recorded shell that share its session and started at/after the
# recorded command start; long-lived processes from earlier commands or setsid survive.
# Only bash builtins and /proc, since task images may lack ps/pgrep.
_KILL_COMMAND_TREE = r'''
shell=$(<"$1") || exit 0
up=$(<"$2") || up=0
tck=$(getconf CLK_TCK 2>/dev/null || echo 100)
sec=${up%.*}; frac=${up#*.}; [ "$frac" = "$up" ] && frac=0
ticks=$(( sec * tck + 10#${frac:0:2} * tck / 100 ))
field() {
  local s; read -r s < "/proc/$1/stat" 2>/dev/null || return 1
  s=${s##*) }; set -- $s; F_PPID=$2; F_SID=$4; F_START=${20}
}
field "$shell" || exit 0
sid=$F_SID
for d in /proc/[0-9]*; do
  p=${d#/proc/}
  [ "$p" = "$shell" ] && continue
  field "$p" || continue
  [ "$F_SID" = "$sid" ] || continue
  [ "$F_START" -ge "$ticks" ] || continue
  a=$F_PPID
  while [ "$a" -gt 1 ] && [ "$a" != "$shell" ]; do field "$a" || break; a=$F_PPID; done
  [ "$a" = "$shell" ] && kill -9 "$p" 2>/dev/null
done
kill -9 "$shell" 2>/dev/null
exit 0
'''


async def _docker(*args: str, input: bytes | None = None, timeout: float = 300) -> tuple[int, bytes]:
    proc = await asyncio.create_subprocess_exec(
        "docker", *args,
        stdin=asyncio.subprocess.PIPE if input is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(input), timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode, out


class DockerSandbox(ShellSandboxBase):
    def __init__(self, cfg: DockerSandboxConfig):
        super().__init__(init_commands=cfg.init_commands, max_capture_bytes=cfg.max_capture_bytes,
                         workdir=cfg.workdir)
        self.cfg = cfg
        self.container_id: str | None = None
        self._tag = new_id("sh")
        self._pid_file = f"/tmp/.sciteam_{self._tag}.pid"
        self._start_file = f"/tmp/.sciteam_{self._tag}.start"

    @property
    def _attached(self) -> bool:
        return self.cfg.attach_container is not None

    def _exec_prefix(self, *, stdin: bool) -> list[str]:
        assert self.container_id
        args = ["exec"] + (["-i"] if stdin else []) + ["-w", self._workdir]
        if self.cfg.user:
            args += ["-u", self.cfg.user]
        if self._attached:
            for k, v in self.cfg.env.items():
                args += ["-e", f"{k}={v}"]
        return args + [self.container_id]

    def _shell_argv(self) -> list[str]:
        base = ["docker", *self._exec_prefix(stdin=True)]
        if self._attached:
            return base + ["bash", "-c", f'echo $$ > {self._pid_file}; exec bash --noprofile --norc']
        return base + ["bash", "--noprofile", "--norc"]

    def _command_prelude(self) -> str:
        if not self._attached:
            return ""
        return f"read -r __sciteam_up _ < /proc/uptime; printf '%s' \"$__sciteam_up\" > {self._start_file}\n"

    async def _ensure_image(self) -> None:
        code, _ = await _docker("image", "inspect", self.cfg.image, timeout=60)
        if code == 0:
            return
        args = ["pull", self.cfg.image]
        if self.cfg.platform:
            args[1:1] = ["--platform", self.cfg.platform]
        code, out = await _docker(*args, timeout=self.cfg.pull_timeout_s)
        if code != 0:
            raise RuntimeError(f"docker pull failed: {out.decode(errors='replace')[-2000:]}")

    async def start(self) -> None:
        if self._attached:
            self.container_id = self.cfg.attach_container
            await self._spawn_shell(None)
            return
        try:
            await self._ensure_image()
            args = ["run", "-d", "--network", self.cfg.network, "-w", self._workdir,
                    "--label", "sciteam=1"]
            for k, v in self.cfg.labels.items():
                args += ["--label", f"{k}={v}"]
            for k, v in self.cfg.env.items():
                args += ["-e", f"{k}={v}"]
            for m in self.cfg.mounts:
                args += ["-v", m]
            if self.cfg.memory:
                args += ["--memory", self.cfg.memory]
            if self.cfg.cpus:
                args += ["--cpus", str(self.cfg.cpus)]
            if self.cfg.platform:
                args += ["--platform", self.cfg.platform]
            args += [self.cfg.image, "sleep", "infinity"]
            code, out = await _docker(*args, timeout=300)
            if code != 0:
                raise RuntimeError(f"docker run failed: {out.decode(errors='replace')[-2000:]}")
            self.container_id = out.decode().strip().splitlines()[-1]
            _LIVE_CONTAINERS.add(self.container_id)
            await self._spawn_shell(None)
        except BaseException:
            await self.close()
            raise

    async def _kill_all(self) -> None:
        if not self.container_id:
            return
        if self._attached:
            await _docker("exec", "-u", "root", self.container_id, "bash", "-c", _KILL_COMMAND_TREE,
                          "_", self._pid_file, self._start_file, timeout=60)
            return
        # kill -9 -1 signals every process except init (sleep infinity) and itself.
        await _docker("exec", self.container_id, "sh", "-c", "kill -9 -1", timeout=60)

    async def read_file(self, path: str) -> bytes:
        code, out = await _docker(*self._exec_prefix(stdin=False), "cat", "--", path, timeout=120)
        if code != 0:
            raise FileNotFoundError(out.decode(errors="replace").strip() or path)
        return out

    async def write_file(self, path: str, data: bytes) -> None:
        script = 'mkdir -p "$(dirname -- "$1")" && cat > "$1"'
        code, out = await _docker(*self._exec_prefix(stdin=True), "sh", "-c", script, "_", path,
                                  input=data, timeout=120)
        if code != 0:
            raise OSError(out.decode(errors="replace").strip())

    async def exec_once(self, command: str, *, timeout: float = 600) -> tuple[int, str]:
        """Out-of-band command (not via the model's shell), e.g. for patch extraction."""
        code, out = await _docker(*self._exec_prefix(stdin=False), "bash", "-lc", command,
                                  timeout=timeout)
        return code, out.decode("utf-8", "replace")

    async def close(self) -> None:
        if self.shell:
            await self.shell.terminate()
        if self._attached:
            if self.container_id:
                # Only the shell: processes the agent left running may be what the verifier checks.
                script = 'kill -9 "$(<"$1")" 2>/dev/null; rm -f "$1" "$2"'
                await _docker("exec", "-u", "root", self.container_id, "bash", "-c", script, "_",
                              self._pid_file, self._start_file, timeout=60)
            self.container_id = None
            return
        if self.container_id:
            await _docker("rm", "-f", self.container_id, timeout=120)
            _LIVE_CONTAINERS.discard(self.container_id)
            self.container_id = None
