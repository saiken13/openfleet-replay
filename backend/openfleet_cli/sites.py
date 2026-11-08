"""`openfleet sites`: tenants by site or team (DECISIONS #039). Direct database access, like
`openfleet keys` and `openfleet users`:

    docker compose exec backend openfleet sites create warehouse-a --name "Warehouse A"
    docker compose exec backend openfleet sites assign warehouse-a amr-01 amr-02
    docker compose exec backend openfleet users set-site alice warehouse-a   # or --all
    docker compose exec backend openfleet sites list
"""

import argparse
import json
from dataclasses import asdict

from app.services.site_service import SiteService
from openfleet_cli.keys import run_db


def cmd_create(args: argparse.Namespace) -> int:
    site = run_db(args.database_url, lambda s: SiteService(s).create(args.site_id, args.name))
    print(f"created site {site.id!r} ({site.name})")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    sites = run_db(args.database_url, lambda s: SiteService(s).list_sites())
    if args.json:
        print(json.dumps([asdict(x) for x in sites]))
        return 0
    for x in sites:
        print(f"{x.id:<20} {x.name:<28} {x.robots:>4} robots {x.users:>4} scoped users")
    return 0


def cmd_assign(args: argparse.Namespace) -> int:
    n = run_db(args.database_url, lambda s: SiteService(s).assign(args.site_id, args.robots))
    print(f"moved {n} robot(s) to {args.site_id!r}")
    return 0


def add_parser(
    sub: argparse._SubParsersAction, default_database_url: str
) -> argparse.ArgumentParser:
    p = sub.add_parser("sites", help="manage sites (tenants): create, list, assign robots")
    p.add_argument(
        "--database-url",
        default=default_database_url,
        help="database URL (env OPENFLEET_DATABASE_URL)",
    )
    ssub = p.add_subparsers(dest="sites_command", required=True)
    c = ssub.add_parser("create", help="create a site")
    c.add_argument("site_id")
    c.add_argument("--name", help="display name (default: the id)")
    c.set_defaults(sites_func=cmd_create)
    ls = ssub.add_parser("list", help="list sites with robot and user counts")
    ls.add_argument("--json", action="store_true")
    ls.set_defaults(sites_func=cmd_list)
    a = ssub.add_parser("assign", help="move robots to a site")
    a.add_argument("site_id")
    a.add_argument("robots", nargs="+", metavar="ROBOT_ID")
    a.set_defaults(sites_func=cmd_assign)
    return p
