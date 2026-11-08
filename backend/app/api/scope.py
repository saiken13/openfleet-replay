"""Sites (DECISIONS #039): a user scoped to a site sees only that site's robots, and
everything hanging off them (runs, incidents, telemetry, triage, live updates).

Read routes take one of these instead of the plain VIEWER dependency. Each authenticates
like VIEWER, then answers 404 for anything outside the user's site, exactly as if it didn't
exist, so another site's robot ids don't leak. Unscoped users (site_id NULL) see everything.

`tests/test_sites.py::test_every_user_route_with_a_resource_id_checks_the_site` fails if a
new route with {robot_id}, {run_id} or {incident_id} forgets to use one of these.
"""

import uuid
from typing import Annotated

from fastapi import Depends, Path, Query
from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import Viewer
from app.api.deps import SessionDep
from app.db.models import Incident, Robot, Run, User
from app.domain.errors import NotFoundError
from app.schemas.robot import ROBOT_ID_PATTERN


def robots_in_site(site_id: str) -> Select[tuple[str]]:
    """For `.where(<robot id column>.in_(robots_in_site(site)))` in list queries."""
    return select(Robot.id).where(Robot.site_id == site_id)


async def robot_site(session: AsyncSession, robot_id: str) -> str | None:
    return await session.scalar(select(Robot.site_id).where(Robot.id == robot_id))


def can_see(user: User, site_id: str | None) -> bool:
    return user.site_id is None or site_id == user.site_id


async def require_robot_in_scope(
    user: Viewer,
    session: SessionDep,
    robot_id: Annotated[str, Path(pattern=ROBOT_ID_PATTERN)],
) -> User:
    if user.site_id is not None and await robot_site(session, robot_id) != user.site_id:
        raise NotFoundError(f"robot {robot_id!r} not found")
    return user


async def _check_run(user: User, session: AsyncSession, run_id: uuid.UUID) -> None:
    if user.site_id is None:
        return
    site = await session.scalar(
        select(Robot.site_id).join(Run, Run.robot_id == Robot.id).where(Run.id == run_id)
    )
    if site != user.site_id:
        raise NotFoundError(f"run {run_id} not found")


async def require_run_in_scope(user: Viewer, session: SessionDep, run_id: uuid.UUID) -> User:
    await _check_run(user, session, run_id)
    return user


async def require_runs_in_scope(
    user: Viewer, session: SessionDep, base: Annotated[uuid.UUID, Query()], candidate: uuid.UUID
) -> User:
    await _check_run(user, session, base)
    await _check_run(user, session, candidate)
    return user


async def require_incident_in_scope(
    user: Viewer, session: SessionDep, incident_id: uuid.UUID
) -> User:
    if user.site_id is not None:
        site = await session.scalar(
            select(Robot.site_id)
            .join(Incident, Incident.robot_id == Robot.id)
            .where(Incident.id == incident_id)
        )
        if site != user.site_id:
            raise NotFoundError(f"incident {incident_id} not found")
    return user


ROBOT_VIEWER = Depends(require_robot_in_scope)
RUN_VIEWER = Depends(require_run_in_scope)
COMPARE_VIEWER = Depends(require_runs_in_scope)
INCIDENT_VIEWER = Depends(require_incident_in_scope)
