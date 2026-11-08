"""`openfleet users ...`: dashboard/read-API users, roles and API tokens (DECISIONS #022).

Direct database access, like `openfleet keys`:
    docker compose exec backend openfleet users create alice --role operator
    docker compose exec backend openfleet users token create ci-bot --label github-actions

Passwords are read from a prompt, or from stdin with --password-stdin (never from argv,
which other local users can see in the process list).
"""

import argparse
import getpass
import json
import sys
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from app.domain.enums import UserRole
from app.services.user_service import UserService
from openfleet_cli.keys import KeysError, run_db

ROLES = [r.value.lower() for r in UserRole]


def _role(value: str) -> UserRole:
    return UserRole(value.upper())


def _read_password(args: argparse.Namespace) -> str:
    if args.password_stdin:
        return sys.stdin.readline().rstrip("\n")
    first = getpass.getpass("Password: ")
    if getpass.getpass("Repeat password: ") != first:
        raise KeysError("passwords do not match")
    return first


def cmd_create(args: argparse.Namespace) -> int:
    password = None if args.sso else _read_password(args)
    user = run_db(
        args.database_url,
        lambda s: UserService(s).create(args.username, password, _role(args.role)),
    )
    how = ", signs in with SSO only" if args.sso else ""
    print(f"created user {user.username!r} ({user.role.lower()}{how})")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    users = run_db(args.database_url, lambda s: UserService(s).list_users())
    rows = [
        {
            "username": u.username,
            "role": u.role,
            "created_at": u.created_at.isoformat(),
            "disabled_at": u.disabled_at.isoformat() if u.disabled_at else None,
        }
        for u in users
    ]
    if args.json:
        print(json.dumps(rows))
    elif not rows:
        print("no users")
    for r in rows if not args.json else []:
        state = "disabled" if r["disabled_at"] else "active"
        print(f"{r['username']:<24} {r['role'].lower():<9} {state}")
    return 0


def cmd_set_role(args: argparse.Namespace) -> int:
    user = run_db(
        args.database_url, lambda s: UserService(s).set_role(args.username, _role(args.role))
    )
    print(f"{user.username!r} is now {user.role.lower()}")
    return 0


def cmd_passwd(args: argparse.Namespace) -> int:
    password = _read_password(args)
    now = datetime.now(UTC)
    run_db(args.database_url, lambda s: UserService(s).set_password(args.username, password, now))
    print(f"password changed for {args.username!r}; their sessions were signed out")
    return 0


def cmd_disable(args: argparse.Namespace) -> int:
    disable = args.users_command == "disable"
    now = datetime.now(UTC)
    run_db(args.database_url, lambda s: UserService(s).set_disabled(args.username, disable, now))
    if disable:
        print(f"disabled {args.username!r} and revoked all their sessions and tokens")
    else:
        print(f"enabled {args.username!r} (old tokens stay revoked)")
    return 0


def cmd_token_create(args: argparse.Namespace) -> int:
    expires = datetime.now(UTC) + timedelta(days=args.expires_days) if args.expires_days else None
    issued = run_db(
        args.database_url,
        lambda s: UserService(s).issue_api_token(args.username, args.label, expires),
    )
    when = f"expires {expires:%Y-%m-%d}" if expires else "no expiry"
    print(
        f"Issued API token {issued.row.id} for {args.username!r} ({when}). "
        "Store it now: it cannot be shown again. Use it as OPENFLEET_TOKEN.",
        file=sys.stderr,
    )
    print(issued.token)
    return 0


def _token_dict(token: Any, username: str) -> dict[str, Any]:
    return {
        "id": str(token.id),
        "username": username,
        "kind": token.kind,
        "prefix": token.prefix,
        "label": token.label,
        "created_at": token.created_at.isoformat(),
        "expires_at": token.expires_at.isoformat() if token.expires_at else None,
        "revoked_at": token.revoked_at.isoformat() if token.revoked_at else None,
    }


def cmd_token_list(args: argparse.Namespace) -> int:
    rows = run_db(args.database_url, lambda s: UserService(s).tokens(args.user))
    out = [_token_dict(t, u) for t, u in rows]
    if args.json:
        print(json.dumps(out))
        return 0
    if not out:
        print("no tokens")
    now = datetime.now(UTC).isoformat()
    for r in out:
        if r["revoked_at"]:
            state = "revoked"
        elif r["expires_at"] and r["expires_at"] <= now:
            state = "expired"
        else:
            state = "active"
        line = f"{r['id']}  {r['username']:<20} {r['kind'].lower():<8} {r['prefix']}…  {state}"
        print(f"{line}  {r['label'] or ''}".rstrip())
    return 0


def cmd_token_revoke(args: argparse.Namespace) -> int:
    try:
        token_id = uuid.UUID(args.token_id)
    except ValueError as exc:
        raise KeysError(f"not a token id: {args.token_id!r}") from exc
    row = run_db(args.database_url, lambda s: UserService(s).revoke_token(token_id))
    print(f"revoked token {row.id}")
    return 0


def add_parser(sub: Any, default_database_url: str) -> argparse.ArgumentParser:
    u = sub.add_parser("users", help="manage dashboard users and API tokens (direct database)")
    u.add_argument(
        "--database-url",
        default=default_database_url,
        help="database URL (env OPENFLEET_DATABASE_URL)",
    )
    usub = u.add_subparsers(dest="users_command", required=True)

    def with_password(p: argparse.ArgumentParser) -> None:
        p.add_argument("--password-stdin", action="store_true", help="read the password from stdin")

    c = usub.add_parser("create", help="create a user")
    c.add_argument("username")
    c.add_argument("--role", choices=ROLES, default="viewer")
    c.add_argument(
        "--sso",
        action="store_true",
        help="no password: the user signs in with Google/OIDC (username = their email)",
    )
    with_password(c)
    c.set_defaults(users_func=cmd_create)

    ls = usub.add_parser("list", help="list users")
    ls.add_argument("--json", action="store_true")
    ls.set_defaults(users_func=cmd_list)

    r = usub.add_parser("set-role", help="change a user's role (applies immediately)")
    r.add_argument("username")
    r.add_argument("role", choices=ROLES)
    r.set_defaults(users_func=cmd_set_role)

    pw = usub.add_parser("passwd", help="set a user's password (signs out their sessions)")
    pw.add_argument("username")
    with_password(pw)
    pw.set_defaults(users_func=cmd_passwd)

    for name, help_text in (
        ("disable", "disable a user and revoke all their sessions and tokens"),
        ("enable", "re-enable a disabled user"),
    ):
        d = usub.add_parser(name, help=help_text)
        d.add_argument("username")
        d.set_defaults(users_func=cmd_disable)

    t = usub.add_parser("token", help="API tokens for CLI/CI (OPENFLEET_TOKEN)")
    tsub = t.add_subparsers(dest="token_command", required=True)
    tc = tsub.add_parser("create", help="issue an API token for a user and print it once")
    tc.add_argument("username")
    tc.add_argument("--label")
    tc.add_argument("--expires-days", type=int, help="default: no expiry")
    tc.set_defaults(users_func=cmd_token_create)
    tl = tsub.add_parser("list", help="list sessions and API tokens (never the tokens)")
    tl.add_argument("--user")
    tl.add_argument("--json", action="store_true")
    tl.set_defaults(users_func=cmd_token_list)
    tr = tsub.add_parser("revoke", help="revoke a token or session by id (idempotent)")
    tr.add_argument("token_id")
    tr.set_defaults(users_func=cmd_token_revoke)
    return u
