# SKILL: impl.debug_loop

**Purpose**: iterate implementation against the frozen eval to a working
candidate.

**Inputs**: scaffold + latest eval output / stack trace.

**Outputs**: revised module; a one-line log per attempt (kept in context).

**Steps**
1. Reproduce the failure with the smallest input you can.
2. Fix the root cause, not the symptom; one behavioral change per iteration.
3. Re-run the frozen eval after every change; never batch unverified fixes.
4. Log: attempt N — what changed — what the eval said.

**Failure modes**: shotgun edits; "fixing" by weakening the design; tuning
to the eval seed (adversary will re-seed).

**Stop**: eval passes structurally (valid run) — success predicate outcome is
whatever it is; report it honestly.
