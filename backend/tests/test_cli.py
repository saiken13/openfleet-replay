import json
from pathlib import Path

import httpx
import pytest

from openfleet_cli.checks import CheckConfig, evaluate
from openfleet_cli.main import EXIT_ERROR, EXIT_FAILED, EXIT_OK, main

EXAMPLE_CONFIG = Path(__file__).resolve().parents[2] / "examples" / "regression.yaml"


def summary(scan_avg=15.0, scan_p05=14.0, failed=0.0, critical=0, cpu=40.0, status="SUCCESS"):
    return {
        "run": {
            "id": "r1",
            "robot_id": "robot-01",
            "software_version": "1.0",
            "status": status,
        },
        "duration_s": 120.0,
        "metrics": {
            "samples": 120,
            "cpu_avg": cpu,
            "cpu_max": cpu + 10,
            "mem_avg": 50.0,
            "mem_max": 60.0,
            "battery_min": 70.0,
        },
        "topics": [
            {
                "topic": "/scan",
                "avg_hz": scan_avg,
                "p05_hz": scan_p05,
                "failed_pct": failed,
                "degraded_pct": 0.0,
            },
            {"topic": "/odom", "avg_hz": 50.0, "p05_hz": 49.0, "failed_pct": 0, "degraded_pct": 0},
        ],
        "incidents_by_severity": {"INFO": 0, "WARNING": 0, "CRITICAL": critical},
        "incidents": [{"rule": "topic_rate"}] if critical else [],
    }


def results_by_name(s, cfg, base=None):
    return {r.name: r for r in evaluate(s, CheckConfig.model_validate(cfg), base)}


# --- pure evaluation -----------------------------------------------------------------------


def test_topic_thresholds() -> None:
    cfg = {"topics": {"/scan": {"min_avg_hz": 10, "min_p05_hz": 5, "max_failed_pct": 0}}}
    good = results_by_name(summary(), cfg)
    assert all(r.passed for r in good.values()) and len(good) == 3

    bad = results_by_name(summary(scan_avg=8, scan_p05=2, failed=5), cfg)
    assert not any(r.passed for r in bad.values())
    assert bad["topic /scan avg_hz"].detail == "8 (limit >= 10)"


def test_missing_topic_fails_only_if_required() -> None:
    required = results_by_name(summary(), {"topics": {"/camera": {"min_avg_hz": 1}}})
    optional = results_by_name(
        summary(), {"topics": {"/camera": {"min_avg_hz": 1, "required": False}}}
    )
    assert not required["topic /camera"].passed
    assert optional["topic /camera"].passed


def test_incident_and_status_checks() -> None:
    cfg = {
        "incidents": {"max_critical": 0, "forbidden_rules": ["topic_rate"]},
        "run": {"allowed_status": ["SUCCESS"]},
    }
    r = results_by_name(summary(critical=1, status="FAILED"), cfg)
    assert not r["incidents CRITICAL"].passed
    assert not r["incidents forbidden rules"].passed
    assert not r["run.status"].passed


def test_baseline_comparison() -> None:
    cfg = {"baseline": {"max_topic_avg_hz_drop_pct": 10, "max_new_critical": 0}}
    base = summary(scan_avg=15)
    r = results_by_name(summary(scan_avg=14), cfg, base)  # 6.7% drop: ok
    assert r["baseline topic /scan avg_hz drop %"].passed
    r = results_by_name(summary(scan_avg=12, critical=1), cfg, base)  # 20% drop, +1 critical
    assert not r["baseline topic /scan avg_hz drop %"].passed
    assert not r["baseline new CRITICAL incidents"].passed


def test_unknown_config_keys_are_rejected() -> None:
    with pytest.raises(ValueError):
        CheckConfig.model_validate({"topics": {"/scan": {"min_hz": 10}}})


def test_example_config_is_valid() -> None:
    import yaml

    CheckConfig.model_validate(yaml.safe_load(EXAMPLE_CONFIG.read_text()))


# --- CLI end to end (HTTP mocked) ----------------------------------------------------------


def transport_for(summaries: dict[str, dict]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        run_id = request.url.path.split("/")[4]
        if run_id in summaries:
            return httpx.Response(200, json=summaries[run_id])
        return httpx.Response(404, json={"detail": "not found"})

    return httpx.MockTransport(handler)


@pytest.fixture
def cfg_file(tmp_path: Path) -> Path:
    p = tmp_path / "regression.yaml"
    p.write_text("topics:\n  /scan:\n    min_avg_hz: 10\nincidents:\n  max_critical: 0\n")
    return p


def test_cli_exit_codes(cfg_file: Path, capsys) -> None:
    t = transport_for({"good": summary(), "bad": summary(scan_avg=4, critical=2)})
    assert main(["check", "good", "-c", str(cfg_file)], t) == EXIT_OK
    assert "2/2 checks passed" in capsys.readouterr().out
    assert main(["check", "bad", "-c", str(cfg_file)], t) == EXIT_FAILED
    out = capsys.readouterr().out
    assert "[FAIL] topic /scan avg_hz" in out and "0/2 checks passed" in out


def test_cli_errors_exit_2(cfg_file: Path, tmp_path: Path, capsys) -> None:
    t = transport_for({"good": summary()})
    assert main(["check", "missing", "-c", str(cfg_file)], t) == EXIT_ERROR
    assert "not found" in capsys.readouterr().err
    assert main(["check", "good", "-c", str(tmp_path / "nope.yaml")], t) == EXIT_ERROR
    bad = tmp_path / "bad.yaml"
    bad.write_text("topics: {/scan: {min_hz: 3}}\n")
    assert main(["check", "good", "-c", str(bad)], t) == EXIT_ERROR


def test_cli_json_output_with_baseline(cfg_file: Path, capsys) -> None:
    cfg_file.write_text("baseline:\n  max_topic_avg_hz_drop_pct: 5\n")
    t = transport_for({"base": summary(scan_avg=15), "cand": summary(scan_avg=12)})
    code = main(["check", "cand", "-c", str(cfg_file), "--baseline", "base", "--json"], t)
    assert code == EXIT_FAILED
    payload = json.loads(capsys.readouterr().out)
    assert payload["run_id"] == "cand"
    assert [r["passed"] for r in payload["results"]] == [False, True]  # /scan dropped, /odom ok
