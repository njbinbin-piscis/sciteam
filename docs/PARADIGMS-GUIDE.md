# SciTeam Paradigm Gallery & Decision Matrix

SciTeam ships 21 built-in coordination paradigms. In Institution Engineering,
a paradigm is not hardcoded Python orchestrator logic; it is a declarative,
machine-validatable institutional manifest specifying **seats**, **authorities**,
**separation of powers**, and **stopping conditions**.

Use `sciteam init <paradigm>` to scaffold any paradigm into a fully runnable,
testable project with a single command.

---

## 1. Paradigm Archetypes & Decision Matrix

| Task Archetype | Recommended Paradigm | Authority Topology | When to USE | When NOT to USE |
|---|---|---|---|---|
| **Code Review & Release Gates** | `approval_gate` | Route: Deny > Allow > Human | Production deployments requiring hard deny-rules and separation of powers between author and approver. | Rapid brainstorming with no risk of destructive side-effects. |
| **Bug Fixing & Code Repair** | `build_fix_loop` | Loop: Implementer ↔ Evaluator | Tasks with a frozen, machine-checkable test suite. Tight falsification loop. | Ambiguous tasks where success cannot be mechanically evaluated by test runners. |
| **Adversarial Peer Review** | `adversarial_pair` | Pair: Proponent vs Adversary | Challenging complex proposals, finding hidden security assumptions or logical flaws. | Simple data processing or tasks requiring collaborative synthesis. |
| **Scientific Hypothesis Generation** | `co_scientist_tournament` | Loop: Gen → Critique → Rank → Meta-review | Complex scientific discovery, literature synthesis, and multi-candidate blind tournaments. | Single-turn Q&A or low-complexity procedural tasks. |
| **Software Engineering SOP** | `software_sop_chain` | Chain: Analyst → Designer → Engineer → QA | Multi-stage engineering pipelines requiring strict documentation handoffs. | Exploratory research where requirements fluctuate rapidly. |
| **Strategic Roadmapping** | `planning_council` | Council: Multi-stakeholder consensus | High-stakes architectural choices requiring buy-in from multiple expert perspectives. | Routine operational fixes with obvious, deterministic solutions. |
| **Broad Literature Search** | `survey_gather` | Fan-Out: Parallel Gatherers → Curator | High-throughput evidence gathering across large search spaces. | Tasks requiring deep iterative debate among a small number of agents. |
| **Institutional Rule Amendment** | `amendment_review` | Council: 2/3 Committee + Quorum | Regulating and evolving organizational policies and prompt permissions. | Normal everyday task execution (amendments are constitutional operations). |
| **Red-Teaming & Blast Radius** | `blast_radius` | Adversarial: Attacker vs Safety Guard | Safety auditing, evaluating containment boundaries, preventing system escapes. | Routine, non-adversarial documentation tasks. |
| **Collaborative Writing** | `write_atelier` | Atelier: Muse → Drafter → Critic → Editor | Long-form documents, whitepapers, literature surveys with distinct editorial stages. | Quick summary snippets or code-only tasks. |

---

## 2. The 21 Built-in Paradigms

### A. Routing & Gatekeepers
* **`approval_gate`**: Three-stage evaluation pipeline (Deny rules win unconditionally, Allow rules auto-approve low-risk actions, residual escalates to human gate).

### B. Iterative Refinement & Loops
* **`build_fix_loop`**: Tight verification loop where implementer and evaluator alternate against a frozen test suite.
* **`generator_critic`**: Classic two-seat generator/critic loop with bounded iteration and quality thresholds.
* **`co_scientist_tournament`**: 6-seat scientific tournament (Generation, Reflection, Ranking, Evolution, Proximity, Meta-review).
* **`debate_elo`**: Iterative competitive debate where arguments are scored and ranked via ELO pairings.

### C. Sequential Chains & Pipelines
* **`pipeline_chain`**: Standard deterministic pipeline where each stage passes an immutable artifact to the next.
* **`software_sop_chain`**: MetaGPT-style standard operating procedure (Requirements Analyst → Architect → Engineer → QA).
* **`plan_and_execute`**: Two-phase execution decoupling high-level planning from mechanical task execution.
* **`agent_handoff`**: Dynamic delegation chain where agents hand off tasks based on domain specialization.

### D. Deliberation & Councils
* **`planning_council`**: Multi-seat expert board evaluating trade-offs before authoring a unified roadmap.
* **`retro_council`**: Post-mortem retrospective council analyzing campaign failure telemetry to update institutional memory.
* **`amendment_review`**: Legislative review body that deliberates and votes on constitutional rule changes.

### E. Parallel Fan-Out & Swarms
* **`fan_out_gather`**: Parallel workers executing partitioned subtasks, synchronized at an aggregation barrier.
* **`survey_gather`**: Parallel literature retrieval with deduplication, citation checking, and evidence assembly.
* **`brainstorm_swarm`**: High-entropy parallel divergence exploring broad solution spaces before convergence.

### F. Adversarial & Safety
* **`adversarial_pair`**: Proponent and adversary challenging every claim with mandatory counter-evidence.
* **`debate`**: Structured multi-agent debate with an independent judge evaluating logical validity.
* **`blast_radius`**: Adversarial red-teaming measuring potential damage and safety confinement boundaries.
* **`standing_watch`**: Ambient background sentinel auditing actions for policy compliance.

### G. Specialized Workbenches
* **`robin_lab`**: Multi-agent laboratory workbench for biophysical and chemical experimentation workflows.
* **`write_atelier`**: Multi-role writing studio (Muse, Lead Writer, Methods Writer, Citation Librarian).

---

## 3. Institutional Guarantees (The Three Invariants)

Regardless of which paradigm you choose, SciTeam mechanically enforces three invariants:
1. **Separation of Powers**: A seat that holds `emits_exit_artifact` can **never** also hold `may_assess_round`. Self-review is structurally illegal at load time.
2. **Deterministic Mutation Resistance**: Every paradigm manifest must survive mutation testing (`sciteam mutate`) with a score of `1.00`. Any rule that can be corrupted without failing load validation is rejected.
3. **Cryptographic Traceability**: All actions produce hash-chained `EventLog` records, exportable to standalone HTML via `sciteam ledger view`.
