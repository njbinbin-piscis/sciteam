"""Validation worker (docs/40-institution.md §7): export the evidence commit to a clean
directory, run the validator command in a fresh sandbox, record the result. Never calls a model."""

import asyncio
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path

from sciteam.institution.service import InstitutionService
from sciteam.institution.view import Validation
from sciteam.kernel.blobs import BlobStore
from sciteam.providers.sandbox.base import Sandbox

SandboxFactory = Callable[[Path], Sandbox]     # clean directory -> sandbox mounting it
TAIL_CHARS = 3000
INFRA_ATTEMPTS = 3


async def export_commit(workspace: Path, sha: str, dest: Path) -> None:
    git = await asyncio.create_subprocess_exec(
        "git", "-C", str(workspace), "archive", "--format=tar", sha,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    data, err = await git.communicate()
    if git.returncode != 0:
        raise RuntimeError(f"git archive {sha[:10]} failed: {err.decode()[-500:]}")
    tar = await asyncio.create_subprocess_exec(
        "tar", "-x", "-C", str(dest), stdin=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE)
    _, err = await tar.communicate(data)
    if tar.returncode != 0:
        raise RuntimeError(f"tar failed: {err.decode()[-500:]}")


class ValidationWorker:
    def __init__(self, svc: InstitutionService, *, workspace: Path, blobs: BlobStore,
                 sandbox: SandboxFactory, max_parallel: int = 2):
        self.svc, self.workspace, self.blobs = svc, workspace, blobs
        self.sandbox, self.max_parallel = sandbox, max_parallel
        self._running: dict[str, asyncio.Task] = {}
        self._attempts: dict[str, int] = {}

    def busy(self) -> bool:
        return bool(self._running) or any(v.status == "requested"
                                          for v in self.svc.inst.validations.values())

    def step(self) -> None:
        for v in list(self.svc.inst.validations.values()):
            if v.status != "requested" or v.id in self._running:
                continue
            hit = self._cached(v)
            if hit is not None:
                self.svc.record_validation(v.id, ok=hit.status == "passed",
                                           exit_code=hit.exit_code, wall_s=0.0,
                                           output_blob=hit.output_blob, tail=hit.tail,
                                           cached_from=hit.id)
                continue
            if len(self._running) >= self.max_parallel:
                break
            task = asyncio.get_running_loop().create_task(self._run(v))
            self._running[v.id] = task
            task.add_done_callback(lambda _t, vid=v.id: self._running.pop(vid, None))

    def _cached(self, v: Validation) -> Validation | None:
        return next((o for o in self.svc.inst.validations.values()
                     if o.id != v.id and o.status in ("passed", "failed") and
                     (o.validator, o.commit, o.command) == (v.validator, v.commit, v.command)),
                    None)

    async def _run(self, v: Validation) -> None:
        tmp = Path(tempfile.mkdtemp(prefix=f"sciteam-{v.id}-"))
        sb: Sandbox | None = None
        try:
            await export_commit(self.workspace, v.commit, tmp)
            sb = self.sandbox(tmp)
            await sb.start()
            res = await sb.run(v.command, timeout_s=v.timeout_s)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # infrastructure, not the submission: retry, then record as such
            n = self._attempts[v.id] = self._attempts.get(v.id, 0) + 1
            if n >= INFRA_ATTEMPTS:
                self.svc.record_validation(v.id, ok=False, exit_code=None, wall_s=0.0,
                                           output_blob=None, infra=True,
                                           tail=f"validation infrastructure error: {e}"[-1000:])
            return
        finally:
            if sb is not None:
                await sb.close()
            shutil.rmtree(tmp, ignore_errors=True)
        out = res.output
        if res.timed_out:
            out += f"\n[timed out after {v.timeout_s:.0f}s]"
        ref = self.blobs.put_text(out) if out else None
        self.svc.record_validation(
            v.id, ok=res.exit_code == 0 and not res.timed_out, exit_code=res.exit_code,
            wall_s=res.duration_s, output_blob=ref.sha256 if ref else None,
            tail=out[-TAIL_CHARS:])

    async def close(self) -> None:
        for t in list(self._running.values()):
            t.cancel()
        if self._running:
            await asyncio.gather(*self._running.values(), return_exceptions=True)
