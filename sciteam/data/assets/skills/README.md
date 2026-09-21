# Skills for SciTeam experiments.

Each `SKILL.md` should declare what the job needs:

```markdown
## Requires capabilities
- literature.search
- literature.audit
```

Capability ids come from `assets/tools/catalog.yaml`. Mission start compares
declarations to `RuntimeCapabilities` and fails closed on gaps. Agents must
not fill gaps with training memory.
