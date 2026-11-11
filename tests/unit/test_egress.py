import asyncio
import json

from sciteam.runtime.egress import EgressProxy, allowed, proxy_env


def test_allowed_matches_domain_and_subdomain_only():
    a = ["pubmed.ncbi.nlm.nih.gov", "*.arxiv.org"]
    assert allowed("pubmed.ncbi.nlm.nih.gov", a) and allowed("export.arxiv.org", a)
    assert not allowed("evilarxiv.org", a) and not allowed("example.com", a)
    assert allowed("anything.io", ["*"]) and not allowed("x.io", [])


def test_proxy_refuses_outside_and_logs(tmp_path):
    async def go():
        p = EgressProxy(["allowed.test"], tmp_path / "egress.jsonl", bind="127.0.0.1")
        port = await p.start()
        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(b"CONNECT blocked.test:443 HTTP/1.1\r\nHost: blocked.test\r\n\r\n")
        await w.drain()
        line = (await r.readline()).decode()
        w.close()
        await p.close()
        return line
    assert "403" in asyncio.run(go())
    rec = json.loads((tmp_path / "egress.jsonl").read_text().splitlines()[0])
    assert rec["host"] == "blocked.test" and rec["allowed"] is False


def test_proxy_tunnels_allowed_host(tmp_path):
    async def go():
        async def echo(r, w):
            w.write(await r.read(5))
            await w.drain()
            w.close()
        srv = await asyncio.start_server(echo, "127.0.0.1", 0)
        up = srv.sockets[0].getsockname()[1]
        p = EgressProxy(["127.0.0.1"], tmp_path / "e.jsonl", bind="127.0.0.1")
        port = await p.start()
        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(f"CONNECT 127.0.0.1:{up} HTTP/1.1\r\n\r\n".encode())
        await w.drain()
        assert b"200" in await r.readline()
        await r.readline()
        w.write(b"hello")
        await w.drain()
        data = await r.read(5)
        w.close()
        srv.close()
        await p.close()
        return data
    assert asyncio.run(go()) == b"hello"
    assert json.loads((tmp_path / "e.jsonl").read_text().splitlines()[0])["allowed"] is True


def test_proxy_env_sets_both_cases():
    e = proxy_env("172.17.0.1", 3128)
    assert e["https_proxy"] == "http://172.17.0.1:3128" and e["HTTP_PROXY"] == e["http_proxy"]
