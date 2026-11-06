"""Robot API keys (DECISIONS #021).

Keys are 256-bit random tokens, so one SHA-256 is enough to store them: there is nothing to
brute-force, and unlike a password hash it can be looked up by an index on every request.
"""

import hashlib
import logging
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import RobotApiKey
from app.domain.errors import ConflictError, InvalidRequestError, NotFoundError
from app.repositories.api_key_repository import ApiKeyRepository
from app.schemas.robot import ROBOT_ID_PATTERN

log = logging.getLogger(__name__)

KEY_PREFIX = "ofk_"
MIN_KEY_LENGTH = 16  # applies to seeded keys; issued keys are much longer
DISPLAY_PREFIX_LENGTH = 12
_ROBOT_ID_RE = re.compile(ROBOT_ID_PATTERN)


def generate_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


@dataclass(frozen=True)
class IssuedKey:
    row: RobotApiKey
    key: str  # the only time the plaintext exists server-side


class ApiKeyService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.keys = ApiKeyRepository(session)

    async def authenticate(self, key: str) -> str | None:
        """The robot id a key belongs to, or None if it is unknown or revoked.
        Read-only: it never commits, so it can share the request's transaction."""
        row = await self.keys.active_by_hash(hash_key(key))
        return row.robot_id if row is not None else None

    async def issue(self, robot_id: str, label: str | None = None) -> IssuedKey:
        _check_robot_id(robot_id)
        key = generate_key()
        row = await self.keys.insert_if_absent(
            robot_id, hash_key(key), key[:DISPLAY_PREFIX_LENGTH], label
        )
        assert row is not None  # a fresh 256-bit token never collides
        await self.session.commit()
        return IssuedKey(row=row, key=key)

    async def list(self, robot_id: str | None = None) -> list[RobotApiKey]:
        return await self.keys.list(robot_id)

    async def revoke(self, key_id: uuid.UUID, now: datetime | None = None) -> RobotApiKey:
        """Idempotent: revoking a revoked key returns it unchanged."""
        row = await self.keys.revoke(key_id, now or datetime.now(UTC))
        if row is None:
            row = await self.keys.get(key_id)
            if row is None:
                raise NotFoundError(f"API key {key_id} not found")
        await self.session.commit()
        return row

    async def seed(self, keys: dict[str, str]) -> int:
        """Ensure each (robot_id -> key) pair exists; for demo/dev setups with known keys.
        Idempotent, and never un-revokes: a seeded key that was revoked stays revoked.
        Returns how many keys were inserted."""
        inserted = 0
        for robot_id, key in keys.items():
            _check_robot_id(robot_id)
            if len(key) < MIN_KEY_LENGTH:
                raise InvalidRequestError(
                    f"seed key for {robot_id!r} is shorter than {MIN_KEY_LENGTH} characters"
                )
            digest = hash_key(key)
            row = await self.keys.insert_if_absent(
                robot_id, digest, key[:DISPLAY_PREFIX_LENGTH], "seeded"
            )
            if row is not None:
                inserted += 1
                continue
            existing = await self.keys.by_hash(digest)
            if existing is not None and existing.robot_id != robot_id:
                raise ConflictError(
                    f"seed key for {robot_id!r} is already issued to {existing.robot_id!r}"
                )
        await self.session.commit()
        return inserted


def _check_robot_id(robot_id: str) -> None:
    if not _ROBOT_ID_RE.fullmatch(robot_id):
        raise InvalidRequestError(f"invalid robot id {robot_id!r}")
