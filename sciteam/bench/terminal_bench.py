"""Terminal-Bench 2.x driver: a Harbor external agent (docs/20-harness.md §11.2).

Harbor owns the task container (docker compose, service `main`) and runs the verifier after
`run` returns. We attach our persistent shell to that container with `docker exec -i`, so the
harness sees the same sandbox semantics as in SWE-bench. Usage:

    harbor run -d terminal-bench/terminal-bench-2-1 \
        --agent sciteam.bench.terminal_bench:SciTeamAgent --ak model=deepseek-flash -k 5 -n 8
"""

import asyncio
import json
import time
import tomllib
from pathlib import Path

from harbor.agents.base import BaseAgent
from harbor.agents.options import AgentOptions
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

import sciteam
from sciteam.harness import Harness, HarnessConfig
from sciteam.kernel import BlobStore, ConfigStore, EventStore, new_id
from sciteam.providers.llm import load_models, make_provider
from sciteam.providers.sandbox import DockerSandbox, DockerSandboxConfig

DEFAULT_CONFIGS = Path(sciteam.__file__).resolve().parents[1] / "configs"

TASK_TEMPLATE = """<task>
{instruction}
</task>

You are in the task's environment; the shell starts in {workdir}. Complete the task so that its requirements hold in this environment's final state, verify them, then call finish."""  # noqa: E501


class SciTeamOptions(AgentOptions):
    harness: str = "harness/terminal-default.yaml"
    model: str | None = None
    configs_dir: str | None = None
    # Seconds kept free before Harbor's agent timeout for the closing call and teardown.
    wall_margin_s: float = 90
    wall_margin_fraction: float = 0.1


async def _docker_out(*args: str) -> str:
    proc = await asyncio.create_subprocess_exec("docker", *args, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT)
    out, _ = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"docker {' '.join(args)} failed: {out.decode(errors='replace')}")
    return out.decode().strip()


async def find_main_container(environment: BaseEnvironment) -> str:
    from harbor.environments.docker.docker import _sanitize_docker_compose_project_name

    project = _sanitize_docker_compose_project_name(environment.session_id)
    ids = (await _docker_out("ps", "-q", "--filter", f"label=com.docker.compose.project={project}",
                             "--filter", "label=com.docker.compose.service=main")).split()
    if len(ids) != 1:
        raise RuntimeError(f"expected one main container for compose project {project}, got {ids}")
    return ids[0]


def agent_timeout_s(environment: BaseEnvironment, logs_dir: Path) -> float | None:
    task_toml = Path(environment.environment_dir).parent / "task.toml"
    try:
        base = tomllib.loads(task_toml.read_text()).get("agent", {}).get("timeout_sec")
    except OSError:
        return None
    if base is None:
        return None
    mult = 1.0
    job_cfg = logs_dir.parent.parent / "config.json"
    if job_cfg.exists():
        cfg = json.loads(job_cfg.read_text())
        mult = cfg.get("agent_timeout_multiplier") or cfg.get("timeout_multiplier") or 1.0
    return float(base) * float(mult)


class SciTeamAgent(BaseAgent):
    options_model = SciTeamOptions

    @staticmethod
    def name() -> str:
        return "sciteam"

    def version(self) -> str | None:
        return sciteam.__version__

    async def setup(self, environment: BaseEnvironment) -> None:
        return None

    def _load_config(self) -> tuple[ConfigStore, HarnessConfig]:
        opts: SciTeamOptions = self.options or SciTeamOptions()
        configs = ConfigStore(Path(opts.configs_dir) if opts.configs_dir else DEFAULT_CONFIGS)
        cfg = configs.load(opts.harness, HarnessConfig)
        if opts.model:
            cfg.model = opts.model
        return configs, cfg

    async def run(self, instruction: str, environment: BaseEnvironment,
                  context: AgentContext) -> None:
        opts: SciTeamOptions = self.options or SciTeamOptions()
        configs, cfg = self._load_config()
        timeout = agent_timeout_s(environment, self.logs_dir)
        if timeout:
            margin = max(opts.wall_margin_s, opts.wall_margin_fraction * timeout)
            cfg.budget.wall_clock_s = min(cfg.budget.wall_clock_s, timeout - margin)
        cid = await find_main_container(environment)
        workdir = environment.task_env_config.workdir or (
            await _docker_out("inspect", "-f", "{{.Config.WorkingDir}}", cid)) or "/"
        user = environment._resolve_user(None)
        sb = DockerSandbox(DockerSandboxConfig(
            image="", workdir=workdir, attach_container=cid,
            user=str(user) if user is not None else None,
            env=environment._merge_env(None) or {}))
        model_info = load_models(configs.base / "models.yaml")[cfg.model]
        llm = make_provider(model_info)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        store = EventStore(self.logs_dir / "events.db")
        blobs = BlobStore(self.logs_dir / "blobs")
        stream = f"tb/{environment.session_id}/{new_id('try')}"
        t0 = time.monotonic()
        h = None
        try:
            await sb.start()
            h = Harness(cfg, model_info=model_info, llm=llm, sandbox=sb, store=store, blobs=blobs,
                        system_prompt=configs.load_text(cfg.system_prompt_ref))
            task = TASK_TEMPLATE.format(instruction=instruction.strip(), workdir=workdir)
            res = await h.run(task, stream=stream, meta={"bench": "terminal-bench",
                                                         "session": environment.session_id})
            context.metadata = {"outcome": res.outcome, "reason": res.reason,
                                "steps": res.steps, "compactions": res.compactions}
        finally:
            if h is not None:
                t = h.state.totals
                context.n_input_tokens = t.prompt_tokens
                context.n_cache_tokens = t.cache_hit_tokens
                context.n_output_tokens = t.completion_tokens
                context.cost_usd = t.cost_usd
                context.metadata = {**(context.metadata or {}), "stream": stream,
                                    "harness": cfg.name, "model": cfg.model,
                                    "wall_budget_s": cfg.budget.wall_clock_s,
                                    "wall_s": round(time.monotonic() - t0, 1)}
            await sb.close()
            await llm.aclose()
            store.close()
