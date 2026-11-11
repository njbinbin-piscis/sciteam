"""Realworld helpers: a lockfile of what was installed, and declared secrets.

Lockfile: after members install software, `freeze_env` records it in <workspace>/env/ so that
`restore_commands` rebuilds the same environment in a fresh sandbox (the validator and replay
run offline and need it rebuilt from files, not from the network).

Secrets: only names the project declares are injected into a sandbox, and `scrub` removes their
values from anything that is logged."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping

from sciteam.providers.sandbox.base import Sandbox

LOCK_DIR = "env"
PIP_LOCK = f"{LOCK_DIR}/requirements.lock"
APT_LOCK = f"{LOCK_DIR}/apt.lock"
WHEELS = f"{LOCK_DIR}/wheels"


async def freeze_env(sb: Sandbox) -> dict[str, str]:
    """Write pip and apt lock files into the workspace; returns {file: sha256}."""
    out: dict[str, str] = {}
    await sb.run(f"mkdir -p {LOCK_DIR}", timeout_s=30)
    pip = await sb.run("pip freeze --exclude-editable 2>/dev/null || true", timeout_s=120)
    apt = await sb.run("apt-mark showmanual 2>/dev/null | sort || true", timeout_s=60)
    for path, res in ((PIP_LOCK, pip), (APT_LOCK, apt)):
        data = res.output.strip() + "\n" if res.output.strip() else ""
        await sb.write_file(path, data.encode())
        out[path] = hashlib.sha256(data.encode()).hexdigest()
    if out[PIP_LOCK] != hashlib.sha256(b"").hexdigest():
        # keep the wheels so an offline sandbox can rebuild the environment without a network
        await sb.run(f"pip download -q --no-deps -r {PIP_LOCK} -d {WHEELS} || true", timeout_s=900)
    return out


def restore_commands(pip_lock: str, apt_lock: str = "") -> list[str]:  # noqa: ARG001
    """Commands that rebuild the recorded environment; empty locks give no commands."""
    # apt packages need a network or a mirror; the offline rebuild restores python packages only
    if any(line.strip() for line in pip_lock.splitlines()):
        return [f"pip install -q --no-index --no-deps --find-links /workspace/{WHEELS} "
                f"-r /workspace/{PIP_LOCK}"]
    return []


def declared_env(declared: list[str], environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The declared secrets that exist on the host; nothing else is ever passed in."""
    environ = os.environ if environ is None else environ
    return {k: environ[k] for k in declared if k in environ}


def scrub(text: str, secrets: Mapping[str, str]) -> str:
    for name, value in secrets.items():
        if value and len(value) >= 4:
            text = text.replace(value, f"[secret:{name}]")
    return text
