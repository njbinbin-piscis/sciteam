from datetime import UTC, datetime, time

from sciteam.providers.llm.types import ModelInfo, Usage


def _parse_range(r: str) -> tuple[time, time]:
    a, b = r.split("-")
    return time.fromisoformat(a.strip()), time.fromisoformat(b.strip())


def is_peak(model: ModelInfo, ts: datetime) -> bool:
    # Chinese public holidays are billed off-peak by the provider but treated as peak here,
    # so the estimate can only overstate cost.
    ts = ts.astimezone(UTC)
    if model.peak_weekdays_only and ts.weekday() >= 5:
        return False
    t = ts.time()
    return any(a <= t < b for a, b in map(_parse_range, model.peak_hours_utc))


def cost_usd(usage: Usage, model: ModelInfo, ts: datetime) -> float:
    if "peak" in model.pricing_usd_per_mtok and "off_peak" in model.pricing_usd_per_mtok:
        tier = model.pricing_usd_per_mtok["peak" if is_peak(model, ts) else "off_peak"]
    else:
        tier = next(iter(model.pricing_usd_per_mtok.values()))
    hit = usage.cache_hit_tokens
    miss = usage.cache_miss_tokens or max(usage.prompt_tokens - hit, 0)
    return (hit * tier.cache_hit + miss * tier.cache_miss + usage.completion_tokens * tier.output) / 1e6
