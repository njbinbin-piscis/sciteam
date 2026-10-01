> 快照：自研究工作区 `paper/rebuildV2/23-arxiv-draft-v1.md` 复制（2026-10-01）。
> 文中的 canonical run 与复现脚本属于已废弃的上一版运行时（分支 `abandoned/sciteam-v1`）。
> 本仓库用真实模型重做同一组机制，见 `docs/60-experiment.md` 的 X-inst。
> 文中指向其他研究笔记与 `paper/lab/` 的路径不在本仓库内。

# 23 · arXiv 首版草稿 v1

> 状态：v1（第四十九轮）。本文件是 `00-charter.md` §1/§5/§6 冻结主张的
> 论文化展开，是**唯一**要投 arXiv 的正文草稿。语言：英文（arXiv 惯例），
> 结构注释用中文标出，便于讨论时定位。
>
> §8「Rescued Historical Evidence」已用 `22-evidence-package-v1.md` 的
> 逐项稽核结果回填（六条可用证据 + 明确排除项），措辞已按稽核发现的
> 名实不符（如 `camp_mini_kv_reentry2` 实为 plan revision 而非
> reentry）逐一修正。正文其余部分（定义、架构、canonical run、消融、
> 划界）本就不依赖 §8 独立成立——这是刻意的写法顺序：canonical run 是
> 存在性论证的主证据，历史证据只是补充机制证据。
>
> 复现入口：`paper/lab/harness/run_canonical_institution_demo.py`
> （`--mode {full,prompt_only,no_separation,no_recovery}`，`--config-swap`）。
> 本文 §5–§7 引用的所有日志行、verdict JSON 均为该脚本本轮真实运行的
> 逐字输出（`paper/lab/runs/canonical_institution_demo/*/summary.json`），
> 无删改、无编造数字。

---

## Title

**Institution Engineering for LLM Agent Systems: From Agents in Workflows to Agents in Institutions**

## Abstract (draft)

Recent work has organized LLM agent systems along an increasingly explicit
engineering lineage: prompts, then context assembly, then harness/loop
control, then explicit task/agent/state graphs ("Graph Engineering"). Each
step externalizes what used to be implicit into an artifact that can be
inspected, versioned, and reused across agents. We argue this lineage is
incomplete: a graph fixes *what runs next*, but says nothing about *who is
authorized to act*, *which outputs count as valid*, *which rule version
governs a given decision*, or *how the rules themselves may be legitimately
changed*. We call the missing layer **Institution Engineering**: treating
seats, authority, validity ("counts-as"), and amendment procedure as
first-class, executable, machine-enforced artifacts, and treating their
specification, validation, execution, observation, revision, and regression
testing as an explicit engineering lifecycle. We do not claim to be first to
externalize governance for agent systems — Electronic Institutions,
Institutional AI, POLIS, ArbiterOS, and MAS-Lab all precede us on specific
dimensions (§9). Our contribution is a reference architecture, realized in a
running system (SciTeam), in which agent seats' outputs gain system-level
validity only through institutional rules rather than through the identity
of whoever occupies the seat. We report a **canonical run**, currently
executed with scripted, deterministic seats as a zero-cost existence-proof
tier (a live-LLM version is scoped as immediate follow-up, not silently
substituted — §11), in which a real repo-level bug-fix task is closed
through authority checks, a mechanical audit-veto-triggered reopen, and a
version-governed institutional amendment — with the interpreter's code held
byte-identical throughout (E0 = 0). We report three structural ablations
that isolate the contribution of mechanical validity checking, separation
of powers, and a legal recovery path, and a configuration-swap
demonstration that changes closing rules by editing only the institution
manifest. We do not claim this improves task success rates, generalizes
across protocols, or constitutes a formal safety guarantee; we do not yet
claim the canonical run itself involves live, open-ended LLM agents — we
claim that the mechanisms it exercises are the same mechanisms a live-agent
run would exercise, and that a live-agent run is the direct, already-scoped
next step, not a different design.

---

## 1. Introduction

### 1.1 An engineering lineage, not a single trick

Building agent systems on top of LLMs has gone through a sequence of
externalizations, each one taking something that used to live implicitly
inside a single long prompt and turning it into its own explicit,
inspectable artifact:

| Stage | What got externalized | Representative pattern |
|---|---|---|
| Prompt | Task instructions | One long prompt string |
| Context | What the model sees at each step | Retrieval, context assembly, memory |
| Harness / Loop | Tool-call iteration control | ReAct-style loops, agent harnesses |
| Graph | Task decomposition, agent coordination, runtime state | Explicit DAGs of tasks/agents/state ("Graph Engineering", "System Intelligence") |
| **Institution** | **Who may act, what counts, which rule version applies, how rules change** | **This paper** |

Each stage did not replace the previous one; it added a layer of
externalized structure on top. Graph Engineering's own contribution
statement is instructive: it organizes *task organization*, *agent
coordination*, and *runtime state management* as an explicit graph, and
explicitly lists authority, provenance, recovery, and governance as **future
work**, not as part of the graph itself. We take that gap seriously and fill
it, rather than folding it into a graph node.

**Why this gap is becoming urgent rather than incidental (motivation, not
evidence).** Frontier model releases are increasingly marketed around
general-purpose, highly autonomous capability — for instance OpenAI's
September 2026 GPT-6 Astra release, whose president described it as
plausibly marking an "AGI era" (a framing OpenAI's own CEO had called
overloaded marketing language days earlier, so we treat it as an industry
signal, not an adjudicated scientific fact). We cite this only to motivate
timing: as individual agents are claimed to approach higher, more
general-purpose competence, the open bottleneck for *systems* built from
many such agents shifts from "can one agent do the task" toward "how do
many independently capable, standing-bearing agents interact under shared,
revisable rules." Higher single-agent capability does not by itself answer
that question one way or the other — it is a distinct architectural
question from single-agent competence, and it is the one this paper's
reference architecture (§3–§4) targets. We do not treat any capability
announcement as evidence for the architecture we propose; §5–§8 remain the
paper's only evidence.

### 1.2 The gap: graphs decide what runs, not what counts

A graph — however dynamic, however richly annotated — answers "what runs
next." It does not, by itself, answer:

- *Standing*: is this particular agent instance, in this particular role,
  allowed to produce an output that has system-level effect?
- *Counts-as*: does a given output, once produced, actually change the
  system's state of record, or is it just text that a downstream node
  happens to read?
- *Versioning*: which version of the rules was in force when this decision
  was made, and can that be reconstructed later?
- *Amendment*: when the rules themselves need to change, is that change
  itself subject to a rule-governed process (proposal, review, shadow
  evaluation, promotion), or is it just a code edit?

Workflow orchestration and Graph Engineering can express any of these things
*ad hoc*, by adding more nodes and more conditional edges. Our claim is not
that this is impossible; it is that doing so on a per-graph basis puts
authority logic in the same place as task logic, and that separating them
into an institution — a distinct, versioned, machine-interpreted artifact —
is a better engineering decomposition, in the exact same sense that
separating a task graph from prompt text was a better decomposition one
step earlier in the lineage.

**This does not obsolete Graph Engineering, and we do not claim it does.**
The relationship is necessity, not competition. Our own reference
architecture (§3–§4) does not eliminate the graph substrate — the
institution interpreter schedules seats over an explicit plan graph
(`AdaptiveCampaignPlanner`, a DAG of mission nodes with dependencies,
revisions, and reopen edges, §4); the institution adds authority, validity,
versioning, and amendment on top of that graph, it does not replace it.
Any sufficiently complex agent system needs an explicit graph of what runs
next regardless of whether it also needs an institution; only a system with
multiple principals whose standing and output legitimacy must be governed
additionally needs an institution layered on top. A single-agent coding
assistant with a tool-call loop has no such need, and Graph Engineering (or
a plain harness/loop) remains the correct, sufficient answer for that class
of system — it should not carry institutional machinery it does not use. We
therefore read the lineage in §1.1 as cumulative, not as sequential
replacement: Institution Engineering is the layer that becomes necessary
once a system crosses into multi-principal, accountability-bearing
territory, not a universal upgrade that makes the previous layer obsolete.

### 1.3 Contributions

This paper delivers, and only claims to deliver, an **existence
demonstration**, not a completeness survey:

1. A frozen, internally consistent definition of Institution, Institutional
   Multi-Agent System (I-MAS), and Institution Engineering (§2).
2. A four-layer reference architecture — Institution Specification,
   Institution Interpreter, Agent Adapter, Institution Ledger — realized in
   a working system, SciTeam (§3–§4).
3. A canonical run — currently a scripted, zero-cost existence-proof tier,
   with a live-LLM version scoped as immediate follow-up (§11) — in which
   multiple seats collectively close a real, frozen-test-gated bug-fix
   task, with an unauthorized submission rejected, a late audit failure
   mechanically reopening upstream work, and a legal close recorded against
   an unchanged interpreter (§5).
4. Three structural ablations — no mechanical validity check, no separation
   of powers, no legal recovery path — that isolate what each mechanism
   contributes by removing it and observing the resulting failure mode
   (§6).
5. A configuration-swap demonstration in which a single-approver rule
   becomes a two-of-two committee rule by editing only the institution
   manifest, with the interpreter and worker adapters byte-identical
   (§7).
6. An honest inventory of which historical experimental readings from
   earlier phases of this project survive re-audit for contamination and
   which do not (§8).
7. An explicit, per-item boundary against the five nearest neighboring
   literatures, so that no claim in this paper can be mistaken for a claim
   already established elsewhere (§9).

We explicitly do **not** claim primitive completeness, protocol coverage,
task-performance improvement, cross-backend superiority, or any formal
safety guarantee (§10).

### 1.4 A boundary note: a question this paper deliberately does not ask

Institutional text could, in principle, have effect on an agent through two
very different mechanisms: (a) a *mechanical boundary*, where an
interpreter external to the agent checks and enforces a rule regardless of
what the agent understands or intends — this is what our own authority and
validity checks do (e.g., §5.3's bystander rejection is a code-level check,
not a request an LLM might refuse or comply with); or (b) a *norm an agent
complies with because it read and understood the institutional text*, with
no mechanical backing at all. This paper is entirely about (a): every
result in §5–§7 is a hard, code-enforced boundary, exercised so far with
scripted seats. We deliberately do not investigate (b) here, and we do not
mix the two kinds of claim in one paper. Whether LLM agents comply with
institutional norms *because they understand and grant them legitimacy* —
as opposed to complying with any instruction a system prompt happens to
contain, which would not be a distinctly institutional phenomenon at all —
is a genuinely different research question, requiring its own literature
grounding (symbol grounding, speech-act theory, institutional-fact theory)
and its own discriminating experimental design (at minimum, a matched pair
that states the same rule once as a versioned, amendment-governed
institutional rule and once as an unattributed ad hoc suggestion, to see
whether compliance strength, persistence, or self-justification differs by
institutional framing alone). We register this as a distinct, separately
scoped follow-up line rather than as an extension of this paper's own
claims.

---

## 2. Definitions

> 与 `00-charter.md` §1 完全一致，正文为该节的英文冻结版，不得漂移。

**Institution.** A rule system, independent of any particular executor and
shared across multiple agents, that specifies: which **seats** exist; what
**authority** each seat holds; which actions are permitted; which produced
artifacts **count as** valid system-level effects; how failure, veto,
escalation, and reopening are handled; which rule **version** governs a
given decision; and how the rules themselves may be legitimately amended.

**Institutional Multi-Agent System (I-MAS).** A multi-agent system in which
agents do not merely play a role described in a prompt, but **occupy an
institutional seat**: their output acquires system-level validity only
after passing the seat's authority and validity checks, as enforced by an
interpreter external to the agent itself.

**Institution Engineering.** The engineering discipline of treating an
institution's full lifecycle — specification, validation, execution,
observation, revision, and regression testing — as first-class, executable,
versioned artifacts, with tooling (shadow evaluation, mechanical rollback,
amendment ledgers) supporting each stage.

**The core distinguishing sentence, used throughout for boundary work:**

> A workflow or graph decides *what runs next*. An institution additionally
> decides *who is authorized to act*, *what counts*, *which rule version
> applies*, and *how the rules may legitimately change*.

This is a distinction of **what is being decided**, not of implementation
substrate: an institution can be, and in our reference implementation is,
*realized on top of* a DAG-shaped execution plan (§3). The claim is that not
every DAG is an institution — a DAG becomes one exactly when authority,
counts-as, versioning, and amendment are added as enforced, first-class
properties of the plan rather than being left to prompt text or ad hoc code.

---

## 3. Reference Architecture

Four layers, each independently testable:

| Layer | Responsibility | SciTeam realization |
|---|---|---|
| **Institution Specification** | Seats, authority, protocol, exit contract, guard conditions, re-entry rules, amendment/version format | `assets/institutions/`, `schemas/amendment_proposal.schema.json`, `sciteam/coordination.py` |
| **Institution Interpreter** | Schedules seats, enforces validity ("counts-as") checks, rejects unauthorized actions, drives the amendment pipeline | `sciteam/orchestrator.py`, `sciteam/amendment.py`, `harness/run_org_series.py` |
| **Agent Adapter** | Binds worker backends (own / pi / dsh) to seats; the worker itself never holds institutional effect, only the seat does | `sciteam/heartbeat.py` + per-backend worker adapters |
| **Institution Ledger** | Records proposals, approvals, vetoes, rule versions, evidence, reopen and final-effect events | `runs/_org/<arm>/amendments.jsonl`, `runs/<id>/institutions/ledger.jsonl` |

The separation between Interpreter and Agent Adapter is what lets §7's
config-swap demonstration hold the interpreter byte-identical while the
institution's *effect* changes: authority and validity live in the
Interpreter layer, driven by data in the Specification layer, never in the
Adapter.

## 4. Institution Lifecycle: Closing the Loop

An institution that can only be specified and executed, but never validated
before deployment, revised, or regression-tested, is not yet engineered —
it is a one-shot configuration. This project's own history bore this out:
earlier phases of SciTeam shipped structural shadow evaluation and an
amendment ledger, but three load-bearing mechanisms were missing or
incomplete. Closing them was this paper's second deliverable (`close-
system-loop`, tracked separately from the canonical run):

1. **Behavioral shadow evaluation** (`sciteam/behavioral_shadow.py`). Prior
   to this work, `run_shadow_eval.py` always recorded a `null` behavioral
   verdict — an amendment could pass *structural* shadow (it parses, it is
   internally consistent) while silently widening or narrowing authority
   with no mechanical check. `evaluate_authority_behavior` now instantiates
   every paradigm's authority predicate (`is_artifact_role`) against its
   declared seats, for both the incumbent and the candidate asset tree, and
   `run_behavioral_shadow` flags a regression whenever the candidate breaks
   an authority property the incumbent held. This turns "does the candidate
   rule change actually change behavior, and in which direction" into a
   decidable pass/fail/indeterminate verdict recorded in the ledger, not a
   claim taken on the amendment author's word.

2. **Mechanical, authorized reopen** (`sciteam/audit_veto.py`). The engine
   already had `REOPEN_UPSTREAM` and `SUPERSEDE` as plan primitives, but
   nothing translated an audit's veto into one automatically. We defined a
   schema-valid veto signal (`schemas/audit_veto.schema.json`), a scanner
   that fails closed on malformed signals, and `VetoUpstreamReviser`, a
   `PlanReviser` that mechanically emits `REOPEN_UPSTREAM` for every veto
   signed by a seat holding `may_veto_exit` authority (checked against
   `TeamConfig.may_veto_roles()`), ignores unauthorized signals outright,
   and tracks which vetoes it has already actioned so a fixed veto file
   is never replayed on every planner tick. `CompositeReviser` was added to
   `adaptive_planner.py` so this can be composed with any other reviser the
   campaign already uses, rather than requiring a single monolithic
   reviser.

3. **Mechanical rollback** (`sciteam/amendment.py::rollback_amendment`). A
   `rolled_back` ledger state existed as a label with no code path that
   produced it. `apply_amendment` now archives full before/after asset
   content at promotion time; `rollback_amendment` reverses a promoted
   amendment strictly from that archive, refusing to run unless the ledger
   status is exactly `promoted`, the archive exists, and the asset's current
   content still matches the archived post-amendment text (i.e., it fails
   closed on drift rather than silently clobbering an unrelated later
   edit).

All three are covered by unit tests independent of the canonical run
(`tests/test_behavioral_shadow.py`, `tests/test_audit_veto.py`,
`tests/test_amendment.py`), and the veto→reopen mechanism is additionally
exercised end-to-end inside the canonical run itself (§5).

---

## 5. Canonical Run

> **Disclosure, stated here rather than only in §11:** the run reported in
> this section uses **scripted, deterministic seats**, not live LLM calls.
> It is this project's zero-cost existence-proof tier, run first by its own
> stated discipline; a live-LLM canonical run with the same manifest is the
> immediate next funded step (§11), not a claim already made here.

### 5.1 Task

A minimal but real repo-level bug-fix task with a frozen, held-out test
suite (`harness/run_canonical_institution_demo.py`): `calc.py` contains
`apply_discount(price, pct)`, whose known bug is that it does not clamp
`pct`, so a discount over 100% produces a negative price instead of
flooring at zero. `test_calc.py` is frozen and includes exactly the
regression case (`pct=150` → expected `0.0`) alongside two non-regression
cases.

### 5.2 Seats and manifest

| Seat | Authority |
|---|---|
| `implementer` | May write `calc.py` (the only `emit_role`); a submission from any other identity is checked against this and rejected |
| `auditor` | May run the frozen test suite and, if it fails, issue an authorized veto (`may_veto_exit`); may approve closure |
| `chair` | Closes the case once the approval rule (`{kind: single, roles: [auditor], threshold: 1}`) is satisfied |
| `bystander` | Holds no seat; used to demonstrate that an unauthorized identity's submission is never granted effect |

### 5.3 What actually happened (verbatim log, `full` mode)

```
bystander: attempted submission, authorized=False (expected: False)
implementer(attempt=1): wrote patch, submission authorized (seat=implementer)
auditor(attempt=1): frozen tests passed=False
auditor: wrote authorized veto targeting m_implement
implementer(attempt=2): wrote patch, submission authorized (seat=implementer)
auditor(attempt=2): frozen tests passed=True
chair: closed_legally=True (approvals={'auditor': True}, rule={'kind': 'single', 'roles': ['auditor'], 'threshold': 1})
```

The planner's feedback ledger (`plan_snapshot()["feedback_events"]`)
independently records the mechanical reopen this log line refers to:
`{"action": "REOPEN_UPSTREAM", "target": "m_implement", "reason_code":
"late_test_failure", "requested_by": "auditor"}` — i.e. the reopen is not
just narrated in a log string, it is a structured planner-level event
attributable to the authorized seat that triggered it.

### 5.4 Verdict and invariants checked

```json
{
  "closed_legally": true,
  "approval_rule": {"kind": "single", "roles": ["auditor"], "threshold": 1},
  "approvals": {"auditor": true},
  "tests_passed": true
}
```

- The bystander's submission attempt was checked against the manifest and
  logged as `authorized=False` before any patch write was permitted to
  count.
- The first (buggy) patch's failure against the frozen suite produced an
  authorized veto, which mechanically produced a `REOPEN_UPSTREAM` event
  targeting the implementer's step — not merely a log message, a planner-
  level ledger event.
- The second (fixed) patch passed the frozen suite and the chair's closure
  is legal under the manifest's approval rule.
- `interpreter_hash_before == interpreter_hash_after` (`_interpreter_hash()`
  over `sciteam/amendment.py`, `sciteam/audit_veto.py`,
  `sciteam/adaptive_planner.py`, `sciteam/behavioral_shadow.py`,
  `sciteam/worker_common.py`, and the harness script itself): **E0 = 0**
  across the whole run — nothing about the interpreter changed to make this
  specific run succeed.

## 6. Three Structural Ablations

Each ablation removes exactly one mechanism from the canonical run and
observes the resulting failure mode, holding the underlying buggy-then-
fixed task fixed. All numbers below are verbatim from this round's runs.

### 6.1 `prompt_only` — no mechanical validity check

The chair accepts the implementer's own narrative claim ("tests pass, fix
is complete") without ever invoking the frozen test suite.

```json
{
  "verdict": {"closed_legally": true, "tests_passed": null},
  "ground_truth_tests_actually_passed": false
}
```

**Failure mode demonstrated:** the case closes legally under the manifest's
approval rule while the underlying bug is still present. This isolates
exactly what mechanical validity checking (§3's Interpreter layer enforcing
`counts-as` against a real check, not a claim) contributes: without it, an
institution's authority and approval-rule machinery can still run to
completion and still produce a false "legally closed."

### 6.2 `no_separation` — implementer also holds auditor authority

The same seat that wrote the patch also reviews it. Critically, we did not
model this ablation as "the same objective check, run by a differently-
named seat" — that would not demonstrate anything, since a deterministic
frozen-test run produces the same result regardless of who invokes it. The
actual institutional risk separation of powers guards against is that
**nothing else forces the objective check to run at all** once author and
reviewer are the same seat; we model the self-review as a shallow syntax-
only check (`ast.parse`, no behavioral test execution) rather than the
frozen suite:

```json
{
  "verdict": {"closed_legally": true, "tests_passed": true},
  "ground_truth_tests_actually_passed": false
}
```

**Failure mode demonstrated:** the case closes legally on a check that
never actually exercised the frozen behavioral contract, while the real
frozen suite (run separately, for the paper, but not consulted
institutionally) still fails. This isolates what an independent auditor
seat contributes: it is not that a different seat computes a different
number, it is that *institutionally requiring* an independent seat is what
forces the real check to be the one consulted for closure.

### 6.3 `no_recovery` — no legal reopen path

The planner is constructed with no reviser at all (`reviser=None`), so a
veto file is written on audit failure, but nothing translates it into a
`REOPEN_UPSTREAM` action.

```json
{
  "verdict": {"closed_legally": false, "reason": "no_legal_reopen_path", "tests_passed": false}
}
```

**Failure mode demonstrated:** the campaign has no legal move left even
though a correct fix was one implementer turn away — the planner offers no
next mission distinct from re-presenting the same, already-failed state.
This isolates what mechanical veto→reopen (§4, item 2) contributes: without
it, a legitimate late failure is indistinguishable, institutionally, from a
permanently stuck campaign.

### 6.4 Summary table

| Mode | `closed_legally` | Real bug fixed? | Failure isolated |
|---|---|---|---|
| `full` (canonical) | **true** | **true** | — (baseline) |
| `prompt_only` | true (false positive) | false | mechanical validity checking |
| `no_separation` | true (false positive) | false | separation of powers |
| `no_recovery` | false (stuck) | n/a | legal recovery path |

The three ablations produce three *different* failure modes, not one
generic "ablation makes it worse" result — this is the evidence that each
of the three mechanisms is doing distinct, non-redundant work.

## 7. Configuration-Swap Demonstration

We change the closing rule from single-auditor approval to a two-of-two
committee (`{kind: committee, roles: [auditor_1, auditor_2], threshold:
2}`) by editing only the institution manifest passed into `run_full`. No
line of `sciteam/*.py` or the harness script changes; `interpreter_hash`
is computed over the same file set as §5.4.

```json
{
  "manifest": {
    "approval_rule": {"kind": "committee", "roles": ["auditor_1", "auditor_2"], "threshold": 2}
  },
  "verdict": {
    "closed_legally": true,
    "approvals": {"auditor_1": true, "auditor_2": true},
    "tests_passed": true
  },
  "interpreter_hash_unchanged": true
}
```

This is the paper's operationalization of T3 ("mode-as-policy"): the
demonstrable engineering property is that a genuine coordination-pattern
change — from single-approver to committee — is expressible entirely as
institution-manifest data, with the interpreter and worker adapters
untouched. We do not claim this generalizes to arbitrary policy changes;
we claim this one, concrete, checked instance holds.

---

## 8. Rescued Historical Evidence

> 稽核方法与判定纪律见 `00-charter.md` §2「旧数字重新评估纪律」；本节
> 是 `22-evidence-package-v1.md`（**已经两轮独立稽核交叉复核**：初次
> 稽核给出候选清单，二次独立稽核直接核对原始 JSON/git 对象，纠正了
> 初次稽核里两处未经字段级核验的错误——一处 E0 污染场次被误当清洁
> 场次引用，两个失败使命名字张冠李戴——本节按二次稽核后的纠正版本
> 撰写）九节结果的最终收录。被排除的污染读数（Elo、跨臂效率、"审计
> 全绿⇒制度有效"、误标的 `reentry=true`）一律不出现在本节或本文任何
> 其他地方。**这些历史证据是机制/存在性的辅助材料，本文的主证据是
> §5–§7 本轮新跑的 canonical run**；§5–§7 本身也已被二次稽核重新定性
> 为"机制级脚本原型"（见下条 E8），measure 与措辞已在正文中同步收紧。

Re-audited on 2026-09-06, twice independently, against `paper/lab` at
engine tag `engine-cert-gen2` (commit `bd7829c3082f90d43fdbbd76f471b5bed1e4e9c8`).
The current working tree also carries substantial **uncommitted** changes on
top of that tag, including every mechanism described in §4 and the
canonical-run harness itself in §5–§7; nothing in this section or in §5–§7
should be read as already belonging to a tagged, certified release.

1. **Paradigm-coverage structural gate (38/38, and a corrected count for
   the rest of the suite).** The eight coordination patterns taxonomized by
   an external classification (Huang & Zhou, arXiv:2605.13850) are each a
   subset of this project's paradigm template set, checked by
   `tests/test_taxonomy_patterns.py`, `tests/test_coverage_paradigms.py`,
   and `tests/test_paradigms.py` — 38 tests, re-run today, still 38/38
   passing, zero LLM cost, on files this round did not touch. This is
   expressiveness/coverage evidence, not institutional-mechanism evidence.
   Separately, this round added or extended 68 institution-mechanism tests
   (`test_asset_closure.py`, `test_amendment.py`'s new rollback cases,
   `test_audit_veto.py`, `test_behavioral_shadow.py`,
   `test_canonical_institution_demo.py`, and others) — all currently
   uncommitted, all 68/68 passing today. Neither count should be conflated
   with "the whole suite is green": the full `paper/lab` suite is currently
   **816/829 passing**, with the same 7 pre-existing failures identified in
   both audit passes, none touching any file this section or §4–§7 cites.
2. **A clean 16-paradigm contract-closure run exists; an earlier 15-
   paradigm run does not qualify.** `c2_own_wave_ab_rebuild` and
   `c2_pi_wave_ab_rebuild` each closed 14 of 16 paradigm missions under
   contract with `E0=0` (own's failures: `m_agent_handoff`,
   `m_approval_gate`; pi's failures: `m_software_sop_chain`,
   `m_approval_gate`) — an earlier 15-paradigm campaign
   (`paradigm_matrix_llm_wab_20260804T190231Z`, 14/15 closed) is **not**
   admitted as clean E0 evidence: its own `engine_diff_stat.txt` records
   `engine_changed_during_campaign: true`, self-disclosed by the operator
   as a mid-run hot-fix to gate-clearing logic. We report only the 16-
   paradigm pair, over a subset of the 22-template library, with both
   named failures disclosed.
3. **Two rounds of negative-result-driven plan revision** (not "reentry").
   `camp_mini_kv_reentry2_20260731T055214Z` performed two real
   `adaptive_planner` plan revisions (`m_hyp_r2→m_build_r2`, then
   `m_hyp_r3→m_build_r3`), triggered by an explicit `science_loop=on`
   negative-frozen-eval condition, with `E0=0` throughout. Despite its
   directory name, this run's own `exports/mission_reentries.json` records
   `count: 0` and its trace never sets `reentry: true` — we report this as
   plan revision, explicitly *not* as a demonstration of mission reentry, a
   name-vs-mechanism mismatch both audit passes independently flagged.
4. **pi/own contract closure under one interpreter; dsh has a named gap.**
   `gen2_e4ii_mini_pi_r2` and `gen2_e4ii_mini_own_r2` each closed 5/5
   missions under contract with `E0=0`, evidence that the same institution
   interpreter binds to more than one worker backend on the same five-step
   task. `atlas-lab-dsh:gen2` is a certified image (its digest is in
   `SOURCE.md`), but this specific five-step contract-closure task was
   never run against it — this is an honest, named gap, not a claim we
   quietly avoid. (A different dsh run, `gen2_a_l1_reentry_dsh`, closes 5/5
   missions on a *different* five-mission task; it is real evidence of
   dsh's contract closure in general, but not interchangeable with the
   five-step task the pi/own pair above ran.) We also disclose the
   corresponding limitation on the admitted pi/own pair directly: the audit
   check in these runs has zero observed variance (always `passed: true`),
   so this is evidence of "no observed violation," not evidence that the
   mechanism would catch a real one — that stronger claim is exactly what
   §5's canonical run (with an authorized veto that *does* fire) supplies
   instead.
5. **A legislative veto was really exercised, against a real defect —
   not a red-team adversarial-injection experiment.** `camp_covering_gp1`
   (a Gen-1 campaign, archived under git tag `gen1-covering-20260815`,
   re-read directly from that tagged object) records: a proposal drew a
   `veto_with_reasons` verdict because the ledger's recorded content hash
   diverged from the artifact's actual on-disk bytes by one trailing
   newline; the artifact was corrected; a fresh review then returned
   `no_veto`; only then did `legislation_enacted` fire, respecting that
   ordering. We state this precisely as "an authorized reviewer seat's veto
   power was really exercised against a real traceability defect, blocking
   enactment until it was fixed" — not as evidence of a deliberately
   submitted malicious or rule-violating amendment being caught, which
   neither audit pass found any historical-run-level evidence for
   (deliberately-violating-amendment rejection is currently evidenced only
   at unit-test level, e.g.
   `test_audit_veto.py::test_unauthorized_veto_is_scanned_but_never_honored`).
6. **Organizational amendment promotion lifecycle, including negative
   cases.** Across `_org/{f1a,f2n,g1,s1}/amendments.jsonl`, L-layer and
   P-layer amendments move `drafted → shadow_pass → promoted` with recorded
   before/after content hashes and institution version identifiers;
   negative cases exist at two different rejection points —
   `amd_assembler_readback` and two `_org/g1` amendments reached
   `shadow_fail` after passing audit (evidence the shadow gate catches
   something audit does not), and two `_org/s1` amendments were `rejected`
   even earlier, at `drafted`, because that arm was frozen. The negative
   cases are reported because a promotion pipeline that never rejects
   anything is not evidence the gate does anything.

Excluded, and not referenced anywhere else in this paper: Elo/ranking
aggregation values (`sciteam/aggregation.py` documents itself as a
lexicographic stub, "not an LLM judge"), any cross-backend efficiency or
token/wall-clock comparison, the inference "audits were all green,
therefore the institution is effective," and treating
`camp_mini_kv_reentry2`'s directory name, or any dry-run trace, as evidence
of mission reentry.

Full per-item paths, both audit passes' cross-checked findings, the
corrected wab-run E0 disclosure, and the provenance-gap inventory (no
admitted item above carries a machine-checkable engine git-commit field
inside its own run directory; each must be manually cross-referenced
against the separately-tracked engine certificate) are in
`22-evidence-package-v1.md` §§0–9.

---

## 9. Boundary Against Neighboring Literature

> 与 `00-charter.md` §6 一致，为正文冻结版；任何一行主张变化必须先改
> charter，再回填本节，不允许本节先漂移。

| Neighbor | What it has actually done | How this project differs |
|---|---|---|
| **Graph Engineering** (arXiv:2608.21156) | Survey/framework paper organizing Task Organization / Agent Coordination / Runtime State Management into "System Intelligence"; discusses authority, provenance, recovery, governance as **future directions** (§5–6), not an implemented system | Graph Engineering organizes **relations** among tasks/components/state; this project organizes **authority** — who may act, what counts, how rules legitimately change. An institution can be realized on a graph, but not every graph is an institution |
| **Institutional AI** (arXiv:2601.10599 concept + 2601.11369 empirical) | Already implements externalized governance graphs, an Oracle/Controller, a versioned manifest interpreter, and an append-only governance log; demonstrates effectiveness in a Cournot collusion scenario | Its research question is incentive-compatible governance (does governance suppress collusion); this project's research question is the **institution engineering lifecycle** itself (how to construct, validate, run, and amend an institution). We do not claim priority on externalizing governance |
| **POLIS** (arXiv:2608.09828) | 5280-episode mechanism study; authority provenance, attempt/outcome separation, post-block recovery; the primary delegation workflow's actual decisions concentrate in a single planner | Treats institution as an **experimental treatment**, asking which mechanism is safer; this project treats institution as an **engineering artifact**, asking how to let multiple genuinely active agents occupy seats and produce legitimate joint action. Our canonical run requires multiple seats to be driven by distinct agent instances, not a single planner in disguise |
| **ArbiterOS** (arXiv:2510.13857) | Kernel-as-Governor, intra-agent governance; Agent Constitution + Evaluation-Driven Lifecycle (EDLC); explicitly scoped to non-inter-agent orchestration | Governs one agent's internal execution vs. governing standing, authority, and collective action among multiple principals. "Constitution-as-data" and "evaluation-driven lifecycle" are already its terms; we do not reclaim them, and restrict our distinction to the inter-agent dimension |
| **MAS-Lab** (arXiv:2606.30546) | Declarative MAS specification plus MAS-OS, covering governance/trace/replay/fault injection; three small controlled lab deployments (local) reported | Its first-class object is MAS specification and experimental infrastructure; our first-class object is the **institution** itself — office/seat, standing, counts-as, legitimate collective act, authority-preserving amendment. The distinction rests on a concrete mechanism (same model output has different effect depending on seat), demonstrated in §5–6, not asserted |
| **Classical Electronic Institutions** (ISLANDER/AMELI/EIDE tradition) | Already covers roles, interaction protocols, norms, enactment, and monitoring | We do not claim to invent electronic institutions; our contribution is adapting this tradition to **open-ended, probabilistic-output** LLM agents: an artifact contract projects open-ended output into institutional events, the same institution can bind to modern agent harnesses, and humans and LLM agents can jointly occupy seats |

---

## 10. What This Paper Does Not Claim

- We do not claim primitive completeness or coverage of all published
  coordination protocols.
- We do not claim institutions improve task success rates; SWE-bench-style
  fix rates are not evidence of "institutional effectiveness" in this
  paper.
- We do not claim to be first to externalize governance as declarative
  text — see §9.
- We do not claim any formal safety guarantee (deadlock-freedom,
  correctness-by-construction) unless an actual proof is given, which this
  paper does not attempt.
- We do not claim own/pi/dsh constitute three independent runtimes; they
  are three worker backends behind one institution interpreter, used only
  for a contract-closure smoke check.
- We do not claim agents "live" institutionally in the human sense;
  "institutional participation" here is defined relative to "working like a
  human worker," not identity, welfare, or intrinsic motivation.
- We do not claim, or attempt to test, whether LLM agents comply with
  institutional norms because they understand and grant them legitimacy as
  opposed to generically following whatever a system prompt says (§1.4);
  that is a distinct, separately scoped follow-up question (Line A.1), not
  a claim of this paper.

## 11. Limitations and Future Work

- The canonical run in §5–§7 uses **scripted seats** (deterministic Python
  callables), not live LLM calls, following this project's own zero-cost
  existence-proof-first discipline (`INSTITUTION_ITER.md` row 3) and its
  A10 pre-execution verbatim-material review requirement. A live-LLM
  canonical run — with the same manifest, the same six-seat design scaled
  from three to six seats (Registrar, Proposer ×2, Adjudicator,
  Implementer, Auditor, Chair, per `00-charter.md` §5) — is the natural,
  already-scoped next funded step, tracked as follow-up rather than
  silently substituted. Its scope, for this paper's own claims, is limited
  to confirming that the mechanical boundaries of §5–§7 (authority checks,
  veto→reopen, config-swap) still hold when seats are occupied by
  non-scripted agents; it is not designed to test §1.4's separate question,
  which is out of scope for this paper (see §1.4 and Line A.1).
- T1's capability threshold is asserted qualitatively (the canonical run's
  model/task combination clears it) but not measured as a curve; a full
  threshold sweep across model generations is left to an AAMAS/JAAMAS
  extension (`04-capability-battery.md` §1.5–6).
- The behavioral shadow mechanism (§4, item 1) currently checks one
  authority predicate family (`is_artifact_role`); extending it to
  veto-authority and approval-rule regressions is a direct, scoped next
  step, not a new research question.
- §8's rescued evidence is bounded by the provenance gaps the audit itself
  found: none of the six admitted historical items carries a machine-
  checkable git-commit field inside its own run directory (only the
  separately-tracked engine certificate does); a follow-up provenance-
  recording script is scoped but not yet built (`22-evidence-package-v1.md`
  §8).
- **Outlook, stated explicitly as conjecture, not a claim this paper
  defends or tests.** If individual agents continue to be marketed and
  deployed as higher, more general-purpose competence (§1.1), we conjecture
  that the open coordination problem for multi-agent *systems* shifts from
  designing a bespoke task graph per application toward designing,
  versioning, and revising the institution that population of agents
  operates under — graph-level orchestration becomes necessary but no
  longer sufficient once standing and rule legitimacy, not just task
  sequencing, are load-bearing. This paper does not test that conjecture:
  §5–§8 establish only that an institution layer is mechanically
  implementable and load-bearing, at small scale, with scripted or
  historically-logged seats. Whether institutions are *the* dominant
  coordination pattern for large populations of highly capable agents — as
  opposed to one useful layer among several — is a distinct empirical
  question left to future, larger-scale, live-LLM, and eventually
  multi-institution studies (v1.5/v2 territory per this project's own
  roadmap discipline, not a silent extension of this paper's claims).

---

## 12. Reproducibility

All canonical-run and ablation numbers in §5–§7 are produced by:

```bash
cd paper/lab
python harness/run_canonical_institution_demo.py --mode full
python harness/run_canonical_institution_demo.py --mode prompt_only
python harness/run_canonical_institution_demo.py --mode no_separation
python harness/run_canonical_institution_demo.py --mode no_recovery
python harness/run_canonical_institution_demo.py --mode full --config-swap
```

Each writes `runs/canonical_institution_demo/<mode>[_swap]/summary.json`.
Regression tests locking in the specific invariants cited in §5–§7 live in
`tests/test_canonical_institution_demo.py`. The three closed-loop mechanisms
from §4 are independently unit-tested in `tests/test_behavioral_shadow.py`,
`tests/test_audit_veto.py`, and `tests/test_amendment.py`.

**Code release.** The engine underlying this paper (institution manifest
loader, authority/artifact-validity checks, veto→reopen, behavioral shadow,
amendment + rollback, `RuntimePort` adapter seam) is released as a standalone,
`pip`-installable open-source package, `sciteam` (MIT license), independent of
this paper's internal research tree — <https://github.com/njbinbin-piscis/sciteam>.
The canonical run and ablations in §5–§7 are runnable from a fresh clone with
no API credentials required (scripted seats; 337 passing tests); live-LLM
campaigns are supported via the same `RuntimePort` seam given the reader's
own model credentials.
