from sciteam.experiment.metrics import run_metrics
from sciteam.kernel.events import Event
from sciteam.runtime.config import RuntimeConfig, TeamSandboxConfig, benchmark_problems


def _validated(**kw):
    import yaml
    from pathlib import Path
    base = yaml.safe_load((Path(__file__).parents[2] / "configs/runtime/default.yaml").read_text())
    sb = kw.pop("sandbox", TeamSandboxConfig(kind="docker", image="x@sha256:abc",
                                             network="bridge"))
    base.update(kw)
    base["sandbox"] = sb.model_dump()
    return RuntimeConfig.model_validate(base)


def test_default_is_realworld_and_keeps_network():
    rt = _validated()
    assert rt.mode == "realworld" and rt.sandbox.network == "bridge"


def test_benchmark_forces_offline():
    rt = _validated(mode="benchmark")
    assert rt.sandbox.network == "none"


def test_benchmark_start_checks():
    ok = _validated(mode="benchmark")
    assert benchmark_problems(ok) == []
    unpinned = _validated(mode="benchmark",
                          sandbox=TeamSandboxConfig(kind="docker", image="x:latest"))
    assert any("digest" in p for p in benchmark_problems(unpinned))
    local = _validated(mode="benchmark", sandbox=TeamSandboxConfig(kind="local"))
    assert any("docker" in p for p in benchmark_problems(local))
    inst = _validated(mode="benchmark", sandbox=TeamSandboxConfig(
        kind="docker", image="x@sha256:a", init_commands=["pip install numpy"]))
    assert any("install" in p for p in benchmark_problems(inst))
    assert benchmark_problems(_validated()) == []


def test_interjection_excludes_run_from_table():
    def ev(t, p):
        return Event.model_construct(type=t, payload=p)
    started = ev("run_started", {"mode": "benchmark", "config": {}})
    m = run_metrics([started], {}, None, {})
    assert m["mode"] == "benchmark" and m["excluded_from_table"] is False
    m = run_metrics([started, ev("run_interjected", {})], {}, None, {})
    assert m["excluded_from_table"] is True
