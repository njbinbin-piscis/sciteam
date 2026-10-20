You are implementer 1 of two. The other implementer is solving the same problem independently in double-blind isolation: you cannot see their files or message them, and they cannot see yours.

1. Read `/workspace/PROBLEM.md` carefully. Start your assigned goal task (`task_transition(task="T1", transition="start")`), then implement all required functions in `/private/impl1/solution.py` (`/workspace` is read-only).
2. Thoroughly self-test `/private/impl1/solution.py` on every step, physical convention, unit, boundary condition, and edge case before submitting.
3. Submit your goal task with `task_transition(task="T1", transition="submit", commit="HEAD")` (always pass `commit="HEAD"` referencing the read-only `/workspace` seed commit; never create a git repo in `/private`).
4. Once `T1` reaches `in_review`, call `finish(outcome="completed", summary=...)` immediately. The auditor will run the cross-comparison validator once both implementers have submitted.
5. If the auditor returns (`reject`s) `T1` naming specific steps that disagreed, independently re-derive and audit the physics, signs, units, and boundary conditions of those steps in `/private/impl1/solution.py`, then resubmit `T1` with `commit="HEAD"` and call `finish(outcome="completed", summary=...)`.
