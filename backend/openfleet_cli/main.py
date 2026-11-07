"""openfleet: command-line client for OpenFleet Replay.

    openfleet check RUN_ID --config regression.yaml [--baseline RUN_ID] [--api URL] [--json]
    openfleet redetect RUN_ID [--set NAME=VALUE ...] [--fail-on-missed] [--json]
    openfleet keys create ROBOT_ID [--label TEXT] | keys list [--robot ID] | keys revoke KEY_ID
    openfleet users create|list|set-role|passwd|disable|enable ... | users token create|list|revoke

`check` and `redetect` read the API with a user API token: --token or OPENFLEET_TOKEN.

Exit codes: 0 all checks passed (or a keys command succeeded), 1 a check failed (or, with
`redetect --fail-on-missed`, a recorded incident is no longer detected),
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

from openfleet_cli import keys, retention, users
from openfleet_cli.checks import CheckConfig, CheckResult, evaluate

EXIT_OK, EXIT_FAILED, EXIT_ERROR = 0, 1, 2


class CliError(Exception):
    pass


def _get_summary(client: httpx.Client, run_id: str) -> dict[str, Any]:
    return _call(client, "GET", f"/api/v1/runs/{run_id}/summary", run_id)


def _call(
    client: httpx.Client, method: str, path: str, run_id: str, body: Any = None
) -> dict[str, Any]:
    try:
        resp = client.request(method, path, json=body)
    except httpx.HTTPError as exc:
        raise CliError(f"cannot reach API: {exc}") from exc
    if resp.status_code == 404:
        raise CliError(f"run {run_id} not found")
    if resp.status_code == 401:
        raise CliError(
            "not authorized: set OPENFLEET_TOKEN (or --token) to a user API token "
            "from `openfleet users token create USER`"
        )
    if resp.status_code == 422:
        raise CliError(f"rejected by the API: {resp.text[:500]}")
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
    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}
    with httpx.Client(
        base_url=args.api, timeout=30, transport=transport, headers=headers
    ) as client:
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


def _parse_overrides(pairs: list[str]) -> dict[str, float]:
    out = {}
    for pair in pairs:
        name, sep, value = pair.partition("=")
        try:
            if not sep or not name:
                raise ValueError
            out[name.strip()] = float(value)
        except ValueError as exc:
            raise CliError(f"--set expects NAME=NUMBER, got {pair!r}") from exc
    return out


def _print_redetect(r: dict[str, Any]) -> None:
    run = r["run"]
    print(f"Run {run['id']}  robot={run['robot_id']}  version={run['software_version']}")
    rules = "overridden thresholds" if r["overrides"] else "the current rules"
    print(f"Replayed {r['batches']} batches with {rules}")
    for note in r["notes"]:
        print(f"  note: {note}")
    labels = {"both": "CAUGHT", "recorded_only": "MISSED", "replay_only": "NEW"}
    width = max((len(k["dedup_key"]) for k in r["keys"]), default=10)
    for k in r["keys"]:
        rec = f"{k['recorded_count']}x {k['recorded_severity']}" if k["recorded_count"] else "-"
        rep = f"{k['replayed_count']}x {k['replayed_severity']}" if k["replayed_count"] else "-"
        at = k["first_replayed_offset_s"]
        when = f"  first at +{at:.0f}s" if at is not None else ""
        print(
            f"  [{labels[k['verdict']]:>6}] {k['dedup_key'].ljust(width)}  "
            f"recorded {rec:<12} replay {rep:<12}{when}"
        )
    print(f"{r['caught']} caught, {r['missed']} missed, {r['new']} new")


def cmd_redetect(args: argparse.Namespace, transport: httpx.BaseTransport | None = None) -> int:
    overrides = _parse_overrides(args.set or [])
    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}
    with httpx.Client(
        base_url=args.api, timeout=120, transport=transport, headers=headers
    ) as client:
        r = _call(client, "POST", f"/api/v1/runs/{args.run_id}/redetect", args.run_id, overrides)
    if args.json:
        print(json.dumps(r))
    else:
        _print_redetect(r)
    return EXIT_FAILED if args.fail_on_missed and r["missed"] else EXIT_OK


def _add_token_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--token",
        default=os.environ.get("OPENFLEET_TOKEN"),
        help="user API token (env OPENFLEET_TOKEN); `openfleet users token create USER`",
    )


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
    _add_token_arg(c)
    c.set_defaults(func=cmd_check)
    r = sub.add_parser("redetect", help="run the current detection rules over a recorded run")
    r.add_argument("run_id")
    r.add_argument(
        "--set",
        action="append",
        metavar="NAME=VALUE",
        help="override a threshold, e.g. --set cpu_high_pct=80 (repeatable)",
    )
    r.add_argument(
        "--fail-on-missed",
        action="store_true",
        help="exit 1 if a recorded incident is no longer detected",
    )
    r.add_argument("--json", action="store_true", help="machine-readable output")
    _add_token_arg(r)
    r.set_defaults(func=cmd_redetect)
    keys.add_parser(sub, _default_database_url()).set_defaults(func=cmd_keys)
    users.add_parser(sub, _default_database_url()).set_defaults(func=cmd_users)
    retention.add_parser(sub, _default_database_url(), _default_retention_days()).set_defaults(
        func=cmd_retention
    )
    return p


def _default_database_url() -> str:
    from app.core.config import Settings

    return Settings().database_url  # OPENFLEET_DATABASE_URL, else the local-dev default


def _default_retention_days() -> float | None:
    from app.core.config import Settings

    return Settings().retention_days


def cmd_retention(args: argparse.Namespace, transport: httpx.BaseTransport | None = None) -> int:
    try:
        return retention.cmd_retention(args)
    except keys.KeysError as exc:
        raise CliError(str(exc)) from exc


def cmd_users(args: argparse.Namespace, transport: httpx.BaseTransport | None = None) -> int:
    try:
        return args.users_func(args)
    except keys.KeysError as exc:
        raise CliError(str(exc)) from exc


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
