"""Builds the nightly report (docs/BENCHMARKS.md) from the JSON the nightly jobs leave behind:

    edge_bench --json          bench-*.json             protocol/gateway micro-benchmarks
    run_scenarios.py           scenarios/*.json         edge scenarios against compose
    firmware.robot             firmware-metrics.json    Renode figures
                               robot_output.xml         Robot Framework results

    python scripts/nightly_report.py ARTIFACTS_DIR [-o report.md] [--json report.json]
                                     [--baseline PREVIOUS_RESULTS_DIR]

It finds those files anywhere under ARTIFACTS_DIR. Every figure keeps the environment label
it was measured in (native / qemu-user / renode / compose on a VM); figures from different
environments are never compared with each other. With --baseline (a directory holding an
earlier run's bench-*.json, e.g. the previous nightly's, restored from the CI cache), each
benchmark is shown next to that run's figure for the *same* environment and architecture,
as a ratio.
That comparison is informational: shared CI runners are noisy, so the report flags large
changes but only correctness (failed tests or scenarios) fails the job.
"""

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

FLAG_RATIO = 1.5  # slower than the baseline by this factor: flagged in the report


def load_json(paths: list[Path]) -> list[dict]:
    out = []
    for p in paths:
        try:
            out.append(json.loads(p.read_text()))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"warning: skipping {p}: {exc}", file=sys.stderr)
    return out


def robot_results(paths: list[Path]) -> list[dict]:
    tests = []
    for p in paths:
        try:
            root = ET.parse(p).getroot()
        except (OSError, ET.ParseError) as exc:
            print(f"warning: skipping {p}: {exc}", file=sys.stderr)
            continue
        for t in root.iter("test"):
            status = t.find("status")
            tests.append(
                {
                    "name": t.get("name", "?"),
                    "status": status.get("status") if status is not None else "?",
                    "message": (status.text or "").strip()[:200] if status is not None else "",
                }
            )
    return tests


def bench_key(b: dict) -> str:
    return f"{b.get('env')}|{b.get('arch')}"


def build(root: Path, baseline: list[dict], exclude: Path | None = None) -> tuple[str, dict]:
    def results(pattern: str) -> list[Path]:  # never mistake the baseline for this run
        skip = exclude.resolve() if exclude else None
        return sorted(
            f for f in root.rglob(pattern) if skip is None or not f.resolve().is_relative_to(skip)
        )

    benches = load_json(results("bench-*.json"))
    scenarios = [s for s in load_json(sorted(root.rglob("scenarios/*.json"))) if "scenario" in s]
    firmware = load_json(sorted(root.rglob("firmware-metrics.json")))
    robot = robot_results(sorted(root.rglob("robot_output.xml")))
    base = {bench_key(b): {r["name"]: r for r in b["results"]} for b in baseline}

    failures: list[str] = []
    md: list[str] = ["# OpenFleet nightly report", ""]
    md.append(
        "Every figure is labelled with where it was measured. Emulated figures (qemu-user, "
        "Renode) describe the emulator, not a CPU; nothing here ran on robot hardware."
    )
    md.append("")

    # --- correctness first --------------------------------------------------------------
    md += ["## Results", "", "| suite | passed | failed |", "|---|---|---|"]
    if robot:
        bad = [t for t in robot if t["status"] != "PASS"]
        md.append(f"| Renode Robot Framework (firmware) | {len(robot) - len(bad)} | {len(bad)} |")
        failures += [f"robot: {t['name']}: {t['message']}" for t in bad]
    if scenarios:
        bad_s = [s for s in scenarios if not s["passed"]]
        md.append(f"| edge scenarios (compose) | {len(scenarios) - len(bad_s)} | {len(bad_s)} |")
        failures += [f"scenario {s['scenario']}: {s.get('error', '')}" for s in bad_s]
    md.append("")
    if failures:
        md += ["**Failures**", ""] + [f"- {f}" for f in failures] + [""]

    # --- benchmarks ---------------------------------------------------------------------
    flagged: list[str] = []
    if benches:
        md += ["## Micro-benchmarks (edge_bench)", ""]
        for b in benches:
            ref = base.get(bench_key(b), {})
            md.append(f"**{b['env']}**, {b['arch']}, {b['compiler']}, {b['build_type']}")
            md.append("")
            md.append("| case | ns/op | ops/s | MB/s | vs baseline |")
            md.append("|---|---:|---:|---:|---:|")
            for r in b["results"]:
                vs = ""
                if r["name"] in ref:
                    ratio = r["ns_per_op"] / ref[r["name"]]["ns_per_op"]
                    vs = f"{ratio:.2f}x"
                    if ratio >= FLAG_RATIO:
                        vs += " ⚠"
                        flagged.append(f"{b['env']}/{b['arch']} {r['name']}: {ratio:.2f}x slower")
                md.append(
                    f"| {r['name']} | {r['ns_per_op']:.1f} | {r['ops_per_s']:,.0f} | "
                    f"{r['mb_per_s']:.1f} | {vs} |"
                )
            md.append("")
        if baseline:
            md.append(
                f"Ratios are time per operation against the previous run's figure for the "
                f"same environment and architecture (> 1 = slower; ⚠ at {FLAG_RATIO}x). "
                f"Informational only: shared runners are noisy."
            )
            md.append("")

    # --- firmware (Renode) --------------------------------------------------------------
    for fw in firmware:
        md += [
            "## Firmware on Renode",
            "",
            f"_{fw.get('note', '')}_",
            "",
            "| metric | value | what |",
            "|---|---:|---|",
        ]
        for name, m in fw["metrics"].items():
            md.append(f"| {name} | {m['value']:,.1f} {m['unit']} | {m.get('what', '')} |")
        md.append("")

    # --- scenarios ----------------------------------------------------------------------
    if scenarios:
        md += ["## Edge scenarios (compose)", ""]
        env = scenarios[0]["env"]
        md.append(f"Agent: {env['agent']}. Host: {env['host']}. {env['note']}")
        md.append("")
        md += ["| scenario | device | metric | value | basis |", "|---|---|---|---:|---|"]
        for s in scenarios:
            device = s["env"]["device"].split(" (")[0]
            rows = list(s["metrics"].items())
            if "redetect" in s:
                d = s["redetect"]
                rows.append(
                    (
                        "redetect",
                        {
                            "value": f"caught {d['caught']} / missed {d['missed']} "
                            f"/ new {d['new']}",
                            "unit": "",
                            "basis": "replay",
                        },
                    )
                )
            if not rows:
                rows = [("(none)", {"value": "", "unit": "", "basis": ""})]
            for i, (name, m) in enumerate(rows):
                label = s["scenario"] + ("" if s["passed"] else " (FAILED)") if i == 0 else ""
                value = f"{m['value']} {m['unit']}".strip()
                md.append(
                    f"| {label} | {device if i == 0 else ''} | {name} | {value} | "
                    f"{m.get('basis', '')} |"
                )
        md.append("")

    if flagged:
        md += ["## Flagged (informational)", ""] + [f"- {f}" for f in flagged] + [""]
    if not (benches or scenarios or firmware or robot):
        md.append("_No results found._")
        failures.append("no results found")

    summary = {
        "failures": failures,
        "flagged": flagged,
        "benches": benches,
        "firmware": firmware,
        "scenarios": scenarios,
        "robot": robot,
    }
    return "\n".join(md) + "\n", summary


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("root", type=Path)
    p.add_argument("-o", "--output", type=Path, default=Path("nightly-report.md"))
    p.add_argument("--json", type=Path)
    p.add_argument("--baseline", type=Path)
    args = p.parse_args()
    baseline: list[dict] = []
    if args.baseline and args.baseline.is_dir():
        baseline = load_json(sorted(args.baseline.rglob("bench-*.json")))
    md, summary = build(args.root, baseline, args.baseline)
    args.output.write_text(md)
    if args.json:
        args.json.write_text(json.dumps(summary, indent=2) + "\n")
    print(md)
    if summary["failures"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
