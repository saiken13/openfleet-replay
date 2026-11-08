import uuid
from datetime import datetime

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import User, UserToken


class UserRepository:
    """All SQL for users and their tokens. No business decisions."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def by_username(self, username: str) -> User | None:
        stmt = (
            select(User).where(User.username == username).execution_options(populate_existing=True)
        )
        return (await self.session.scalars(stmt)).one_or_none()

    async def insert_if_absent(self, username: str, password_hash: str, role: str) -> User | None:
        stmt = (
            insert(User)
            .values(id=uuid.uuid4(), username=username, password_hash=password_hash, role=role)
            .on_conflict_do_nothing(index_elements=["username"])
            .returning(User)
        )
        return (await self.session.scalars(stmt)).one_or_none()

    async def by_oidc_subject(self, subject: str) -> User | None:
        return await self.session.scalar(select(User).where(User.oidc_subject == subject))

    async def list_users(self) -> list[User]:
        return list(await self.session.scalars(select(User).order_by(User.username)))

    async def active_token(self, token_hash: str, now: datetime) -> tuple[UserToken, User] | None:
        """A token that is not revoked or expired, of a user who is not disabled."""
        stmt = (
            select(UserToken, User)
            .join(User, User.id == UserToken.user_id)
            .where(
                UserToken.token_hash == token_hash,
                UserToken.revoked_at.is_(None),
                or_(UserToken.expires_at.is_(None), UserToken.expires_at > now),
                User.disabled_at.is_(None),
            )
        )
        row = (await self.session.execute(stmt)).one_or_none()
        return (row[0], row[1]) if row is not None else None

    async def extend_token(self, token_id: uuid.UUID, expires_at: datetime) -> None:
        """Move a token's expiry forward (never back); concurrent extensions just race to the
        same kind of value, and the later one wins."""
        await self.session.execute(
            update(UserToken)
            .where(UserToken.id == token_id, UserToken.expires_at < expires_at)
            .values(expires_at=expires_at)
        )

    async def token_by_hash(self, token_hash: str) -> UserToken | None:
        stmt = select(UserToken).where(UserToken.token_hash == token_hash)
        return (await self.session.scalars(stmt)).one_or_none()

    async def insert_token(
        self,
        user_id: uuid.UUID,
        kind: str,
        token_hash: str,
        prefix: str,
        label: str | None,
        expires_at: datetime | None,
        absolute_expires_at: datetime | None = None,
    ) -> UserToken | None:
        """Returns None if the hash already exists (race-safe)."""
        stmt = (
            insert(UserToken)
            .values(
                id=uuid.uuid4(),
                user_id=user_id,
                kind=kind,
                token_hash=token_hash,
                prefix=prefix,
                label=label,
                expires_at=expires_at,
                absolute_expires_at=absolute_expires_at,
            )
            .on_conflict_do_nothing(index_elements=["token_hash"])
            .returning(UserToken)
        )
        return (await self.session.scalars(stmt)).one_or_none()

    async def tokens(self, user_id: uuid.UUID | None) -> list[tuple[UserToken, str]]:
        stmt = (
            select(UserToken, User.username)
            .join(User, User.id == UserToken.user_id)
            .order_by(User.username, UserToken.created_at)
        )
        if user_id is not None:
            stmt = stmt.where(UserToken.user_id == user_id)
        return [(t, u) for t, u in (await self.session.execute(stmt)).all()]

    async def revoke_token(self, token_id: uuid.UUID, now: datetime) -> UserToken | None:
        stmt = (
            update(UserToken)
            .where(UserToken.id == token_id, UserToken.revoked_at.is_(None))
            .values(revoked_at=now)
            .returning(UserToken)
            .execution_options(populate_existing=True)
        )
        return (await self.session.scalars(stmt)).one_or_none()

    async def revoke_token_by_hash(self, token_hash: str, now: datetime) -> None:
        await self.session.execute(
            update(UserToken)
            .where(UserToken.token_hash == token_hash, UserToken.revoked_at.is_(None))
            .values(revoked_at=now)
        )

    async def revoke_all_tokens(self, user_id: uuid.UUID, now: datetime) -> None:
        await self.session.execute(
            update(UserToken)
            .where(UserToken.user_id == user_id, UserToken.revoked_at.is_(None))
            .values(revoked_at=now)
        )

    async def get_token(self, token_id: uuid.UUID) -> UserToken | None:
        return await self.session.get(UserToken, token_id, populate_existing=True)
