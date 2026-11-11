"""Domain allow-list egress for realworld sandboxes.

A small HTTP/HTTPS (CONNECT) forward proxy on the host. Sandboxes on the bridge network get
HTTP(S)_PROXY pointing at it; a request to a host outside the allow-list is refused with 403.
Every decision is appended to a JSONL log. A process that ignores the proxy variables is not
stopped by this layer alone; host firewall rules close that gap in deployment."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path


def allowed(host: str, allow: list[str]) -> bool:
    """Exact host or any subdomain of an allowed domain; '*' allows everything."""
    host = host.lower().rstrip(".")
    for a in allow:
        a = a.lower().lstrip("*.")
        if a == "" or a == "*" or host == a or host.endswith("." + a):
            return True
    return False


class EgressProxy:
    def __init__(self, allow: list[str], log: Path, bind: str = "0.0.0.0"):
        self.allow, self.log, self.bind = list(allow), Path(log), bind
        self.port = 0
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> int:
        self.log.parent.mkdir(parents=True, exist_ok=True)
        self._server = await asyncio.start_server(self._handle, self.bind, 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def close(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    def _record(self, host: str, ok: bool, method: str) -> None:
        with self.log.open("a") as f:
            f.write(json.dumps({"t": round(time.time(), 3), "host": host, "method": method,
                                "allowed": ok}) + "\n")

    async def _handle(self, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        try:
            head = await r.readuntil(b"\r\n\r\n")
            first = head.split(b"\r\n", 1)[0].decode("latin-1").split()
            method, target = first[0], first[1]
            if method == "CONNECT":
                host, _, port = target.partition(":")
                port = port or "443"
            else:
                from urllib.parse import urlsplit
                u = urlsplit(target)
                host, port = u.hostname or "", str(u.port or 80)
            ok = bool(host) and allowed(host, self.allow)
            self._record(host, ok, method)
            if not ok:
                w.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                await w.drain()
                return
            ur, uw = await asyncio.open_connection(host, int(port))
            if method == "CONNECT":
                w.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                await w.drain()
            else:
                uw.write(head)
            await asyncio.gather(_pipe(r, uw), _pipe(ur, w))
        except (asyncio.IncompleteReadError, ConnectionError, OSError, IndexError):
            pass
        finally:
            w.close()


async def _pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
    try:
        while data := await src.read(65536):
            dst.write(data)
            await dst.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        try:
            dst.close()
        except OSError:
            pass


def proxy_env(host: str, port: int, no_proxy: str = "localhost,127.0.0.1") -> dict[str, str]:
    url = f"http://{host}:{port}"
    return {"HTTP_PROXY": url, "HTTPS_PROXY": url, "http_proxy": url, "https_proxy": url,
            "NO_PROXY": no_proxy, "no_proxy": no_proxy}
