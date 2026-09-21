---
id: memory.commit
version: "1.0.0"
---

# Skill: memory.commit

## Purpose
Preserve reusable failure and repair evidence without copying a full transcript.

## Commit only
- Counterexample or contract failure
- Method attempted
- Effective repair, when demonstrated
- Stable evidence pointers (work item, round, log event, artifact hash)

## Rules
Do not save unsupported conclusions, private chain-of-thought, raw long logs, or
claims that were not tested. A success without a preceding failure is ordinary
history, not a repair memory.

## Stop
The bounded seat-memory record can be independently traced to persisted evidence.
