"""Sites: the tenants (DECISIONS #039). Managed with `openfleet sites` / `openfleet users`."""

import re
from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Robot, Site, User
from app.domain.errors import ConflictError, InvalidRequestError, NotFoundError
from app.services.user_service import normalize_username

SITE_ID_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,63}$"
_SITE_ID_RE = re.compile(SITE_ID_PATTERN)


@dataclass(frozen=True)
class SiteInfo:
    id: str
    name: str
    robots: int
    users: int  # users scoped to this site (unscoped users see every site)


class SiteService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, site_id: str, name: str | None = None) -> Site:
        if not _SITE_ID_RE.fullmatch(site_id):
            raise InvalidRequestError(f"invalid site id {site_id!r} (lowercase, digits, - and _)")
        stmt = (
            insert(Site)
            .values(id=site_id, name=name or site_id)
            .on_conflict_do_nothing(index_elements=["id"])
            .returning(Site)
        )
        site = (await self.session.scalars(stmt)).one_or_none()
        if site is None:
            raise ConflictError(f"site {site_id!r} already exists")
        await self.session.commit()
        return site

    async def get(self, site_id: str) -> Site:
        site = await self.session.get(Site, site_id)
        if site is None:
            raise NotFoundError(f"site {site_id!r} not found")
        return site

    async def list_sites(self) -> list[SiteInfo]:
        robots = select(Robot.site_id, func.count().label("n")).group_by(Robot.site_id).subquery()
        users = (
            select(User.site_id, func.count().label("n"))
            .where(User.site_id.is_not(None))
            .group_by(User.site_id)
            .subquery()
        )
        stmt = (
            select(Site, func.coalesce(robots.c.n, 0), func.coalesce(users.c.n, 0))
            .outerjoin(robots, robots.c.site_id == Site.id)
            .outerjoin(users, users.c.site_id == Site.id)
            .order_by(Site.id)
        )
        return [
            SiteInfo(s.id, s.name, int(r), int(u)) for s, r, u in (await self.session.execute(stmt))
        ]

    async def assign(self, site_id: str, robot_ids: list[str]) -> int:
        """Move robots (registered or not yet seen: unknown ids are an error) to a site."""
        await self.get(site_id)
        known = set(await self.session.scalars(select(Robot.id).where(Robot.id.in_(robot_ids))))
        missing = sorted(set(robot_ids) - known)
        if missing:
            raise NotFoundError(f"robots not found: {', '.join(missing)}")
        await self.session.execute(
            update(Robot).where(Robot.id.in_(robot_ids)).values(site_id=site_id)
        )
        await self.session.commit()
        return len(known)

    async def set_user_site(self, username: str, site_id: str | None) -> User:
        """`site_id=None`: the user sees every site. Open WebSockets notice at their next
        re-check and close, so the client reconnects with the new scope."""
        if site_id is not None:
            await self.get(site_id)
        name = normalize_username(username)
        user = await self.session.scalar(select(User).where(User.username == name))
        if user is None:
            raise NotFoundError(f"user {username!r} not found")
        user.site_id = site_id
        await self.session.commit()
        return user
