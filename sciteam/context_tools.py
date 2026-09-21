"""``read_archived_result`` — the capability-gated recovery tool from
``docs/SESSION-KERNEL.md`` §5.1.

Compaction (mechanical prune today, summarize in a future stage) replaces a
large tool result with a short pointer so the live context stays bounded.
This tool is how a seat re-opens the full text behind that pointer *within
the same session*, when it turns out to matter later — content-addressed
(sha256 of the original payload), never a bare filesystem path, so the
pointer survives a run-directory move or a replay on another machine.

Deliberately readonly and deterministic: it never mutates state, and asking
for the same ``result_ref`` twice returns byte-identical content — so it is
safe for the P1-7 same-work-item result cache and safe to run concurrently
with other readonly tools.
"""

from __future__ import annotations

from typing import Any

from sciteam.session import FileSessionStore, SessionError

READ_ARCHIVED_RESULT_TOOL_DEF: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "read_archived_result",
        "description": (
            "Retrieve the full text of a tool result that was folded into a "
            "short pointer by context compaction. Use the result_ref shown in "
            "a '[compacted]'/'archived' note when the details actually matter "
            "again — do not re-run the original tool speculatively."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "result_ref": {
                    "type": "string",
                    "description": "Content-addressed pointer from a prior tool_result event.",
                },
            },
            "required": ["result_ref"],
        },
    },
}


def read_archived_result(args: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """``(args, context) -> dict`` handler, same shape as ``fs_tools.py``'s.

    ``context["session_store"]`` is the composition root's single
    ``FileSessionStore`` for this run (see ``llm_worker.py``'s
    ``tool_context`` construction) — *not* reconstructed from
    ``mission_workspace``, because the session store's archive root is a
    run-wide object, not a per-seat one.
    """
    store = context.get("session_store")
    result_ref = str(args.get("result_ref") or "")
    if not result_ref:
        return {"ok": False, "error": "result_ref is required"}
    if store is None:
        return {"ok": False, "error": "no session store is attached to this run"}
    if not isinstance(store, FileSessionStore):
        return {"ok": False, "error": "attached session store does not support archival reads"}
    try:
        content = store.read_archived(result_ref)
    except SessionError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "result_ref": result_ref, "content": content, "chars": len(content)}
