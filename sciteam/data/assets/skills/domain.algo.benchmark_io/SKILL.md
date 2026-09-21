# SKILL: domain.algo.benchmark_io

**Purpose**: read and write frozen-benchmark artifacts correctly — locate the
trace, respect its format, and put candidate outputs exactly where the frozen
eval expects them. Never touch the eval script or the frozen inputs.

**Inputs**: the benchmark directory for the registered `problem_id`
(e.g. `/lab/benchmarks/p2_kvcache/`); the protocol registration artifact
(paths + hashes).

**Outputs**: verified data paths cited in the deliverable; candidate files in
the exact location and format the frozen eval reads.

**Steps**
1. Resolve trace and eval entry points from the protocol registration; verify
   the recorded hashes match the files on disk before relying on them.
2. Read the data format from the benchmark's own schema/README. If the format
   is ambiguous, say so in the deliverable — do not silently infer.
3. Write candidate outputs only to the location the eval documents. The frozen
   inputs and the eval script are read-only; a change there voids the run.
4. Cite exact paths (not summaries) wherever the deliverable references data.

**Failure modes**: inventing a data format; editing frozen inputs or the eval;
quoting benchmark contents from memory instead of from files on disk.

**Stop**: paths and hashes recorded in the mission artifact; no writes outside
the documented output location.
