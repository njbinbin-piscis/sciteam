"""Typed acceptance specs and computed claims (docs/09 §4). A spec names the quantities a task
must report and where its reproduction writes them; a claim reports one value on one commit.
Replaying a claim exports the commit to a clean directory, runs the spec's reproduction command
in the validation sandbox, reads the value by JSON Pointer and compares. Never calls a model."""

import asyncio
import json
import math
import shlex
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sciteam.institution.validation import (TAIL_CHARS, SandboxFactory, export_commit,
                                            uncommitted)

# Closed outcome codes of a replay; only "ok" counts as replayed.
REPLAY_CODES = ("ok", "export_failed", "reproduce_failed", "file_missing", "not_json",
                "pointer_missing", "not_number", "mismatch", "out_of_range", "infra")
INFRA_ATTEMPTS = 3


class ClaimSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    file: str                     # JSON file the reproduction writes, relative to the repo root
    pointer: str                  # RFC 6901 JSON Pointer to a number in that file
    description: str = ""
    min: float | None = None      # plausible range; a value outside it fails the replay
    max: float | None = None

    @model_validator(mode="after")
    def _check(self):
        if self.pointer and not self.pointer.startswith("/"):
            raise ValueError(f"claim {self.name}: pointer must be empty or start with '/'")
        if Path(self.file).is_absolute() or ".." in Path(self.file).parts:
            raise ValueError(
                f"claim {self.name}: file must be a path inside the repository, relative to its "
                f"root: the reproduce command runs at the root of a clean, writable export of "
                f"the commit (also when the command itself lives in /private/<you>/), so have "
                f"it write the JSON there, e.g. file='replication.json'")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError(f"claim {self.name}: min is above max")
        return self


class AcceptanceSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reproduce: str                # shell command that regenerates the outputs from the code
    claims: list[ClaimSpec] = Field(min_length=1)
    deliverables: list[str] = Field(default_factory=list)
    rel_tol: float = Field(default=1e-6, ge=0)
    abs_tol: float = Field(default=1e-9, ge=0)
    timeout_s: float = Field(default=1800, gt=0)
    notes: str = ""

    @model_validator(mode="after")
    def _unique(self):
        names = [c.name for c in self.claims]
        dup = sorted({n for n in names if names.count(n) > 1})
        if dup:
            raise ValueError(f"duplicate claim names {dup}")
        if not self.reproduce.strip():
            raise ValueError("reproduce must be a command")
        return self

    def claim(self, name: str) -> ClaimSpec | None:
        return next((c for c in self.claims if c.name == name), None)


def parse_spec(raw: Any) -> AcceptanceSpec:
    """A spec given as an object or as JSON text."""
    if isinstance(raw, str):
        raw = json.loads(raw)
    return AcceptanceSpec.model_validate(raw)


def render_spec(spec: AcceptanceSpec) -> str:
    lines = [f"reproduce: `{spec.reproduce}` (tolerance rel {spec.rel_tol:g}, "
             f"abs {spec.abs_tol:g})"]
    for c in spec.claims:
        rng = "" if c.min is None and c.max is None else \
            f" in [{'-inf' if c.min is None else f'{c.min:g}'}, " \
            f"{'inf' if c.max is None else f'{c.max:g}'}]"
        lines.append(f"- {c.name}: {c.file}#{c.pointer}{rng}"
                     + (f" — {c.description}" if c.description else ""))
    if spec.deliverables:
        lines.append(f"deliverables: {', '.join(spec.deliverables)}")
    return "\n".join(lines)


# ---- pointer and comparison ------------------------------------------------------------

class PointerMissing(KeyError):
    pass


def resolve_pointer(doc: Any, pointer: str) -> Any:
    if pointer == "":
        return doc
    cur, at = doc, ""
    for raw in pointer[1:].split("/"):
        tok = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(cur, dict) and tok in cur:
            cur = cur[tok]
        elif isinstance(cur, list) and tok.isdigit() and int(tok) < len(cur):
            cur = cur[int(tok)]
        else:
            raise PointerMissing(f"{pointer}: no '{tok}' at {at or '/'}, which holds "
                                 + _shape(cur))
        at += "/" + raw
    return cur


def _shape(v: Any, limit: int = 12) -> str:
    if isinstance(v, dict):
        keys = [str(k) for k in v][:limit]
        return "keys " + ", ".join(keys) + (" …" if len(v) > limit else "") if v else "{}"
    if isinstance(v, list):
        return f"a list of {len(v)}"
    return f"the value {v!r}"[:80]


def as_number(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


def compare(observed: Any, claimed: float, c: ClaimSpec, spec: AcceptanceSpec) -> str:
    x = as_number(observed)
    if x is None:
        return "not_number"
    if not math.isclose(x, claimed, rel_tol=spec.rel_tol, abs_tol=spec.abs_tol):
        return "mismatch"
    if (c.min is not None and x < c.min) or (c.max is not None and x > c.max):
        return "out_of_range"
    return "ok"


def preview_spec(spec: AcceptanceSpec, texts: dict[str, str | None]) -> list[str]:
    """What the workspace's current output files hold at each claim's pointer; `texts` maps
    each claim file to its content, None when absent."""
    out: list[str] = []
    for c in spec.claims:
        text = texts.get(c.file)
        if text is None:
            out.append(f"{c.name}: {c.file} is not in the workspace yet")
            continue
        try:
            v = resolve_pointer(json.loads(text), c.pointer)
        except (ValueError, UnicodeDecodeError):
            out.append(f"{c.name}: {c.file} is not JSON")
            continue
        except PointerMissing as e:
            out.append(f"{c.name}: {c.file} {e.args[0]}")
            continue
        x = as_number(v)
        if x is None:
            out.append(f"{c.name}: {c.file}{c.pointer} holds {_shape(v)}, which is not a number")
            continue
        outside = (c.min is not None and x < c.min) or (c.max is not None and x > c.max)
        out.append(f"{c.name}: {c.file}{c.pointer} = {x:g}" + (
            f", outside the range [{c.min:g}, {c.max:g}]" if outside and None not in (c.min, c.max)
            else ", outside the range" if outside else ""))
    return out


def read_claim(root: Path, c: ClaimSpec) -> tuple[str, Any]:
    """(code, value) of a claim's quantity in a reproduced tree; code "ok" = value read."""
    path = root / c.file
    if not path.is_file():
        return "file_missing", None
    try:
        doc = json.loads(path.read_text())
    except (ValueError, UnicodeDecodeError):
        return "not_json", None
    try:
        return "ok", resolve_pointer(doc, c.pointer)
    except PointerMissing as e:
        return "pointer_missing", e.args[0]


def render_counterexample(claim, cx) -> str:
    """What an author receives when a claim is rejected: the counterexample alone — command,
    commit, the value the institution replayed, and the private sources the command ran."""
    a = cx.adhoc or {}
    lines = [f"claim {claim.name} ({claim.id}) = {claim.value:g} on commit {claim.commit[:10]} "
             f"has a counterexample {cx.id}:",
             f"  reproduce: `{a.get('reproduce', '')}` on commit {cx.commit[:10]}",
             f"  it writes {a.get('file', '')}#{a.get('pointer', '')} = {cx.observed!r} "
             f"(replayed by the institution in a clean export)"]
    for path, text in (cx.sources or {}).items():
        lines += [f"  --- {path} ---", text]
    lines.append("Reproduce it yourself, fix the work, record the claims on the new commit and "
                 "submit again.")
    return "\n".join(lines)[:6000]


def adhoc_spec(spec: AcceptanceSpec, *claims) -> AcceptanceSpec:
    """One reproduction of a member's own command reads every claim recorded against it."""
    a = claims[0].adhoc
    return AcceptanceSpec(reproduce=a["reproduce"], rel_tol=spec.rel_tol, abs_tol=spec.abs_tol,
                          timeout_s=spec.timeout_s,
                          claims=[ClaimSpec(name=c.name, file=c.adhoc["file"],
                                            pointer=c.adhoc["pointer"]) for c in claims])


def private_sources(command: str, private: tuple[Path, str] | None) -> dict[str, str]:
    """Text of the private-directory files a command names, as the institution saw them."""
    if private is None:
        return {}
    root, base = private[0], private[1].rstrip("/") + "/"
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    out: dict[str, str] = {}
    for tok in tokens:
        if not tok.startswith(base):
            continue
        host = root / tok[len(base):]
        try:
            if host.is_file() and host.stat().st_size <= 20000:
                out[tok] = host.read_text(errors="replace")[:6000]
        except OSError:
            continue
    return out


# ---- worker -----------------------------------------------------------------------------

@dataclass
class Replay:
    code: str
    observed: Any = None
    exit_code: int | None = None
    tail: str = ""
    infra: bool = False


async def replay(workspace: Path, commit: str, spec: AcceptanceSpec,
                 claims: list[tuple[str, float]], sandbox: SandboxFactory,
                 name_uncommitted: bool = True) -> dict[str, Replay]:
    """Replay several claims of one spec on one commit with a single reproduction run.
    `name_uncommitted`: on failure, list the workspace files the commit lacks (useful to the
    author; a reviewer's reproduction lives in their private directory)."""
    tmp = Path(tempfile.mkdtemp(prefix="sciteam-claims-"))
    sb = None
    try:
        try:
            await export_commit(workspace, commit, tmp)
        except RuntimeError as e:
            return {n: Replay("export_failed", tail=str(e)[-TAIL_CHARS:]) for n, _ in claims}
        # a claimed value counts only when this run writes it
        committed = {f for f in {spec.claim(n).file for n, _ in claims} if (tmp / f).is_file()}
        for f in committed:
            (tmp / f).unlink()
        sb = sandbox(tmp)
        await sb.start()
        res = await sb.run(spec.reproduce, timeout_s=spec.timeout_s)
        tail = res.output[-TAIL_CHARS:] + (f"\n[timed out after {spec.timeout_s:.0f}s]"
                                           if res.timed_out else "")
        if res.exit_code != 0 or res.timed_out:
            missing = await uncommitted(workspace, commit) if name_uncommitted else []
            if missing:
                tail += (f"\n[commit {commit[:10]} lacks {', '.join(missing)} from the workspace; "
                         "commit them and record the claim on the new commit]")
            if "Read-only file system" in res.output:
                files = ", ".join(sorted({spec.claim(n).file for n, _ in claims}))
                tail += ("\n[the replay runs in a fresh, writable export of the commit as the "
                         "current directory; private directories are mounted read-only there. "
                         f"Have the command write {files} relative to the current directory, "
                         "then record the claim again]")
            return {n: Replay("reproduce_failed", exit_code=res.exit_code, tail=tail)
                    for n, _ in claims}
        out = {}
        for name, value in claims:
            c = spec.claim(name)
            code, observed = read_claim(tmp, c)
            if code == "pointer_missing":
                out[name] = Replay(code, exit_code=res.exit_code,
                                   tail=f"{tail}\n[{c.file} {observed}]".lstrip("\n"))
                continue
            if code == "file_missing":
                found = sorted(str(p.relative_to(tmp)) for p in tmp.rglob("*.json"))
                note = (f"[the reproduction left no file {c.file} at the repository root; JSON "
                        f"files there: {', '.join(found[:8]) or 'none'}. The claim reads a JSON "
                        "file the reproduction writes, so write the result to one and name it]")
                if c.file in committed:
                    note += (f"\n[{c.file} is in the commit, and the replay removes it before "
                             "running so that the value comes from this run: have the reproduce "
                             f"command write {c.file} relative to the current directory]")
                out[name] = Replay(code, exit_code=res.exit_code,
                                   tail=f"{tail}\n{note}".lstrip("\n"))
                continue
            if code == "ok":
                code = compare(observed, value, c, spec)
            out[name] = Replay(code, observed=observed if as_number(observed) is not None
                               else repr(observed)[:200] if observed is not None else None,
                               exit_code=res.exit_code, tail=tail)
        return out
    finally:
        if sb is not None:
            await sb.close()
        shutil.rmtree(tmp, ignore_errors=True)


class ClaimWorker:
    """Replays requested claims; claims on the same commit and spec share one run."""

    def __init__(self, svc, *, workspace: Path, sandbox: SandboxFactory,
                 max_parallel: int = 1, private: tuple[Path, str] | None = None):
        self.svc, self.workspace, self.sandbox = svc, workspace, sandbox
        self.private = private
        self.max_parallel = max_parallel
        self._running: dict[tuple, asyncio.Task] = {}
        self._attempts: dict[tuple, int] = {}

    def _sandbox_for(self, root: Path, member: str | None):
        """An ad-hoc reproduction runs a member's private script, so the replay mounts that
        member's private directory; a spec reproduction runs on the commit alone."""
        return self.sandbox(root, member) if member else self.sandbox(root)

    def busy(self) -> bool:
        return bool(self._running) or any(c.status == "requested"
                                          for c in self.svc.inst.claims.values())

    def step(self) -> None:
        groups: dict[tuple, list] = {}
        for c in self.svc.inst.claims.values():
            if c.status != "requested":
                continue
            own = (c.actor, c.adhoc["reproduce"]) if c.adhoc else ()
            n = 0
            while any(o.name == c.name for o in groups.get((c.commit, c.spec_sha, own, n), [])):
                n += 1
            groups.setdefault((c.commit, c.spec_sha, own, n), []).append(c)
        for key, claims in groups.items():
            if key in self._running or len(self._running) >= self.max_parallel:
                continue
            t = asyncio.get_running_loop().create_task(self._run(key, claims))
            self._running[key] = t
            t.add_done_callback(lambda _t, k=key: self._running.pop(k, None))

    async def _run(self, key: tuple, claims: list) -> None:
        spec = self.svc.inst.spec_by_sha.get(claims[0].spec_sha)
        if claims[0].adhoc:
            spec = adhoc_spec(spec, *claims)
        try:
            actor = claims[0].actor if claims[0].adhoc else None
            results = await replay(self.workspace, claims[0].commit, spec,
                                   [(c.name, c.value) for c in claims],
                                   lambda root: self._sandbox_for(root, actor),
                                   name_uncommitted=not claims[0].adhoc)
        except asyncio.CancelledError:
            raise
        except Exception as e:    # the sandbox, not the claim: retry, then record as such
            n = self._attempts[key] = self._attempts.get(key, 0) + 1
            if n < INFRA_ATTEMPTS:
                return
            results = {c.name: Replay("infra", tail=f"replay infrastructure error: {e}"[-1000:],
                                      infra=True) for c in claims}
        for c in claims:
            r = results[c.name]
            sources = private_sources(spec.reproduce, self.private) if c.adhoc else {}
            self.svc.record_replay(c.id, code=r.code, observed=r.observed,
                                   exit_code=r.exit_code, tail=r.tail, sources=sources)

    async def close(self) -> None:
        for t in list(self._running.values()):
            t.cancel()
        if self._running:
            await asyncio.gather(*self._running.values(), return_exceptions=True)


async def replicate(svc, claims: list, *, workspace: Path,
                    sandbox: SandboxFactory) -> list[str]:
    """Replay accepted claims again; returns the defect records written for mismatches."""
    groups: dict[tuple, list] = {}
    for c in claims:
        groups.setdefault((c.commit, c.spec_sha), []).append(c)
    defects = []
    for (commit, sha), cs in groups.items():
        spec = svc.inst.spec_by_sha.get(sha)
        if spec is None:
            continue
        try:
            results = await replay(workspace, commit, spec, [(c.name, c.value) for c in cs],
                                   sandbox)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            results = {c.name: Replay("infra", tail=f"replication infrastructure error: {e}",
                                      infra=True) for c in cs}
        for c in cs:
            r = results[c.name]
            d = svc.record_replication(c.id, code=r.code, observed=r.observed, tail=r.tail)
            if d:
                defects.append(d)
    return defects


class ReplicationWorker:
    """Replays the claims of accepted tasks once each has gone `every_s` seconds of wall-clock
    time without a replication; restarts of the process do not reset the clock."""

    def __init__(self, svc, *, workspace: Path, sandbox: SandboxFactory, every_s: float,
                 check_every_s: float = 60.0):
        self.svc, self.workspace, self.sandbox, self.every_s = svc, workspace, sandbox, every_s
        self.check_every_s = check_every_s
        self._checked = 0.0
        self._task: asyncio.Task | None = None

    def busy(self) -> bool:
        return self._task is not None and not self._task.done()

    def step(self) -> None:
        now = time.time()
        if self.every_s <= 0 or self.busy() or now - self._checked < self.check_every_s:
            return
        self._checked = now
        claims = self.svc.accepted_claims(due_after_s=self.every_s, now=now)
        if claims:
            self._task = asyncio.get_running_loop().create_task(
                replicate(self.svc, claims, workspace=self.workspace, sandbox=self.sandbox))

    async def close(self) -> None:
        if self.busy():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)


def success_rate(claims: list, *, first_only: bool = True) -> tuple[int, int]:
    """(replayed ok, replayed) over the authors' finished replays; with first_only each
    (task, name) counts once, by its first finished replay — the registration success the P1
    gate measures. Reviewers' own reproductions are not registrations."""
    seen: set[tuple[str, str]] = set()
    ok = n = 0
    for c in sorted(claims, key=lambda c: c.seq):
        if c.status == "requested" or c.code == "infra" or getattr(c, "adhoc", None):
            continue
        k = (c.task_id, c.name)
        if first_only and k in seen:
            continue
        seen.add(k)
        n += 1
        ok += c.status == "passed"
    return ok, n
