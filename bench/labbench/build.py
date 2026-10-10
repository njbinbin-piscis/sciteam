"""Build the small contamination-controlled SeqQA panel (docs/60 §4.4).

For each computable subtask, sample N public questions (seeded) and pair each with a
perturbed twin: same question template, length and parameters (enzymes, threshold), a fresh
random sequence, the answer recomputed by a validated solver (solve.py) and required to
differ from the original answer. Distractors keep the original's offsets from the answer.
Twins differ from originals only in never having been published, so the paired score gap
estimates how much of the original score comes from memorization.

Usage: build.py SeqQA.parquet --out bench/labbench/data/seqqa_panel.jsonl [--n 8 --seed 7]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
from pathlib import Path

import pandas as pd
import solve
from Bio.Seq import Seq

INSUFFICIENT = "Insufficient information to answer the question."
SUBTASKS = [
    "Prop-seq-gcpercent-v1-public",
    "ORF-seq-numlen-v1-public",
    "RE-seq-numfrags-v1-public",
    "RE-seq-lenfrags-v1-public",
]
END_MARGIN = 10


def answer(subtask: str, q: str) -> str:
    if subtask == "RE-seq-lenfrags-v1-public":
        return ", ".join(map(str, solve.fragments(q)))
    return solve.SOLVERS[subtask](q)


def unambiguous(subtask: str, q: str) -> bool:
    """Digests whose label could depend on conventions the solver cannot check: two enzymes
    cutting at the same position, or a cut close to either end."""
    if not subtask.startswith("RE-"):
        return True
    s = Seq(solve.seq_of(q))
    cuts = [c for e in solve.enzymes_of(q) for c in e.search(s, linear=True)]
    return len(cuts) == len(set(cuts)) and all(END_MARGIN < c < len(s) - END_MARGIN for c in cuts)


def shift(subtask: str, ideal: str, distractor: str, new: str) -> str | None:
    """Move a distractor by its offset from the original answer (numeric answers only)."""
    try:
        d = int(distractor) - int(ideal)
    except ValueError:
        return None
    v = int(new) + d
    if v < 0 or (subtask.startswith("Prop-") and v > 100):
        return None
    return str(v)


def frag_distractors(rng: random.Random, new: str, k: int) -> list[str]:
    frags = [int(x) for x in new.split(", ")]
    out: list[str] = []
    while len(out) < k:
        f = frags[:]
        choice = rng.randrange(3)
        if choice == 0 and len(f) > 1:
            i = rng.randrange(len(f) - 1)
            f[i:i + 2] = [f[i] + f[i + 1]]
        elif choice == 1:
            i = rng.randrange(len(f))
            delta = rng.randint(5, 60)
            if f[i] > delta + 1:
                f[i:i + 1] = [f[i] - delta, delta]
        else:
            rng.shuffle(f)
            f = [max(1, x + rng.randint(-30, 30)) for x in f]
        cand = ", ".join(map(str, f))
        if cand != new and cand not in out:
            out.append(cand)
    return out


def twin(rng: random.Random, subtask: str, row) -> dict | None:
    old_seq = solve.seq_of(row.question)
    gc = (old_seq.count("G") + old_seq.count("C")) / len(old_seq)
    for _ in range(2000):
        seq = "".join(rng.choices("ACGT", weights=[1 - gc, gc, gc, 1 - gc], k=len(old_seq)))
        q = row.question.replace(old_seq, seq)
        if not unambiguous(subtask, q):
            continue
        new = answer(subtask, q)
        if new == row.ideal.strip() or (subtask.startswith("RE-") and len(solve.fragments(q)) < 2):
            continue
        if subtask == "RE-seq-lenfrags-v1-public":
            ds = frag_distractors(rng, new, len(row.distractors))
        else:
            ds = [shift(subtask, row.ideal, d, new) for d in row.distractors]
            if any(x is None for x in ds) or len(set(ds) | {new}) != len(ds) + 1:
                continue
        return {"question": q, "ideal": new, "distractors": ds}
    return None


def item(rng: random.Random, pid: str, subtask: str, variant: str, q: dict, source_id: str,
         slot: int) -> dict:
    """The answer goes to position `slot` (balanced by the caller); the rest are shuffled."""
    others = [*q["distractors"], INSUFFICIENT]
    rng.shuffle(others)
    options = others[:slot] + [q["ideal"]] + others[slot:]
    letters = "ABCDEFGH"
    return {
        "id": pid,
        "subtask": subtask,
        "variant": variant,
        "source_id": source_id,
        "question": q["question"],
        "options": {letters[i]: o for i, o in enumerate(options)},
        "answer": letters[options.index(q["ideal"])],
        "insufficient": letters[options.index(INSUFFICIENT)],
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("parquet", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--n", type=int, default=8)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    rng = random.Random(a.seed)
    df = pd.read_parquet(a.parquet)
    rows = []
    n_opt = 5
    slots = [i % n_opt for i in range(len(SUBTASKS) * a.n * 2)]
    rng.shuffle(slots)
    for st in SUBTASKS:
        pool = [r for _, r in df[df.subtask == st].iterrows()
                if answer(st, r.question) == r.ideal.strip() and unambiguous(st, r.question)]
        picked = rng.sample(pool, a.n)
        for k, r in enumerate(picked):
            t = twin(rng, st, r)
            if t is None:
                raise SystemExit(f"no twin for {r.id}")
            tag = st.split("-v1")[0]
            orig = {"question": r.question, "ideal": r.ideal.strip(), "distractors": list(r.distractors)}
            rows.append(item(rng, f"{tag}-{k}-orig", st, "original", orig, r.id, slots[len(rows)]))
            rows.append(item(rng, f"{tag}-{k}-twin", st, "perturbed", t, r.id, slots[len(rows)]))
            assert answer(st, t["question"]) == t["ideal"]
    a.out.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in rows)
    a.out.write_text(text)
    print(f"{len(rows)} items -> {a.out} sha256 {hashlib.sha256(text.encode()).hexdigest()[:12]}")


if __name__ == "__main__":
    main()
