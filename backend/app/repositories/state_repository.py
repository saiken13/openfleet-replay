from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import NodeStateRow, TopicState


class StateRepository:
    """Current-state projections: topic_states and node_states ("now")."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def topics(self, robot_id: str) -> dict[str, TopicState]:
        rows = await self.session.scalars(
            select(TopicState).where(TopicState.robot_id == robot_id).order_by(TopicState.topic)
        )
        return {r.topic: r for r in rows}

    async def nodes(self, robot_id: str) -> dict[str, NodeStateRow]:
        rows = await self.session.scalars(
            select(NodeStateRow)
            .where(NodeStateRow.robot_id == robot_id)
            .order_by(NodeStateRow.node)
        )
        return {r.node: r for r in rows}

    def add(self, row: TopicState | NodeStateRow) -> None:
        self.session.add(row)

    async def flush(self) -> None:
        await self.session.flush()
