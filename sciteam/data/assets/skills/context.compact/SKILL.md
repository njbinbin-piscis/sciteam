---
id: context.compact
version: "1.0.0"
---

# Skill: context.compact

## Purpose
Condense several parallel specialist outputs (same wave) into one bounded,
evidence-preserving synthesis — without silently dropping a disagreement or
padding the artifact with duplicated raw text.

## Steps
1. Read every parallel-branch result in `# Team context` before writing
   anything. Do not synthesize from memory of earlier rounds alone.
2. Deduplicate: when two branches reach the same conclusion, keep one
   citation to it, not two paraphrases.
3. Never delete a disagreement. If branches conflict, keep both positions in
   the summary and let the exit artifact's decision layer resolve it — this
   skill only compacts, it does not adjudicate.
4. Prefer pointers over pasted text: reference `work_item:<id>` / `round:<n>`
   instead of quoting a branch's full output when a summary sentence
   suffices.
5. Preserve pins verbatim (mission id, exit contract, "official numbers only
   from the frozen eval") — they are never subject to compaction.

## Rules
Do not invent a synthesis that no branch actually produced. Do not compact
away the losing side of a disagreement just because it is inconvenient for a
clean narrative.

## Stop
The condensed section is shorter than the sum of its inputs, every kept claim
still traces to a `work_item`/`round` pointer, and no disagreement was
silently resolved by omission.
