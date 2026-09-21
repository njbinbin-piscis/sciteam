# sciteam-sandbox-docker

Official Docker container sandbox plugin for [SciTeam](https://github.com/njbinbin-piscis/sciteam).

Provides an isolated, containerized execution environment implementing SciTeam's
`SandboxPort` protocol (`run` and `certify`). Enables tool executions, program evaluations,
and candidate models to run in hermetically sealed Linux containers rather than on the host.

## Institutional Guard Model

Resource and security constraints are driven directly by institutional manifest `guard` declarations:
* **Network Isolation**: Default `--network none` (hermetic, zero outbound leaks).
* **Resource Quotas**: Strict memory ceilings (`--memory <MB>m`) and CPU caps (`--cpus <N>`).
* **Process Confinement**: Linux capability dropping (`--cap-drop ALL`), PID limits (`--pids-limit 64`), and read-only root filesystems.
* **Workspace Confinement**: Only the declared task workspace is mounted into `/workspace`. Any path escaping the workspace is rejected before spawning.
* **Institutional Pre-Execution Hook**: Every `docker_sandbox_run` tool call is audited by a registered `tool_call` hook preventing parameter-level escape vectors.

## Installation

```bash
pip install sciteam-sandbox-docker
```

## Usage

```python
from pathlib import Path
from sciteam.sandbox import SandboxRequest
from sciteam_sandbox_docker import DockerSandbox, ContainerGuardSpec

# Create sandbox with custom or default guard
sandbox = DockerSandbox(
    workspace=Path("./my_workspace"),
    guard=ContainerGuardSpec(memory_mb=512, network="none"),
)

result = sandbox.run(
    SandboxRequest(
        program="evaluate.py",
        inputs=("data.csv",),
        outputs=("metrics.json",),
    )
)
print("Execution succeeded:", result.ok)
```
