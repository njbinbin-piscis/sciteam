# Security

## Reporting a vulnerability

Report privately through
[GitHub Security Advisories](https://github.com/njbinbin-piscis/sciteam/security/advisories/new).
Please do not open a public issue for an exploitable defect. Expect an
acknowledgement within a week; this is a small project, not a vendor with an
on-call rota, and that limitation is stated here rather than implied.

## The trust model, stated plainly

SciTeam decides **which agent output counts as an institutional act**. That
is an integrity mechanism, not a containment mechanism. It is explicit about
what it does not defend against, because a governance layer that is vague
about its own boundary is worse than one that has none.

### Trusted, in-process, unsandboxed

**Plugins.** A plugin is arbitrary Python executing in your interpreter with
your privileges, discovered from your environment's installed packages. Same
trust model as `pytest` plugins. Installing a SciTeam plugin is exactly as
consequential as `pip install` of anything else. The plugin rules
("add constraints, never exemptions"; no overriding built-ins) are
**integrity** rules that keep a well-behaved plugin from quietly widening
authority — they are not a security boundary against a hostile one.

**Institution manifests, prompts, and skills.** Loading a manifest executes
no code, but it determines who may submit, who may veto, and what closes a
mission. Treat manifest files as you would production access-control config:
review them, keep them in version control, do not load one from an untrusted
source.

### Semi-trusted: model output

Everything a model returns is untrusted input. The design assumes models will
be wrong, will narrate success they did not achieve, and may be manipulated
by content they read. Concretely:

- **Verifier-owned truth.** Where a check matters, a subprocess or verifier
  owns the pass/fail, never a seat's narration. The `prompt_only` ablation
  exists to show what the same run looks like when that rule is dropped: it
  closes "legally" with the bug still in the file.
- **Authority is not persuadable.** `is_artifact_role` consults the parsed
  manifest. No amount of prompt content makes an unseated agent's output
  count.
- **Prompt injection is not solved here.** A seat that reads attacker-authored
  text can be induced to *attempt* anything. What the institution guarantees
  is that those attempts do not acquire institutional effect without passing
  authority and validity checks — bounding blast radius, not preventing the
  attempt. Anything you would not want attempted must be kept out of the
  seat's tool set.

### Tool execution

`LocalPythonSandbox` runs a program in a subprocess with **no shell**, paths
confined to a resolved workspace (escapes raise `PermissionError`), and
`RLIMIT_AS` / `RLIMIT_CPU` / `RLIMIT_NOFILE` bounds. This is a *blast-radius
reducer for accidents*, not an adversarial sandbox: there is no namespace
isolation, no seccomp filter, and no network restriction. An intentionally
hostile program is not contained by it.

For untrusted workloads, run the whole process inside a container or VM with
the isolation you actually need, and declare tools at the narrowest
`permission` (`readonly` / `side_effect` / `privileged`) that works.

### Credentials

`SCITEAM_LLM_API_KEY` and any credentials your plugins read come from the
environment. SciTeam does not write them to run artifacts, but it also does
not scrub model output: if a model echoes a secret it was shown, that text
lands in the trace like any other output. Do not put credentials in prompts,
skills, or manifests.

### Run artifacts

Run output under `./runs/` (or `$SCITEAM_RUNS_ROOT`) contains full prompts,
model output, tool results and ledger entries. It is as sensitive as the task
it ran. Nothing is written into the installed package.

## Non-goals

No multi-tenancy, no per-seat OS-level privilege separation, no plugin
capability system, no signed/verified plugin distribution, no encryption of
run artifacts at rest. If you need any of these, they belong in the layer
that hosts SciTeam, and building them is not on this project's roadmap.
