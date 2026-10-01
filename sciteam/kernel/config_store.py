import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import TypeVar

import yaml
from pydantic import BaseModel

import sciteam
from sciteam.kernel.canonical import canonical_json, sha256_hex

M = TypeVar("M", bound=BaseModel)


class ConfigSnapshot(BaseModel):
    files: dict[str, str]
    overlays: list[str]
    interpreter_version: str
    interpreter_digest: str = ""          # sha256 of the sciteam/ sources (docs/60 §2)
    digest: str


def interpreter_digest(root: Path | None = None) -> str:
    """Content hash of every .py file of the interpreter, so uncommitted edits show up."""
    root = root or Path(sciteam.__file__).parent
    return sha256_hex(canonical_json({str(p.relative_to(root)): sha256_hex(p.read_bytes())
                                      for p in sorted(root.rglob("*.py"))}))


def _git_commit(path: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode != 0:
            return None
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def split_front_matter(text: str) -> tuple[dict, str]:
    """A text config (profile, README) may start with a YAML header between '---' lines."""
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            head = yaml.safe_load(text[4:end]) or {}
            return (head if isinstance(head, dict) else {}), text[end + 4:].lstrip("\n")
    return {}, text


class ConfigStore:
    def __init__(self, base: Path | str, overlays: Sequence[Path | str] = ()):
        self.base = Path(base)
        self.overlays = [Path(o) for o in overlays]
        self._read: dict[str, str] = {}

    def _resolve(self, rel_path: str) -> Path:
        for layer in reversed(self.overlays):
            p = layer / rel_path
            if p.exists():
                return p
        p = self.base / rel_path
        if not p.exists():
            raise FileNotFoundError(rel_path)
        return p

    def load_text(self, rel_path: str) -> str:
        text = self._resolve(rel_path).read_text(encoding="utf-8")
        self._read[rel_path] = sha256_hex(text)
        return text

    def load(self, rel_path: str, schema: type[M]) -> M:
        data = yaml.safe_load(self.load_text(rel_path)) or {}
        return schema.model_validate(data)

    def list_dir(self, rel_dir: str) -> list[str]:
        """Entry names under a config directory, overlays included."""
        names: set[str] = set()
        for layer in (self.base, *self.overlays):
            d = layer / rel_dir
            if d.is_dir():
                names |= {p.name for p in d.iterdir() if not p.name.startswith(".")}
        return sorted(names)

    def load_yaml(self, rel_path: str) -> dict:
        return yaml.safe_load(self.load_text(rel_path)) or {}

    def snapshot(self) -> ConfigSnapshot:
        commit = _git_commit(self.base)
        version = sciteam.__version__ + (f"+{commit}" if commit else "")
        return ConfigSnapshot(
            files=dict(sorted(self._read.items())),
            overlays=[str(o) for o in self.overlays],
            interpreter_version=version,
            interpreter_digest=interpreter_digest(),
            digest=sha256_hex(canonical_json(dict(sorted(self._read.items())))),
        )
