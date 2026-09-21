**What this changes**

**Why**

**Checklist**

- [ ] `ruff check` / `ruff format --check` / `pytest` pass locally
- [ ] Tests assert structural invariants, not model wording
- [ ] `CHANGELOG.md` updated under `## [Unreleased]` if user-visible
- [ ] No new `Path(__file__).parents[1]` data-path assumption
- [ ] If the plugin registration surface changed: `PLUGIN_API_VERSION`
      bumped, migration note written, pinned contract test updated
      (see `docs/COMPATIBILITY.md`)
- [ ] If `tests/test_canonical_institution_demo.py` changed: the PR
      explains why the paper's truth table moves
