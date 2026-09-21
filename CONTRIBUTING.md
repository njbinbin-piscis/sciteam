# Contributing

Thanks for looking. Before writing code, please read the one thing that
decides where a change belongs.

## Most contributions should be plugins, not pull requests

`sciteam/` is a small frozen base. New model backends, tools, domain
integrations and coordination templates are designed to arrive as separately
installable packages via entry points — see [`docs/PLUGIN-GUIDE.md`](docs/PLUGIN-GUIDE.md)
and the working example in
[`examples/plugins/sciteam-plugin-example/`](examples/plugins/sciteam-plugin-example/).

| You want to | Where it goes |
|---|---|
| support another model or agent framework | a plugin (`register_runtime_port`) |
| add a tool | a plugin (`register_tool`) |
| add a coordination pattern | a manifest, shipped by a plugin (`register_paradigm_dir`) |
| implement a domain port | a plugin (`register_port`) |
| observe or block institutional events | a plugin (`on(...)`) |
| fix a bug in the institution kernel | a PR here |
| make an existing mechanism more robust | a PR here |
| improve docs, tests, packaging | a PR here |
| add an extension point the API lacks | open an issue first — it changes a frozen contract |

If you find yourself needing to edit `sciteam/` to add something the table
above says should be a plugin, that is a gap in the plugin API. Please open
an issue describing the use case rather than working around it; the fix is
usually a new registration method, which benefits everyone.

## Development setup

```bash
git clone https://github.com/njbinbin-piscis/sciteam
cd sciteam
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest -q
```

Before opening a PR, all three must pass:

```bash
ruff check sciteam harness tests
ruff format --check sciteam harness tests
python -m pytest -q
```

CI additionally runs the suite on Python 3.11–3.13, tests the example plugin
as a separately installed package, and verifies that a freshly built wheel
installed into a clean virtualenv — no dev dependencies, no checkout on
`sys.path` — reproduces the canonical run's truth table. If you touch
packaging or anything that loads bundled data, run that last one locally:

```bash
python -m build --wheel --outdir dist
python3 -m venv /tmp/clean && /tmp/clean/bin/pip install dist/*.whl
cd /tmp && /tmp/clean/bin/sciteam demo --mode full
```

## Conventions

**Never assume a repository layout.** Bundled manifests, prompts, skills and
schemas live inside the package under `sciteam/data/` and are resolved
through `sciteam._paths`. `Path(__file__).parents[1] / "assets"` works in a
checkout and breaks in every real install; a test fails the build if it
reappears.

**Tests assert structural invariants, not model wording.** Unit tests use
fakes and in-memory stores; no test may make a network call, need
credentials, or depend on a model's phrasing. If you change an institution
mechanism, add the invariant test alongside it — "the unauthorized seat's
submission does not count under any scheduling order" is a test, "the output
mentions the bug" is not.

**Keep the canonical run intact.** `tests/test_canonical_institution_demo.py`
pins the truth table the paper cites (unauthorized submission rejected,
authorized veto → mechanical reopen, legal close, the three ablations'
distinct failure modes, `interpreter_hash_unchanged`). A change that alters
it needs to explain why in the PR, not just update the expected values.

**Sizes.** Files under ~800 lines, classes ~300, functions ~60. Two modules
currently exceed this and are being split; please do not add to them.

**Comments** explain a constraint the code cannot show. Not what the next
line does, not where the change came from. Identifiers and comments are in
English.

**Dependencies.** Runtime dependencies are `pyyaml` and `jsonschema`. Adding
a third needs a justification in the PR. Test-only dependencies are easier,
but the bar is still "could this be a fake instead".

## Commits and PRs

Conventional Commits: `type(scope): subject` — `feat`, `fix`, `refactor`,
`test`, `docs`, `chore`, `ci`. One logical change per PR; a refactor and a
behaviour change in the same diff are hard to review and harder to revert.

Update `CHANGELOG.md` under `## [Unreleased]` for anything user-visible. If
you changed the plugin registration surface, read
[`docs/COMPATIBILITY.md`](docs/COMPATIBILITY.md) first — it requires a
version bump, a migration note, and an edit to the pinned contract test.

## Reporting things

Bugs and feature requests: GitHub issues. A minimal manifest plus the
command you ran is worth more than a description. Security defects: see
[`SECURITY.md`](SECURITY.md) — please do not use public issues.

## License

Contributions are accepted under the MIT license, same as the project.
