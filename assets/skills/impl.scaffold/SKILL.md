# SKILL: impl.scaffold

**Purpose**: create the candidate module skeleton that the frozen eval loads.

**Inputs**: design artifact; eval interface docs.

**Outputs**: a runnable candidate module implementing the exact interface,
with components stubbed but the module already loadable by the eval.

**Steps**
1. Mirror the interface names/signatures verbatim (NAME, class/function set).
2. Stub each design component behind its parameter defaults.
3. Verify the degenerate configuration behaves like the baseline.
4. Run the frozen eval once on the scaffold to prove loadability.

**Failure modes**: renamed entry points; importing baseline internals;
simulator monkey-patching (instant disqualification).

**Stop**: eval loads the module and produces a schema-valid results.json.
