"""`openfleet keys ...`: issue, list and revoke robot API keys (DECISIONS #021).

These talk to the database directly (OPENFLEET_DATABASE_URL), not to the API: whoever can
reach the database can already do anything, so no extra admin credential is needed. In
Docker Compose:  docker compose exec backend openfleet keys create amr-05
"""

import argparse
import asyncio
import json
import sys
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import RobotApiKey
from app.domain.errors import DomainError
from app.services.api_key_service import ApiKeyService


class KeysError(Exception):
    pass


def _run[T](database_url: str, fn: Callable[[ApiKeyService], Awaitable[T]]) -> T:
    async def go() -> T:
        engine = create_async_engine(database_url)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                return await fn(ApiKeyService(session))
        finally:
            await engine.dispose()

    try:
        return asyncio.run(go())
    except DomainError as exc:
        raise KeysError(exc.message) from exc
    except (OSError, SQLAlchemyError) as exc:
        raise KeysError(f"database error: {exc}") from exc


def _as_dict(row: RobotApiKey) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "robot_id": row.robot_id,
        "prefix": row.prefix,
        "label": row.label,
        "created_at": row.created_at.isoformat(),
        "revoked_at": row.revoked_at.isoformat() if row.revoked_at else None,
    }


def cmd_create(args: argparse.Namespace) -> int:
    issued = _run(args.database_url, lambda s: s.issue(args.robot_id, args.label))
    print(
        f"Issued key {issued.row.id} for robot {args.robot_id!r}. "
        "Store it now: it cannot be shown again.",
        file=sys.stderr,
    )
    print(issued.key)  # stdout carries only the key, so it can be captured by a script
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    rows = _run(args.database_url, lambda s: s.list(args.robot))
    if args.json:
        print(json.dumps([_as_dict(r) for r in rows]))
        return 0
    if not rows:
        print("no keys")
        return 0
    for r in rows:
        state = f"revoked {r.revoked_at:%Y-%m-%d %H:%M}" if r.revoked_at else "active"
        print(f"{r.id}  {r.robot_id:<20} {r.prefix}…  {state:<24} {r.label or ''}".rstrip())
    return 0


def cmd_revoke(args: argparse.Namespace) -> int:
    try:
        key_id = uuid.UUID(args.key_id)
    except ValueError as exc:
        raise KeysError(f"not a key id: {args.key_id!r}") from exc

    async def revoke(s: ApiKeyService) -> RobotApiKey:
        return await s.revoke(key_id)

    row = _run(args.database_url, revoke)
    print(f"revoked key {row.id} ({row.robot_id})")
    return 0


def add_parser(sub: Any, default_database_url: str) -> argparse.ArgumentParser:
    k = sub.add_parser("keys", help="manage robot API keys (direct database access)")
    k.add_argument(
        "--database-url",
        default=default_database_url,
        help="database URL (env OPENFLEET_DATABASE_URL)",
    )
    ksub = k.add_subparsers(dest="keys_command", required=True)

    c = ksub.add_parser("create", help="issue a new key for a robot and print it once")
    c.add_argument("robot_id")
    c.add_argument("--label", help="free text, e.g. where the key is deployed")
    c.set_defaults(keys_func=cmd_create)

    ls = ksub.add_parser("list", help="list keys (never shows the keys themselves)")
    ls.add_argument("--robot", help="only this robot's keys")
    ls.add_argument("--json", action="store_true", help="machine-readable output")
    ls.set_defaults(keys_func=cmd_list)

    r = ksub.add_parser("revoke", help="revoke a key by id (idempotent)")
    r.add_argument("key_id")
    r.set_defaults(keys_func=cmd_revoke)
    return k
