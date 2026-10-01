import hashlib
import os
import tempfile
from pathlib import Path

from pydantic import BaseModel


class BlobRef(BaseModel):
    sha256: str
    size: int
    media_type: str = "text/plain"


class BlobStore:
    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, sha: str) -> Path:
        return self.root / sha[:2] / sha

    def put_bytes(self, data: bytes, media_type: str = "application/octet-stream") -> BlobRef:
        sha = hashlib.sha256(data).hexdigest()
        path = self._path(sha)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                os.replace(tmp, path)
            except BaseException:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise
        return BlobRef(sha256=sha, size=len(data), media_type=media_type)

    def put_text(self, text: str) -> BlobRef:
        return self.put_bytes(text.encode("utf-8"), media_type="text/plain")

    @staticmethod
    def _sha(ref: BlobRef | str) -> str:
        return ref.sha256 if isinstance(ref, BlobRef) else ref

    def get_bytes(self, ref: BlobRef | str) -> bytes:
        return self._path(self._sha(ref)).read_bytes()

    def get_text(self, ref: BlobRef | str) -> str:
        return self.get_bytes(ref).decode("utf-8")

    def read_slice(self, ref: BlobRef | str, *, offset: int, limit: int) -> str:
        """Lines [offset, offset+limit), 0-based."""
        lines = self.get_text(ref).splitlines(keepends=True)
        return "".join(lines[max(offset, 0) : max(offset, 0) + max(limit, 0)])

    def line_count(self, ref: BlobRef | str) -> int:
        return len(self.get_text(ref).splitlines())

    def exists(self, sha256: str) -> bool:
        return self._path(sha256).exists()
