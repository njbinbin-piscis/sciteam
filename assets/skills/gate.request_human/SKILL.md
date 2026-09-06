# SKILL: gate.request_human

**Purpose**: lawfully escalate to the human gate when work is genuinely
blocked — instead of fabricating progress, looping, or silently giving up.
Honest escalation is a legal delivery.

**Inputs**: the blocking condition (missing resource, contradictory
constraints, retries exhausted) and the record of what was already attempted.

**Outputs**: one escalation containing a diagnosis, the attempts made, and a
single concrete question or decision the operator must supply.

**Steps**
1. Verify the blocker is real: re-read the exit contract, the task card, and
   the actual paths/files involved before escalating.
2. Record each alternative tried and why it failed (evidence, not narrative).
3. State precisely what you need from the human: a decision, a resource, or an
   authorization — one ask, answerable in one message.
4. Stop work on the blocked item. Do not simulate or pad results while the
   gate is pending.

**Failure modes**: escalating instead of reading the contract; vague asks
("please advise"); continuing to emit artifacts for the blocked item after
escalation.

**Stop**: escalation emitted with diagnosis + attempts + one concrete ask;
the blocked work item is paused.
