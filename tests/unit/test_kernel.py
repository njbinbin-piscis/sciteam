import asyncio
import sqlite3
import threading

import pytest
from pydantic import BaseModel

from sciteam.kernel import (
    BlobStore,
    ConcurrencyConflict,
    ConfigStore,
    EventStore,
    canonical_json,
    fold,
    new_id,
)


@pytest.fixture
def store(tmp_path):
    s = EventStore(tmp_path / "events.db")
    yield s
    s.close()


def test_append_read_roundtrip(store):
    e1 = store.append("run/a", "started", {"x": 1, "nested": {"b": 2, "a": 1}}, actor="system")
    e2 = store.append("run/a", "step", {"n": 2})
    got = store.read("run/a")
    assert [e.seq for e in got] == [e1.seq, e2.seq]
    assert got[0].payload == {"x": 1, "nested": {"a": 1, "b": 2}}
    assert got[0].hash == e1.hash
    assert got[1].prev_hash == e1.hash
    assert [e.stream_seq for e in got] == [1, 2]


def test_pydantic_payload(store):
    class P(BaseModel):
        a: int
        b: list[str]

    ev = store.append("s", "t", P(a=1, b=["x"]))
    assert store.read("s")[0].payload == {"a": 1, "b": ["x"]} == ev.payload


def test_unserializable_payload_rejected(store):
    with pytest.raises(TypeError):
        store.append("s", "t", {"x": object()})
    assert store.read("s") == []


def test_verify_detects_tamper(store, tmp_path):
    for i in range(5):
        store.append("s", "t", {"i": i})
    assert store.verify("s").ok
    conn = sqlite3.connect(tmp_path / "events.db")
    conn.execute("UPDATE events SET payload='{\"i\":99}' WHERE stream_seq=3")
    conn.commit()
    conn.close()
    rep = store.verify("s")
    assert not rep.ok and rep.first_bad_stream_seq == 3


def test_concurrency_conflict(store):
    store.append("s", "t", {})
    store.append("s", "t", {}, expected_stream_seq=1)
    with pytest.raises(ConcurrencyConflict):
        store.append("s", "t", {}, expected_stream_seq=1)
    assert len(store.read("s")) == 2


def test_canonical_json_key_order():
    assert canonical_json({"b": 1, "a": [1, {"d": 1, "c": 2}]}) == canonical_json(
        {"a": [1, {"c": 2, "d": 1}], "b": 1}
    )


def test_read_global_filters(store):
    store.append("a/1", "x", {})
    store.append("b/1", "y", {})
    store.append("a/2", "y", {})
    assert [e.stream for e in store.read_global(stream_prefix="a/")] == ["a/1", "a/2"]
    assert [e.stream for e in store.read_global(types={"y"})] == ["b/1", "a/2"]
    assert store.streams("a/") == ["a/1", "a/2"]


async def test_subscribe_receives_new_events_once(store):
    store.append("s", "old", {})
    got = []

    async def consume():
        async for ev in store.subscribe(after_seq=1):
            got.append(ev.type)
            if len(got) == 3:
                return

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)
    for t in ("a", "b", "c"):
        store.append("s", t, {})
    await asyncio.wait_for(task, 2)
    assert got == ["a", "b", "c"]


def test_concurrent_threads(store):
    def worker(stream, n):
        for i in range(n):
            store.append(stream, "t", {"i": i})

    threads = [threading.Thread(target=worker, args=(s, 1000)) for s in ("x", "y", "x", "z")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    seqs = [e.seq for e in store.read_global()]
    assert seqs == sorted(seqs) and len(set(seqs)) == 4000
    for s in ("x", "y", "z"):
        assert store.verify(s).ok
    assert len(store.read("x")) == 2000


def test_reopen_persists(tmp_path):
    s = EventStore(tmp_path / "e.db")
    s.append("s", "t", {"k": "v"})
    s.close()
    s2 = EventStore(tmp_path / "e.db")
    assert s2.verify("s").ok and s2.read("s")[0].payload == {"k": "v"}
    s2.close()


def test_fold(store):
    for i in range(4):
        store.append("s", "inc", {"by": i})
    assert fold(store.read("s"), lambda acc, e: acc + e.payload["by"], 0) == 6


def test_blobstore(tmp_path):
    b = BlobStore(tmp_path / "blobs")
    r1 = b.put_text("l0\nl1\nl2\nl3\n")
    r2 = b.put_text("l0\nl1\nl2\nl3\n")
    assert r1 == r2 and b.exists(r1.sha256)
    assert b.read_slice(r1, offset=1, limit=2) == "l1\nl2\n"
    assert b.read_slice(r1, offset=3, limit=10) == "l3\n"
    assert b.read_slice(r1, offset=10, limit=10) == ""
    data = bytes(range(256))
    assert b.get_bytes(b.put_bytes(data)) == data


def test_config_store_overlay_and_snapshot(tmp_path):
    base = tmp_path / "base"
    ov = tmp_path / "ov"
    (base / "h").mkdir(parents=True)
    (ov / "h").mkdir(parents=True)
    (base / "h" / "a.yaml").write_text("x: 1\n")
    (base / "h" / "b.yaml").write_text("x: 2\n")
    (ov / "h" / "a.yaml").write_text("x: 10\n")
    cs = ConfigStore(base, [ov])
    assert cs.load_yaml("h/a.yaml") == {"x": 10}
    snap = cs.snapshot()
    assert list(snap.files) == ["h/a.yaml"]
    cs2 = ConfigStore(base, [ov])
    cs2.load_yaml("h/a.yaml")
    assert cs2.snapshot().digest == snap.digest


def test_ids_sortable():
    ids = [new_id("act") for _ in range(50)]
    assert all(i.startswith("act_") and len(i) == 30 for i in ids)
