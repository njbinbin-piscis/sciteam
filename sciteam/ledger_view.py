"""Zero-dependency HTML viewer for institutional ledgers.

Produces a standalone, self-contained HTML file for inspecting any SciTeam
hash-chained ledger. No external CDNs, scripts, or fonts are loaded, ensuring
offline operation and compliance with zero-trust network policies.
"""

from __future__ import annotations

import html
import json
from pathlib import Path

from sciteam.event_log import (
    EventLog,
    InstitutionEvent,
    cost_report,
    read_events,
    replay,
    verify_chain,
)


def render_html(ledger_or_events: list[InstitutionEvent] | Path | str) -> str:
    """Render an interactive HTML visualization of the institutional ledger."""
    if isinstance(ledger_or_events, (str, Path)):
        p = Path(ledger_or_events)
        if p.is_dir():
            p = p / "ledger.jsonl"
        events = read_events(p)
        verification = verify_chain(p)
        spend_lines = cost_report(p)
        derived_state = replay(events)
        source_name = p.name
    else:
        events = list(ledger_or_events)
        if events:
            # Synthetic verification from in-memory event stream
            log = EventLog(Path("/tmp/dummy_ledger.jsonl"), run_id=events[0].run_id)
            for e in events:
                log.append(
                    kind=e.kind,
                    seat=e.seat,
                    authority=e.authority,
                    effect=e.effect,
                    payload=e.payload,
                    rule_version=e.rule_version,
                )
            verification = verify_chain(log.path)
            spend_lines = cost_report(log.path)
            derived_state = replay(events)
        else:
            verification = None
            spend_lines = ()
            derived_state = None
        source_name = "in-memory stream"

    run_id = events[0].run_id if events else "empty"
    rule_version = events[-1].rule_version if events else "unknown"
    is_valid = verification.ok if verification else True
    chain_length = len(events)
    total_cost = sum(line.usd for line in spend_lines)
    total_calls = sum(line.calls for line in spend_lines)

    vetoes_count = len(derived_state.vetoes) if derived_state else 0
    reopen_count = len(derived_state.reopenings) if derived_state else 0
    refused_count = len(derived_state.refused_acts) if derived_state else 0
    effective_count = len(derived_state.effective_artifacts) if derived_state else 0

    seats = sorted({e.seat for e in events if e.seat})
    kinds = sorted({e.kind for e in events})

    # Rows rendering
    rows_html: list[str] = []
    for e in events:
        is_veto = e.kind in {"veto_issued", "artifact_rejected"}
        is_reopen = e.kind == "reopened"
        row_class = "event-row"
        if is_veto:
            row_class += " row-veto"
        elif is_reopen:
            row_class += " row-reopen"

        effect_class = f"badge-effect-{e.effect}"
        seat_name = html.escape(e.seat or "—")
        auth_name = html.escape(e.authority or "—")
        payload_json = html.escape(json.dumps(e.payload, indent=2, ensure_ascii=False))

        # Kind icon
        kind_icons = {
            "run_started": "🚀",
            "run_finished": "🏁",
            "rule_version_set": "📜",
            "authority_checked": "🛡️",
            "artifact_submitted": "📥",
            "artifact_accepted": "✅",
            "artifact_rejected": "❌",
            "audit_reported": "🔍",
            "veto_issued": "⛔",
            "reopened": "🔄",
            "closed": "🔒",
            "llm_call": "🤖",
            "tool_call": "🛠️",
        }
        icon = kind_icons.get(e.kind, "📋")

        escaped_seat = html.escape(e.seat or "")
        escaped_kind = html.escape(e.kind)
        escaped_effect = html.escape(e.effect)
        rows_html.append(f"""
        <tr class="{row_class}" data-seat="{escaped_seat}"
            data-kind="{escaped_kind}" data-effect="{escaped_effect}">
            <td class="col-seq">{e.seq}</td>
            <td class="col-ts">{html.escape(e.ts.split("T")[-1][:12] if "T" in e.ts else e.ts)}</td>
            <td class="col-seat"><span class="badge badge-seat">{seat_name}</span></td>
            <td class="col-kind">{icon} <code>{escaped_kind}</code></td>
            <td class="col-effect">
                <span class="badge {effect_class}">{escaped_effect}</span>
            </td>
            <td class="col-auth"><code>{auth_name}</code></td>
            <td class="col-payload">
                <details>
                    <summary>View JSON ({len(e.payload)} keys)</summary>
                    <pre>{payload_json}</pre>
                </details>
            </td>
        </tr>
        """)

    cost_rows: list[str] = []
    for line in spend_lines:
        cost_rows.append(f"""
        <tr>
            <td><strong>{html.escape(line.seat)}</strong></td>
            <td><code>{html.escape(line.model)}</code></td>
            <td style="text-align: right;">{line.calls}</td>
            <td style="text-align: right;">{line.prompt_tokens:,}</td>
            <td style="text-align: right;">{line.completion_tokens:,}</td>
            <td style="text-align: right;">${line.usd:.4f}</td>
        </tr>
        """)

    cost_table_html = ""
    if cost_rows:
        cost_table_html = f"""
        <section class="card" style="margin-top: 1.5rem;">
            <h2>💰 Spend Attribution (G2.5)</h2>
            <table class="data-table">
                <thead>
                    <tr>
                        <th>Seat</th>
                        <th>Model</th>
                        <th style="text-align: right;">Calls</th>
                        <th style="text-align: right;">Prompt Tokens</th>
                        <th style="text-align: right;">Completion Tokens</th>
                        <th style="text-align: right;">Cost (USD)</th>
                    </tr>
                </thead>
                <tbody>
                    {"".join(cost_rows)}
                    <tr class="total-row">
                        <td colspan="2"><strong>Total</strong></td>
                        <td style="text-align: right;"><strong>{total_calls}</strong></td>
                        <td colspan="2"></td>
                        <td style="text-align: right;"><strong>${total_cost:.4f}</strong></td>
                    </tr>
                </tbody>
            </table>
        </section>
        """

    seat_options = "".join(
        f'<option value="{html.escape(s)}">{html.escape(s)}</option>' for s in seats
    )
    kind_options = "".join(
        f'<option value="{html.escape(k)}">{html.escape(k)}</option>' for k in kinds
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>SciTeam Ledger — {html.escape(run_id)}</title>
    <style>
        :root {{
            --bg: #0f172a;
            --surface: #1e293b;
            --surface-border: #334155;
            --text: #f8fafc;
            --text-muted: #94a3b8;
            --primary: #38bdf8;
            --success: #4ade80;
            --danger: #f87171;
            --warning: #fbbf24;
            --font-mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
        }}
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            background: var(--bg);
            color: var(--text);
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            line-height: 1.5;
            padding: 2rem;
        }}
        header {{
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            margin-bottom: 2rem;
            padding-bottom: 1rem;
            border-bottom: 1px solid var(--surface-border);
        }}
        h1 {{ font-size: 1.8rem; font-weight: 700; color: #fff; }}
        .meta-line {{ color: var(--text-muted); font-size: 0.95rem; margin-top: 0.25rem; }}
        .badge {{
            display: inline-block;
            padding: 0.2rem 0.6rem;
            border-radius: 9999px;
            font-size: 0.8rem;
            font-weight: 600;
            text-transform: uppercase;
        }}
        .badge-verified {{
            background: rgba(74, 222, 128, 0.2);
            color: var(--success);
            border: 1px solid var(--success);
        }}
        .badge-tampered {{
            background: rgba(248, 113, 113, 0.2);
            color: var(--danger);
            border: 1px solid var(--danger);
        }}
        .badge-seat {{ background: #334155; color: #e2e8f0; font-family: var(--font-mono); }}
        .badge-effect-accepted {{ background: rgba(74, 222, 128, 0.15); color: var(--success); }}
        .badge-effect-refused {{ background: rgba(248, 113, 113, 0.15); color: var(--danger); }}
        .badge-effect-observed {{
            background: rgba(148, 163, 184, 0.15);
            color: var(--text-muted);
        }}

        .stats-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
            gap: 1rem;
            margin-bottom: 2rem;
        }}
        .stat-card {{
            background: var(--surface);
            border: 1px solid var(--surface-border);
            border-radius: 8px;
            padding: 1rem;
        }}
        .stat-val {{ font-size: 1.6rem; font-weight: 700; color: #fff; }}
        .stat-label {{ font-size: 0.85rem; color: var(--text-muted); text-transform: uppercase; }}

        .card {{
            background: var(--surface);
            border: 1px solid var(--surface-border);
            border-radius: 8px;
            padding: 1.25rem;
            margin-bottom: 1.5rem;
        }}
        .controls {{
            display: flex;
            flex-wrap: wrap;
            gap: 1rem;
            align-items: center;
        }}
        select, input {{
            background: #0f172a;
            border: 1px solid var(--surface-border);
            color: #fff;
            padding: 0.4rem 0.8rem;
            border-radius: 6px;
            font-size: 0.9rem;
        }}
        button {{
            background: #2563eb;
            color: #fff;
            border: none;
            padding: 0.4rem 0.9rem;
            border-radius: 6px;
            cursor: pointer;
            font-weight: 500;
        }}
        button:hover {{ background: #1d4ed8; }}

        .data-table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 0.9rem;
        }}
        .data-table th, .data-table td {{
            padding: 0.75rem 1rem;
            border-bottom: 1px solid var(--surface-border);
            text-align: left;
        }}
        .data-table th {{
            background: rgba(15, 23, 42, 0.5);
            color: var(--text-muted);
            font-weight: 600;
        }}
        .event-row:hover {{ background: rgba(51, 65, 85, 0.4); }}
        .row-veto {{ border-left: 4px solid var(--danger); }}
        .row-reopen {{ border-left: 4px solid var(--warning); }}
        .highlight-veto {{ outline: 2px solid var(--warning); }}

        code, pre {{ font-family: var(--font-mono); font-size: 0.85rem; }}
        details summary {{ cursor: pointer; color: var(--primary); }}
        pre {{
            margin-top: 0.5rem;
            padding: 0.75rem;
            background: #0f172a;
            border-radius: 6px;
            overflow-x: auto;
            max-height: 300px;
        }}
        .total-row td {{ font-weight: bold; border-top: 2px solid var(--surface-border); }}
    </style>
</head>
<body>
    <header>
        <div>
            <h1>Institutional Ledger Trajectory</h1>
            <div class="meta-line">
                Run ID: <code>{html.escape(run_id)}</code> ·
                Rule Version: <code>{html.escape(rule_version[:16])}...</code>
            </div>
            <div class="meta-line">Source: {html.escape(source_name)}</div>
        </div>
        <div>
            <span class="badge {"badge-verified" if is_valid else "badge-tampered"}">
                {"✓ CHAIN VERIFIED" if is_valid else "⚠ TAMPER DETECTED"}
            </span>
        </div>
    </header>

    <section class="stats-grid">
        <div class="stat-card">
            <div class="stat-val">{chain_length}</div>
            <div class="stat-label">Total Events</div>
        </div>
        <div class="stat-card">
            <div class="stat-val" style="color: var(--danger);">{vetoes_count}</div>
            <div class="stat-label">Vetoes Issued</div>
        </div>
        <div class="stat-card">
            <div class="stat-val" style="color: var(--warning);">{reopen_count}</div>
            <div class="stat-label">Reopenings</div>
        </div>
        <div class="stat-card">
            <div class="stat-val" style="color: var(--success);">{effective_count}</div>
            <div class="stat-label">Effective Artifacts</div>
        </div>
        <div class="stat-card">
            <div class="stat-val">{refused_count}</div>
            <div class="stat-label">Refused Acts</div>
        </div>
        <div class="stat-card">
            <div class="stat-val" style="color: var(--primary);">${total_cost:.4f}</div>
            <div class="stat-label">Total Spend (USD)</div>
        </div>
    </section>

    <div class="card controls">
        <label>Filter Seat:
            <select id="seatFilter">
                <option value="">All Seats</option>
                {seat_options}
            </select>
        </label>
        <label>Filter Kind:
            <select id="kindFilter">
                <option value="">All Kinds</option>
                {kind_options}
            </select>
        </label>
        <label>Filter Effect:
            <select id="effectFilter">
                <option value="">All Effects</option>
                <option value="accepted">Accepted</option>
                <option value="refused">Refused</option>
                <option value="observed">Observed</option>
            </select>
        </label>
        <button id="toggleHighlight" type="button">⚡ Highlight Veto & Recovery Chain</button>
    </div>

    <section class="card" style="padding: 0; overflow-x: auto;">
        <table class="data-table" id="eventTable">
            <thead>
                <tr>
                    <th style="width: 50px;">Seq</th>
                    <th style="width: 100px;">Time</th>
                    <th style="width: 140px;">Seat</th>
                    <th style="width: 180px;">Event Kind</th>
                    <th style="width: 100px;">Effect</th>
                    <th style="width: 160px;">Authority</th>
                    <th>Payload (Artifacts, Verifications, Verdicts)</th>
                </tr>
            </thead>
            <tbody>
                {"".join(rows_html)}
            </tbody>
        </table>
    </section>

    {cost_table_html}

    <script>
        const seatFilter = document.getElementById('seatFilter');
        const kindFilter = document.getElementById('kindFilter');
        const effectFilter = document.getElementById('effectFilter');
        const toggleBtn = document.getElementById('toggleHighlight');
        const rows = document.querySelectorAll('.event-row');
        let highlightActive = false;

        function applyFilters() {{
            const sVal = seatFilter.value;
            const kVal = kindFilter.value;
            const eVal = effectFilter.value;

            rows.forEach(r => {{
                const seatMatch = !sVal || r.getAttribute('data-seat') === sVal;
                const kindMatch = !kVal || r.getAttribute('data-kind') === kVal;
                const effectMatch = !eVal || r.getAttribute('data-effect') === eVal;
                r.style.display = (seatMatch && kindMatch && effectMatch) ? '' : 'none';
            }});
        }}

        seatFilter.addEventListener('change', applyFilters);
        kindFilter.addEventListener('change', applyFilters);
        effectFilter.addEventListener('change', applyFilters);

        toggleBtn.addEventListener('click', () => {{
            highlightActive = !highlightActive;
            rows.forEach(r => {{
                if (r.classList.contains('row-veto') || r.classList.contains('row-reopen')) {{
                    r.classList.toggle('highlight-veto', highlightActive);
                }}
            }});
            toggleBtn.style.background = highlightActive ? '#d97706' : '#2563eb';
        }});
    </script>
</body>
</html>
"""


def export_html_view(ledger_path: Path | str, out_path: Path | str | None = None) -> Path:
    """Export ledger trajectory as an HTML view."""
    src = Path(ledger_path)
    if out_path is None:
        target = src if src.is_dir() else src.parent
        dest = target / "ledger_view.html"
    else:
        dest = Path(out_path)
    html_content = render_html(src)
    dest.write_text(html_content, encoding="utf-8")
    return dest
