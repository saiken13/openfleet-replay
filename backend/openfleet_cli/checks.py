"""Regression checks over a run summary (GET /runs/{id}/summary). Pure: no I/O.

The CLI never re-implements detection (CONSTRAINTS); it only asserts on what the server
already computed.
"""

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TopicCheck(_Strict):
    min_avg_hz: float | None = None
    min_p05_hz: float | None = None
    max_failed_pct: float | None = Field(None, ge=0, le=100)
    max_degraded_pct: float | None = Field(None, ge=0, le=100)
    required: bool = True  # fail if the topic never appeared in the run


class MetricsCheck(_Strict):
    max_avg_cpu_pct: float | None = None
    max_peak_cpu_pct: float | None = None
    max_avg_mem_pct: float | None = None
    max_peak_mem_pct: float | None = None
    min_battery_pct: float | None = None


class IncidentsCheck(_Strict):
    max_critical: int | None = Field(None, ge=0)
    max_warning: int | None = Field(None, ge=0)
    forbidden_rules: list[str] = []


class RunCheck(_Strict):
    allowed_status: list[str] | None = None
    min_duration_s: float | None = None
    min_samples: int | None = None


class BaselineCheck(_Strict):
    max_topic_avg_hz_drop_pct: float | None = Field(None, ge=0)
    max_new_critical: int | None = Field(None, ge=0)
    max_avg_cpu_increase_pct_points: float | None = None


class CheckConfig(_Strict):
    """regression.yaml"""

    topics: dict[str, TopicCheck] = {}
    metrics: MetricsCheck = MetricsCheck()
    incidents: IncidentsCheck = IncidentsCheck()
    run: RunCheck = RunCheck()
    baseline: BaselineCheck = BaselineCheck()


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    detail: str


def _cmp(name: str, actual: float | None, limit: float, kind: str) -> CheckResult:
    if actual is None:
        return CheckResult(name, False, f"no data (limit {kind} {limit:g})")
    ok = actual >= limit if kind == ">=" else actual <= limit
    shown = f"{actual:g}" if float(actual).is_integer() else f"{actual:.2f}"
    return CheckResult(name, ok, f"{shown} (limit {kind} {limit:g})")


def evaluate(
    summary: dict[str, Any], cfg: CheckConfig, baseline: dict[str, Any] | None = None
) -> list[CheckResult]:
    results: list[CheckResult] = []
    topics = {t["topic"]: t for t in summary.get("topics", [])}

    # run
    run = summary["run"]
    if cfg.run.allowed_status is not None:
        ok = run["status"] in cfg.run.allowed_status
        results.append(
            CheckResult(
                "run.status", ok, f"{run['status']} (allowed: {', '.join(cfg.run.allowed_status)})"
            )
        )
    if cfg.run.min_duration_s is not None:
        results.append(
            _cmp("run.duration_s", summary.get("duration_s"), cfg.run.min_duration_s, ">=")
        )
    if cfg.run.min_samples is not None:
        results.append(
            _cmp("run.samples", summary["metrics"]["samples"], cfg.run.min_samples, ">=")
        )

    # topics
    for name, tc in cfg.topics.items():
        t = topics.get(name)
        if t is None:
            results.append(
                CheckResult(f"topic {name}", not tc.required, "never reported in this run")
            )
            continue
        for field, key, kind in (
            ("min_avg_hz", "avg_hz", ">="),
            ("min_p05_hz", "p05_hz", ">="),
            ("max_failed_pct", "failed_pct", "<="),
            ("max_degraded_pct", "degraded_pct", "<="),
        ):
            limit = getattr(tc, field)
            if limit is not None:
                results.append(_cmp(f"topic {name} {key}", t[key], limit, kind))

    # metrics
    m = summary["metrics"]
    for field, key, kind in (
        ("max_avg_cpu_pct", "cpu_avg", "<="),
        ("max_peak_cpu_pct", "cpu_max", "<="),
        ("max_avg_mem_pct", "mem_avg", "<="),
        ("max_peak_mem_pct", "mem_max", "<="),
        ("min_battery_pct", "battery_min", ">="),
    ):
        limit = getattr(cfg.metrics, field)
        if limit is not None:
            results.append(_cmp(f"metrics {key}", m.get(key), limit, kind))

    # incidents
    by_sev = summary.get("incidents_by_severity", {})
    if cfg.incidents.max_critical is not None:
        results.append(
            _cmp("incidents CRITICAL", by_sev.get("CRITICAL", 0), cfg.incidents.max_critical, "<=")
        )
    if cfg.incidents.max_warning is not None:
        results.append(
            _cmp("incidents WARNING", by_sev.get("WARNING", 0), cfg.incidents.max_warning, "<=")
        )
    if cfg.incidents.forbidden_rules:
        fired = sorted({i["rule"] for i in summary.get("incidents", [])})
        bad = [r for r in fired if r in cfg.incidents.forbidden_rules]
        results.append(
            CheckResult(
                "incidents forbidden rules",
                not bad,
                f"fired: {', '.join(bad)}" if bad else "none fired",
            )
        )

    if baseline is not None:
        results += _baseline(summary, baseline, cfg.baseline)
    return results


def _baseline(
    summary: dict[str, Any], base: dict[str, Any], bc: BaselineCheck
) -> list[CheckResult]:
    results = []
    if bc.max_topic_avg_hz_drop_pct is not None:
        cand_topics = {t["topic"]: t for t in summary.get("topics", [])}
        for t in base.get("topics", []):
            c = cand_topics.get(t["topic"])
            name = f"baseline topic {t['topic']} avg_hz drop %"
            if c is None:
                results.append(CheckResult(name, False, "topic missing from candidate run"))
                continue
            if not t["avg_hz"]:
                continue
            drop = 100.0 * (t["avg_hz"] - c["avg_hz"]) / t["avg_hz"]
            results.append(
                CheckResult(
                    name,
                    drop <= bc.max_topic_avg_hz_drop_pct,
                    f"{t['avg_hz']:.2f} → {c['avg_hz']:.2f} Hz ({drop:+.1f}% drop, "
                    f"limit {bc.max_topic_avg_hz_drop_pct:g}%)",
                )
            )
    if bc.max_new_critical is not None:
        new = summary["incidents_by_severity"].get("CRITICAL", 0) - base[
            "incidents_by_severity"
        ].get("CRITICAL", 0)
        results.append(_cmp("baseline new CRITICAL incidents", new, bc.max_new_critical, "<="))
    if bc.max_avg_cpu_increase_pct_points is not None:
        b, c = base["metrics"].get("cpu_avg"), summary["metrics"].get("cpu_avg")
        inc = None if b is None or c is None else c - b
        results.append(
            _cmp("baseline cpu_avg increase", inc, bc.max_avg_cpu_increase_pct_points, "<=")
        )
    return results
