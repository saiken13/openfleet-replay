import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.auth import VIEWER, Viewer
from app.api.deps import SessionDep
from app.domain.errors import InvalidRequestError
from app.repositories.triage_repository import TriageRepository
from app.schemas.robot import ROBOT_ID_PATTERN
from app.schemas.triage import TriageActionRead, TriagePage

router = APIRouter(prefix="/triage", tags=["incidents"])


@router.get(
    "",
    dependencies=[VIEWER],
    response_model=TriagePage,
    summary="Triage audit log: who changed which incident's status, and when. Newest first.",
)
async def list_triage_actions(
    session: SessionDep,
    user: Viewer,
    actor: Annotated[str | None, Query(max_length=200)] = None,
    robot_id: Annotated[str | None, Query(pattern=ROBOT_ID_PATTERN)] = None,
    incident_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: Annotated[str | None, Query(max_length=40)] = None,
) -> TriagePage:
    before_id = None
    if cursor is not None:
        if not cursor.isdigit():
            raise InvalidRequestError("invalid cursor")
        before_id = int(cursor)
    rows = await TriageRepository(session).search(
        actor=actor.lower() if actor else None,  # usernames are stored lowercase
        robot_id=robot_id,
        incident_id=incident_id,
        before_id=before_id,
        limit=limit + 1,  # one extra row tells whether another page exists
        site_id=user.site_id,  # DECISIONS #039
    )
    page, more = rows[:limit], len(rows) > limit
    return TriagePage(
        items=[
            TriageActionRead(
                id=a.id,
                incident_id=a.incident_id,
                incident_title=title,
                robot_id=a.robot_id,
                actor=a.actor,
                from_status=a.from_status,
                to_status=a.to_status,
                at=a.at,
            )
            for a, title in page
        ],
        next_cursor=str(page[-1][0].id) if more and page else None,
    )
