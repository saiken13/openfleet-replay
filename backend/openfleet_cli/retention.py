"""`openfleet retention`: compact finished runs older than N days (DECISIONS #030).

Direct database access, like `openfleet keys`. The sweeper does the same automatically when
OPENFLEET_RETENTION_DAYS is set; this is for one-off cleanups and for previewing them:

    docker compose exec backend openfleet retention --days 30 --dry-run
"""

import argparse
import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any

from app.services.retention_service import Compacted, RetentionService
from openfleet_cli.keys import KeysError, run_db


def cmd_retention(args: argparse.Namespace) -> int:
    if args.days is None:
        raise KeysError("give --days (or set OPENFLEET_RETENTION_DAYS)")
    now = datetime.now(UTC)
    older_than = timedelta(days=args.days)

    async def go(session: Any) -> list[Compacted]:
        svc = RetentionService(session)
        if args.dry_run:
            return [await svc.counts(r) for r in await svc.due(now, older_than, args.limit)]
        return await svc.run(now, older_than, args.limit)

    done = run_db(args.database_url, go)
    if args.json:
        rows = [{**asdict(c), "run_id": str(c.run_id)} for c in done]
        print(json.dumps({"dry_run": args.dry_run, "runs": rows}, indent=2))
        return 0
    verb = "would compact" if args.dry_run else "compacted"
    for c in done:
        print(
            f"{verb} {c.run_id}: {c.samples} samples, {c.topic_samples} topic samples, "
            f"{c.heartbeat_spans} heartbeat spans, {c.log_events} log events"
        )
    print(f"{verb} {len(done)} run(s) that ended more than {args.days:g} days ago")
    return 0


def add_parser(sub: Any, default_database_url: str, default_days: float | None) -> Any:
    r = sub.add_parser(
        "retention", help="compact finished runs older than N days (direct database access)"
    )
    r.add_argument("--database-url", default=default_database_url)
    r.add_argument(
        "--days", type=float, default=default_days, help="age (env OPENFLEET_RETENTION_DAYS)"
    )
    r.add_argument("--limit", type=int, default=1000, help="at most this many runs per call")
    r.add_argument("--dry-run", action="store_true", help="show what would be removed")
    r.add_argument("--json", action="store_true", help="machine-readable output")
    return r
