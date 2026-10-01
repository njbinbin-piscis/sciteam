"""Chronological team log: messages, task transitions, denials, activations."""

from pathlib import Path

from sciteam.kernel.events import EventStore
from sciteam.runtime.attention import agent_of_activation_stream

SHOWN = {"message_posted", "task_created", "task_status_changed", "act_decided", "act_resolved",
         "activation_started", "activation_finished", "incoming_added", "timer_fired",
         "breaker_tripped", "run_quiescent", "run_stopped", "handoff_limit"}


def team_log(run_dir: Path, *, width: int = 240, denials_only: bool = False) -> str:
    store = EventStore(run_dir / "events.db")
    try:
        lines = []
        for ev in store.read_global():
            if ev.type not in SHOWN:
                continue
            p = ev.payload
            ts = ev.ts.strftime("%H:%M:%S")
            agent = agent_of_activation_stream(ev.stream)
            if ev.type in ("act_decided", "act_resolved"):
                if p["decision"] == "allow" and ev.type == "act_decided":
                    continue
                tag = {"deny": "DENY", "pending": "PEND", "allow": "ALLOW"}[p["decision"]]
                actor = p.get("actor", ev.actor)
                rule = f" [{p['rule']}]" if p.get("rule") else ""
                line = f"{tag} {actor} {p['kind']}{rule}: {p['reason']}"
                if ev.type == "act_resolved":
                    line = f"RESOLVED {p['act_id']} {line}"
            elif denials_only:
                continue
            elif ev.type == "message_posted":
                m = p["message"]
                reply = f" re {m['reply_to']}" if m.get("reply_to") else ""
                line = f"{m['msg_id']} {m['conv']} @{m['author']}{reply}: {m['text']}"
            elif ev.type == "task_created":
                t = p["task"]
                line = f"TASK+ {t['id']} by @{t['created_by']}: {t['title']}"
            elif ev.type == "task_status_changed":
                line = (f"TASK {p['task_id']} {p['transition']} {p['frm']}->{p['to']} by "
                        f"@{ev.actor}" + (f" ({p['note']})" if p.get("note") else ""))
            elif ev.type == "activation_started":
                n = len((p.get("meta") or {}).get("attention", []))
                line = f"ACT> @{agent} {p['activation_id']} ({n} items)"
            elif ev.type == "activation_finished":
                cost = (p.get("totals") or {}).get("cost_usd", 0)
                line = (f"ACT< @{agent} {p['outcome']}"
                        + (f"/{p['reason']}" if p.get("reason") else "")
                        + f" steps={p.get('steps')} ${cost:.3f}: {p.get('summary', '')}")
            elif ev.type == "incoming_added":
                line = f"INCOMING @{agent} ({len((p.get('meta') or {}).get('attention', []))})"
            else:
                line = f"{ev.type.upper()} {p}"
            line = line.replace("\n", " ")
            lines.append(f"{ts} {line[:width]}")
        return "\n".join(lines)
    finally:
        store.close()
