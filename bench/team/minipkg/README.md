# minipkg (confkit) acceptance scoring

Hidden acceptance suite for the `confkit` task (spec: `configs/workspaces/minipkg/SPEC.md`).

- `hidden_tests/` — pytest suite, grouped by spec section; imports only the public `confkit` API.
- `reference/confkit/` — stdlib-only reference implementation; exists only to prove the tests.
- `score.py` — scorer.

## Scoring

```bash
. .venv/bin/activate
python bench/team/minipkg/score.py <workspace_dir> [--json out.json]
```

`<workspace_dir>` is the directory containing `confkit/`. The scorer copies `hidden_tests/` to a
fresh temp dir, runs pytest there with `PYTHONPATH=<workspace_dir>` (600 s timeout), and prints
`{"passed", "failed", "errors", "total", "score", "by_file", "failures"}`. The total is fixed by
collecting against the reference, so tests that cannot run (e.g. `confkit` not importable) count
as failed. The workspace's own `tests/` are never collected.

Sanity check: `python bench/team/minipkg/score.py bench/team/minipkg/reference` must give `score: 1.0`.

## Spec ambiguities (deliberately not tested)

`SPEC.md` stays frozen so runs remain comparable; points it leaves open are not scored.

1. Unquoted list items (`[a, b]`): §2.1 rule 6 types items "by rules 1–5" only.
2. Text form of lists with string items under interpolation (§3 vs §7 quoting).
3. Floats whose `repr` has no `.` (`1e+16`, `inf`, `nan`) under the §7 round-trip promise.
4. Key order inside a section in `dumps`.
5. `x = 1` then `x += 2` within one text: §4 says a non-list earlier value raises `MergeError`,
   but also that within a text "`+=` may follow an `=` of the same key (the append applies to
   it)", which reads as an exception. Removed after 4 of 5 pro teams took the second reading
   with an explicit justification; the cross-layer form (`x = 1` in one text, `x += 2` in a
   later one) is unambiguous and stays tested.

## Confidentiality

Never copy `hidden_tests/` or `reference/` (or excerpts of them) into agent-visible places:
workspaces, prompts, task configs, or logs shown to agents. Agents get only `SPEC.md`.
