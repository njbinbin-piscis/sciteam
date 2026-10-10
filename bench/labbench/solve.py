"""Reference solvers for computable LAB-Bench SeqQA subtasks (docs/60 §4.4).

A solver is trusted only if it reproduces the official `ideal` answer on every public
question of its subtask (`validate`); only trusted solvers may label perturbed questions.
"""

from __future__ import annotations

import re

from Bio.Restriction import Restriction
from Bio.Seq import Seq

SEQ = re.compile(r"[ACGT]{30,}")
STOPS = ("TAA", "TAG", "TGA")


def seq_of(question: str) -> str:
    return max(SEQ.findall(question), key=len)


def gc_percent(question: str) -> str:
    s = seq_of(question)
    return str(round(100 * (s.count("G") + s.count("C")) / len(s)))


def _orfs(s: str) -> list[str]:
    """Forward-strand ATG..stop proteins in all three frames, nested starts included, stop
    codon required and not counted: the only definition that reproduces all 40 public
    ORF-seq-numlen answers (checked against both strands, nesting, stop and >= variants)."""
    out = []
    for f in range(3):
        codons = [s[i:i + 3] for i in range(f, len(s) - 2, 3)]
        for i, c in enumerate(codons):
            if c != "ATG":
                continue
            for j in range(i, len(codons)):
                if codons[j] in STOPS:
                    out.append(str(Seq("".join(codons[i:j])).translate()))
                    break
    return out


def orf_count(question: str) -> str:
    n = int(re.search(r"greater than (\d+) AAs", question).group(1))
    return str(sum(len(p) > n for p in _orfs(seq_of(question))))


def enzymes_of(question: str) -> list:
    names = re.search(r"with (?:the enzymes?|the following enzymes:) ([\w ,]+)\?", question).group(1)
    return [getattr(Restriction, x.strip()) for x in names.split(",")]


def fragments(question: str, linear: bool = True) -> list[int]:
    s = Seq(seq_of(question))
    cuts = set()
    for e in enzymes_of(question):
        cuts.update(e.search(s, linear=linear))
    pos = sorted(c - 1 for c in cuts if 0 < c - 1 < len(s))
    edges = [0, *pos, len(s)]
    return [b - a for a, b in zip(edges, edges[1:]) if b > a]


def num_frags(question: str) -> str:
    return str(len(fragments(question)))


SOLVERS = {
    "Prop-seq-gcpercent-v1-public": gc_percent,
    "ORF-seq-numlen-v1-public": orf_count,
    "RE-seq-numfrags-v1-public": num_frags,
}
