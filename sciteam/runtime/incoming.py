"""Owner-supplied raw inputs.

A project's measurements live in ``workspace/data/incoming``. The team sandbox mounts that
directory read-only. Small text is committed with the workspace; large files and scientific
binaries stay on disk, named in the manifest with their checksum, and are copied back when a
commit is exported for replay. Before the team starts, the same files sit in ``intake/data``.
"""

from __future__ import annotations

import hashlib
import mimetypes
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import yaml

from sciteam.kernel.ids import new_id

MANIFEST = "MANIFEST.yaml"
README = "README.txt"
README_TEXT = (
    "Owner raw inputs. Read them. Do not modify, rename, or delete anything in this "
    "directory. Copy elsewhere before transforming. The bytes are the measurement.\n"
)
GIT_KEEP_MAX = 20 * 1024 * 1024
SIDECAR_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf", ".zip", ".gz", ".tgz", ".bz2",
    ".7z", ".npy", ".npz", ".h5", ".hdf5", ".parquet", ".xlsx", ".xls", ".ico",
    ".nc", ".mat", ".bam", ".fastq", ".fasta", ".fa", ".pdb",
}
BLOCKED_SUFFIXES = {".exe", ".dll", ".so", ".bat", ".cmd", ".ps1", ".scr", ".com", ".sh"}
RESERVED = {MANIFEST, README, ".gitignore"}
TEXT_SUFFIXES = {".csv", ".tsv", ".txt", ".md", ".json", ".yaml", ".yml", ".py", ".r"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
PREVIEW_TEXT = 65536
PREVIEW_IMAGE = 1_500_000

_LOCKS: dict[str, threading.Lock] = {}


def _lock(root: Path) -> threading.Lock:
    return _LOCKS.setdefault(str(Path(root).resolve()), threading.Lock())


def intake_data_root(pdir: Path) -> Path:
    return Path(pdir) / "intake" / "data"


def incoming_root(pdir: Path) -> Path:
    return Path(pdir) / "workspace" / "data" / "incoming"


def clean_name(name: str) -> str:
    raw = (name or "").replace("\\", "/").split("/")[-1].strip()
    if raw in {"", ".", ".."} or raw.startswith(".") or "/" in raw or "\\" in raw:
        raise ValueError("需要一个文件名")
    if any(ord(ch) < 32 for ch in raw):
        raise ValueError("文件名含有无法使用的字符")
    if raw in RESERVED:
        raise ValueError("这个文件名留给资料清单")
    suffix = Path(raw).suffix.lower()
    if suffix in BLOCKED_SUFFIXES:
        raise ValueError("不能上传可执行文件")
    if len(raw) > 180:
        raw = raw[:160] + suffix
    return raw


def stored_as(name: str, size: int) -> str:
    if size > GIT_KEEP_MAX or Path(name).suffix.lower() in SIDECAR_SUFFIXES:
        return "disk"
    return "git"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _columns(name: str, data: bytes) -> list[str]:
    suffix = Path(name).suffix.lower()
    if suffix not in {".csv", ".tsv"}:
        return []
    line = data.splitlines()[0].decode("utf-8-sig", errors="replace") if data else ""
    sep = "\t" if suffix == ".tsv" else ","
    out = []
    for cell in line.split(sep):
        cell = cell.strip().strip('"').strip("'")
        if cell:
            out.append(cell[:80])
        if len(out) >= 40:
            break
    return out


def _unique(root: Path, name: str, taken: set[str]) -> str:
    if name not in taken and not (root / name).exists():
        return name
    stem, suffix = Path(name).stem, Path(name).suffix
    n = 2
    while True:
        cand = f"{stem}-{n}{suffix}"
        if cand not in taken and not (root / cand).exists():
            return cand
        n += 1


def _load(root: Path) -> dict[str, Any]:
    path = root / MANIFEST
    if not path.is_file():
        return {"files": [], "removed": []}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    data.setdefault("files", [])
    data.setdefault("removed", [])
    return data


def _dump(root: Path, data: dict[str, Any]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / MANIFEST).write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    names = [r["name"] for r in data.get("files") or [] if r.get("stored") == "disk"]
    (root / ".gitignore").write_text(
        "# bytes kept beside git; the manifest records their checksums\n"
        + "".join(f"{name}\n" for name in names), encoding="utf-8")
    readme = root / README
    if not readme.exists():
        readme.write_text(README_TEXT, encoding="utf-8")


def _public(rec: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": rec["id"], "name": rec["name"], "size": rec["size"], "sha256": rec["sha256"],
        "media": rec.get("media") or "", "added_at": rec.get("added_at") or "",
        "note": rec.get("note") or "", "source": rec.get("source") or "upload",
        "columns": list(rec.get("columns") or []), "stored": rec.get("stored") or "git",
    }


def briefing_text(root: Path) -> str:
    """What an activation is told about the owner's files. Empty when there are none."""
    if not (root / MANIFEST).is_file():
        return ""
    data = _load(root)
    files = list(data.get("files") or [])
    removed = list(data.get("removed") or [])
    if not files and not removed:
        return ""
    lines = [
        "The owner supplied these raw inputs at /workspace/data/incoming.",
        "Read them. Do not modify, rename, or delete anything in that directory.",
        "Copy to another directory before any transformation. The bytes are the measurement.",
    ]
    for rec in files:
        cols = rec.get("columns") or []
        extra = f", columns: {', '.join(cols)}" if cols else ""
        note = f". Note: {rec['note']}" if rec.get("note") else ""
        lines.append(f"- {rec['name']} ({rec['size']} bytes{extra}){note}")
    for rec in removed:
        lines.append(f"- removed by the owner: {rec.get('name')}")
    return "\n".join(lines)


def commit_incoming(workspace: Path) -> None:
    """Record the manifest and the small files. Sidecars stay untracked on purpose."""
    if not (workspace / ".git").is_dir():
        return
    rel = "data/incoming"
    subprocess.run(["git", "-C", str(workspace), "add", "-A", "--", rel], check=False)
    subprocess.run(
        ["git", "-C", str(workspace), "-c", "user.email=team@sciteam",
         "-c", "user.name=sciteam", "commit", "-q", "-m", "owner data", "--", rel],
        check=False)


def restore_sidecars(workspace: Path, dest: Path) -> None:
    """Copy checksum-matched sidecar bytes into a git archive of ``workspace``."""
    manifest = dest / "data" / "incoming" / MANIFEST
    if not manifest.is_file():
        return
    data = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
    for rec in data.get("files") or []:
        if rec.get("stored") != "disk":
            continue
        src = workspace / "data" / "incoming" / rec["name"]
        if not src.is_file():
            continue
        blob = src.read_bytes()
        if _sha(blob) != rec.get("sha256"):
            continue
        target = dest / "data" / "incoming" / rec["name"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blob)


def publish_intake(pdir: Path) -> None:
    """Copy intake uploads into the workspace incoming directory. A second call leaves it."""
    src = intake_data_root(pdir)
    dest = incoming_root(pdir)
    dest.mkdir(parents=True, exist_ok=True)
    if (dest / MANIFEST).is_file() or not (src / MANIFEST).is_file():
        if not (dest / README).exists():
            (dest / README).write_text(README_TEXT, encoding="utf-8")
        return
    data = _load(src)
    for rec in data.get("files") or []:
        origin = src / rec["name"]
        if origin.is_file():
            shutil.copy2(origin, dest / rec["name"])
    _dump(dest, data)


class DataFolder:
    """One directory of owner files plus its manifest."""

    def __init__(self, root: Path, *, file_cap: int, total_cap: int, started: bool,
                 workspace: Path | None = None):
        self.root = Path(root)
        self.file_cap = file_cap
        self.total_cap = total_cap
        self.started = started
        self.workspace = workspace

    def state(self) -> dict[str, Any]:
        data = _load(self.root) if (self.root / MANIFEST).is_file() else {"files": [], "removed": []}
        return {
            "files": [_public(r) for r in data.get("files") or []],
            "removed": list(data.get("removed") or []),
            "started": self.started,
            "file_cap": self.file_cap,
            "total_cap": self.total_cap,
        }

    def add(self, name: str, blob: bytes, *, note: str = "", source: str = "upload") -> dict[str, Any]:
        name = clean_name(name)
        if not blob:
            raise ValueError("文件是空的")
        if len(blob) > self.file_cap:
            raise ValueError("超过单文件上限")
        with _lock(self.root):
            self.root.mkdir(parents=True, exist_ok=True)
            data = _load(self.root)
            used = sum(int(r.get("size") or 0) for r in data["files"])
            if used + len(blob) > self.total_cap:
                raise ValueError("超过这个课题的原始资料上限")
            name = _unique(self.root, name, {r["name"] for r in data["files"]})
            rec = {
                "id": new_id("dat"), "name": name, "size": len(blob), "sha256": _sha(blob),
                "media": mimetypes.guess_type(name)[0] or "application/octet-stream",
                "added_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "note": (note or "").strip()[:500], "source": source or "upload",
                "columns": _columns(name, blob), "stored": stored_as(name, len(blob)),
            }
            (self.root / name).write_bytes(blob)
            data["files"].append(rec)
            _dump(self.root, data)
        self._commit()
        return _public(rec)

    def set_note(self, file_id: str, note: str) -> dict[str, Any]:
        with _lock(self.root):
            data = _load(self.root)
            rec = self._find(data, file_id)
            rec["note"] = (note or "").strip()[:500]
            _dump(self.root, data)
        self._commit()
        return _public(rec)

    def remove(self, file_id: str) -> dict[str, Any]:
        with _lock(self.root):
            data = _load(self.root)
            rec = self._find(data, file_id)
            data["files"] = [r for r in data["files"] if r["id"] != file_id]
            if self.started:
                data["removed"].append({
                    "id": rec["id"], "name": rec["name"], "sha256": rec["sha256"],
                    "removed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                })
            path = self.root / rec["name"]
            if path.is_file():
                path.unlink()
            _dump(self.root, data)
        self._commit()
        return self.state()

    def preview(self, file_id: str) -> dict[str, Any]:
        data = _load(self.root)
        rec = self._find(data, file_id)
        blob = (self.root / rec["name"]).read_bytes()
        out = _public(rec)
        suffix = Path(rec["name"]).suffix.lower()
        if suffix in IMAGE_SUFFIXES and len(blob) <= PREVIEW_IMAGE:
            import base64
            out["kind"] = "image"
            out["b64"] = base64.b64encode(blob).decode("ascii")
            return out
        if suffix in TEXT_SUFFIXES or suffix in {".csv", ".tsv"}:
            out["kind"] = "text"
            out["text"] = blob[:PREVIEW_TEXT].decode("utf-8", errors="replace")
            out["truncated"] = len(blob) > PREVIEW_TEXT
            return out
        out["kind"] = "binary"
        return out

    def _find(self, data: dict[str, Any], file_id: str) -> dict[str, Any]:
        for rec in data.get("files") or []:
            if rec.get("id") == file_id:
                return rec
        raise ValueError("没有这份资料")

    def _commit(self) -> None:
        if self.workspace is not None:
            commit_incoming(self.workspace)


class DatasetLibrary:
    """Laboratory copies. Attaching one snapshots the bytes into a project."""

    def __init__(self, root: Path, *, file_cap: int, total_cap: int):
        self.root = Path(root)
        self.file_cap = file_cap
        self.total_cap = total_cap

    def list(self) -> list[dict[str, Any]]:
        out = []
        if not self.root.is_dir():
            return out
        for path in sorted(self.root.iterdir()):
            meta = path / "meta.yaml"
            if meta.is_file():
                rec = yaml.safe_load(meta.read_text(encoding="utf-8")) or {}
                if rec.get("id"):
                    out.append(_public(rec))
        return out

    def add(self, name: str, blob: bytes, *, note: str = "") -> dict[str, Any]:
        name = clean_name(name)
        if not blob:
            raise ValueError("文件是空的")
        if len(blob) > self.file_cap:
            raise ValueError("超过单文件上限")
        with _lock(self.root):
            self.root.mkdir(parents=True, exist_ok=True)
            used = sum(int(r.get("size") or 0) for r in self.list())
            if used + len(blob) > self.total_cap:
                raise ValueError("超过资料库上限")
            rec = {
                "id": new_id("set"), "name": name, "size": len(blob), "sha256": _sha(blob),
                "media": mimetypes.guess_type(name)[0] or "application/octet-stream",
                "added_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "note": (note or "").strip()[:500], "source": "library",
                "columns": _columns(name, blob), "stored": "disk",
            }
            folder = self.root / rec["id"]
            folder.mkdir(parents=True)
            (folder / "payload").write_bytes(blob)
            (folder / "meta.yaml").write_text(
                yaml.safe_dump(rec, allow_unicode=True, sort_keys=False), encoding="utf-8")
        return _public(rec)

    def set_note(self, dataset_id: str, note: str) -> dict[str, Any]:
        rec, folder = self._open(dataset_id)
        rec["note"] = (note or "").strip()[:500]
        (folder / "meta.yaml").write_text(
            yaml.safe_dump(rec, allow_unicode=True, sort_keys=False), encoding="utf-8")
        return _public(rec)

    def remove(self, dataset_id: str) -> dict[str, Any]:
        _, folder = self._open(dataset_id)
        shutil.rmtree(folder)
        return {"deleted": dataset_id, "datasets": self.list()}

    def read(self, dataset_id: str) -> tuple[dict[str, Any], bytes]:
        rec, folder = self._open(dataset_id)
        return _public(rec), (folder / "payload").read_bytes()

    def preview(self, dataset_id: str) -> dict[str, Any]:
        rec, blob = self.read(dataset_id)
        out = dict(rec)
        suffix = Path(rec["name"]).suffix.lower()
        if suffix in IMAGE_SUFFIXES and len(blob) <= PREVIEW_IMAGE:
            import base64
            out["kind"] = "image"
            out["b64"] = base64.b64encode(blob).decode("ascii")
            return out
        if suffix in TEXT_SUFFIXES or suffix in {".csv", ".tsv"}:
            out["kind"] = "text"
            out["text"] = blob[:PREVIEW_TEXT].decode("utf-8", errors="replace")
            out["truncated"] = len(blob) > PREVIEW_TEXT
            return out
        out["kind"] = "binary"
        return out

    def _open(self, dataset_id: str) -> tuple[dict[str, Any], Path]:
        folder = self.root / dataset_id
        meta = folder / "meta.yaml"
        if not meta.is_file():
            raise ValueError("资料库里没有这一条")
        return yaml.safe_load(meta.read_text(encoding="utf-8")) or {}, folder


class IncomingBriefing:
    """Activation section listing the owner's raw inputs, read fresh each time."""

    def __init__(self, workspace: Path):
        self.workspace = workspace

    def attention(self, ev, view) -> list:
        return []

    def busy(self) -> bool:
        return False

    def step(self) -> None:
        pass

    def briefing(self, member: str) -> list[tuple[str, str]]:
        text = briefing_text(self.workspace / "data" / "incoming")
        return [("Owner data", text)] if text else []

    async def close(self) -> None:
        pass
