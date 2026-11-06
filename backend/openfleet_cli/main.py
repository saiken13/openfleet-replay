"""openfleet: command-line client for OpenFleet Replay.

    openfleet check RUN_ID --config regression.yaml [--baseline RUN_ID] [--api URL] [--json]
    openfleet keys create ROBOT_ID [--label TEXT] | keys list [--robot ID] | keys revoke KEY_ID

Exit codes: 0 all checks passed (or a keys command succeeded), 1 a check failed,
2 usage/config/API/database error.
"""

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

import httpx
import yaml
from pydantic import ValidationError

from openfleet_cli import keys
from openfleet_cli.checks import CheckConfig, CheckResult, evaluate

EXIT_OK, EXIT_FAILED, EXIT_ERROR = 0, 1, 2


class CliError(Exception):
    pass


def _get_summary(client: httpx.Client, run_id: str) -> dict[str, Any]:
    try:
        resp = client.get(f"/api/v1/runs/{run_id}/summary")
    except httpx.HTTPError as exc:
        raise CliError(f"cannot reach API: {exc}") from exc
    if resp.status_code == 404:
        raise CliError(f"run {run_id} not found")
    if resp.status_code != 200:
        raise CliError(f"API error {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def _load_config(path: str) -> CheckConfig:
    try:
        raw = yaml.safe_load(Path(path).read_text()) or {}
        return CheckConfig.model_validate(raw)
    except FileNotFoundError as exc:
        raise CliError(f"config not found: {path}") from exc
    except (yaml.YAMLError, ValidationError) as exc:
        raise CliError(f"invalid config {path}:\n{exc}") from exc


def _print_table(run: dict[str, Any], results: list[CheckResult]) -> None:
    print(f"Run {run['id']}  robot={run['robot_id']}  version={run['software_version']}")
    width = max((len(r.name) for r in results), default=10)
    for r in results:
        mark = "PASS" if r.passed else "FAIL"
        print(f"  [{mark}] {r.name.ljust(width)}  {r.detail}")
    failed = sum(not r.passed for r in results)
    print(f"{len(results) - failed}/{len(results)} checks passed")


def cmd_check(args: argparse.Namespace, transport: httpx.BaseTransport | None = None) -> int:
    cfg = _load_config(args.config)
    with httpx.Client(base_url=args.api, timeout=30, transport=transport) as client:
        summary = _get_summary(client, args.run_id)
        baseline = _get_summary(client, args.baseline) if args.baseline else None
    results = evaluate(summary, cfg, baseline)
    if not results:
        raise CliError("config defines no checks")
    if args.json:
        print(json.dumps({"run_id": args.run_id, "results": [asdict(r) for r in results]}))
    else:
        _print_table(summary["run"], results)
    return EXIT_OK if all(r.passed for r in results) else EXIT_FAILED


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="openfleet", description=__doc__.split("\n")[0])
    p.add_argument(
        "--api",
        default=os.environ.get("OPENFLEET_API", "http://localhost:8000"),
        help="API base URL (env OPENFLEET_API)",
    )
    sub = p.add_subparsers(dest="command", required=True)
    c = sub.add_parser("check", help="assert regression thresholds on a run")
    c.add_argument("run_id")
    c.add_argument("--config", "-c", required=True, help="regression YAML")
    c.add_argument("--baseline", help="baseline run id to compare against")
    c.add_argument("--json", action="store_true", help="machine-readable output")
    c.set_defaults(func=cmd_check)
    keys.add_parser(sub, _default_database_url()).set_defaults(func=cmd_keys)
    return p


def _default_database_url() -> str:
    from app.core.config import Settings

    return Settings().database_url  # OPENFLEET_DATABASE_URL, else the local-dev default


def cmd_keys(args: argparse.Namespace, transport: httpx.BaseTransport | None = None) -> int:
    try:
        return args.keys_func(args)
    except keys.KeysError as exc:
        raise CliError(str(exc)) from exc


def main(argv: list[str] | None = None, transport: httpx.BaseTransport | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args, transport)
    except CliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
