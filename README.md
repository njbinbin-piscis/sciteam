# SciTeam

An institutional multi-agent system for research work.

Agents occupy seats. An action that changes the system is an **Act**, and it takes effect only after a single **Gate** decides it under the current rule version. The model does not decide what counts. Validators are commands run in a clean environment. Communication policy, institution rules, and the organization manifest are configuration files; changing them is how collaboration patterns change, not by forking the interpreter.

The previous public runtime is kept on [`abandoned/sciteam-v1`](https://github.com/njbinbin-piscis/sciteam/tree/abandoned/sciteam-v1). This repository is a rewrite started 2026-09-23. It does not import that code. Research background, including the paper draft that the abandoned branch was built for, is in [`docs/background/`](docs/background/README.md).

## Status

Five layers, each accepted on a real model before the next one was written. Design: [`docs/00-overall-design.md`](docs/00-overall-design.md). Acceptance record: [`docs/91-roadmap.md`](docs/91-roadmap.md).

| Layer | What | Status |
|---|---|---|
| 1 | Kernel, providers, single-agent harness | Closed. SWE-bench Verified × flash 414/500; Terminal-Bench 2.1 361/428 |
| 2 | Messages, rooms, tasks, scheduler | Closed (C1–C6) |
| 3 | Act, Gate, counts-as, validators, ledger, amendment | Closed (I1–I6) |
| 4 | Founder, charter, hire, environment certificate, goals | Closed (O1–O5) |
| 5 | Experiments that vary only configuration | In progress (`docs/60-experiment.md`) |

## Layout

```
kernel ← providers ← harness ← comm / runtime ← institution ← org ← experiment
```

Lower packages do not import upper ones. The kernel has no department, role, or topology enums; those live under `configs/`.

## Install

```bash
pip install -e ".[dev]"
```

Set `DEEPSEEK_API_KEY` for live runs (see `configs/models.yaml`). Unit tests do not need a key:

```bash
pytest
```

Live tests are marked `live` and are off by default.

## Commands

```bash
sciteam trace <run>
sciteam team start <team.yaml> --run <dir>
sciteam team decide <run> --as <member> <act-id> approve
sciteam exp run <experiment.yaml> --out <dir>
sciteam exp report <dir>
```

Team files are paths relative to `configs/`, for example `teams/minipkg-flat.yaml`.

## 中文

设计文档是中文的，从 [`docs/00-overall-design.md`](docs/00-overall-design.md) 读起。研究主张的快照在 [`docs/background/`](docs/background/README.md)。旧的公开运行时在分支 `abandoned/sciteam-v1`，只供复现上一版论文草稿里的脚本化存在性证明。

## License

MIT. Copyright (c) 2026 cpu-aipic.
