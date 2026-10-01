"""`sciteam trace`: read back an activation from its event stream (docs/20-harness.md §12)."""

import json
from pathlib import Path

from sciteam.kernel import BlobStore, EventStore
from sciteam.providers.llm.types import AssistantTurn


def _short(s: str, n: int) -> str:
    s = s.replace("\n", "⏎ ")
    return s if len(s) <= n else s[: n - 1] + "…"


def trace(run_dir: Path, stream: str | None, *, full: bool, verify: bool) -> None:
    store = EventStore(run_dir / "events.db")
    blobs = BlobStore(run_dir / "blobs")
    if stream is None:
        for s in store.streams():
            evs = store.read(s)
            fin = next((e for e in reversed(evs) if e.type == "activation_finished"), None)
            status = fin.payload["outcome"] if fin else "unfinished"
            print(f"{s}\t{len(evs)} events\t{status}")
        return
    matches = [s for s in store.streams() if s == stream or s.startswith(stream)]
    if not matches:
        raise SystemExit(f"no stream matching {stream}")
    for s in matches:
        if verify:
            rep = store.verify(s)
            print(f"verify {s}: {'ok' if rep.ok else 'FAILED ' + str(rep.reason)} "
                  f"({rep.count} events)")
            continue
        n = 400 if not full else 100_000
        for e in store.read(s):
            p = e.payload
            t = e.ts.strftime("%H:%M:%S")
            if e.type == "model_returned":
                turn = AssistantTurn.model_validate_json(blobs.get_text(p["turn_ref"]))
                u = p["usage"]
                print(f"{t} #{e.stream_seq} MODEL {p['finish_reason']} prompt={u['prompt_tokens']} "
                      f"hit={u['cache_hit_tokens']} out={u['completion_tokens']} "
                      f"${p['cost_usd']:.4f} {p['duration_s']:.0f}s")
                if turn.reasoning and full:
                    print(f"    thinking: {_short(turn.reasoning, n)}")
                if turn.content:
                    print(f"    text: {_short(turn.content, n)}")
                for c in turn.tool_calls:
                    print(f"    call {c.name}: {_short(c.arguments_raw, n)}")
            elif e.type == "tool_finished":
                body = blobs.get_text(p["content_ref"])
                flag = " ERROR" if p["is_error"] else ""
                print(f"{t} #{e.stream_seq} TOOL {p['name']}{flag} {p['duration_s']:.1f}s "
                      f"{p['chars']} chars: {_short(body, n)}")
            elif e.type in ("compaction_finished",):
                r = p["record"]
                print(f"{t} #{e.stream_seq} COMPACTION n={r['n']} {r['method']} "
                      f"{r['tokens_before']}→{r['tokens_after']} tokens, folded "
                      f"{r['folded_from']}..{r['folded_to'] - 1}")
                if full:
                    print(r["summary"])
            elif e.type == "activation_started":
                print(f"{t} #{e.stream_seq} START model={p['model']} meta={json.dumps(p['meta'])}")
                if full:
                    print(blobs.get_text(p["task_ref"]))
            elif e.type in ("tool_started", "model_called"):
                continue
            else:
                print(f"{t} #{e.stream_seq} {e.type.upper()} {_short(json.dumps(p), n)}")
    store.close()
