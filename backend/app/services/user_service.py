"""Users, roles, sessions and API tokens (DECISIONS #022).

Tokens are 256-bit random values stored as SHA-256 (like robot keys, #021). Passwords use
scrypt, run in a worker thread so a login never stalls the event loop.
"""

import asyncio
import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import passwords
from app.db.models import User, UserToken
from app.domain.enums import TokenKind, UserRole
from app.domain.errors import ConflictError, InvalidRequestError, NotFoundError
from app.repositories.user_repository import UserRepository

USERNAME_PATTERN = r"^[a-z0-9][a-z0-9_.@-]{0,63}$"
_USERNAME_RE = re.compile(USERNAME_PATTERN)
SESSION_PREFIX = "ofs_"
API_TOKEN_PREFIX = "ofu_"
MIN_SEED_TOKEN_LENGTH = 16
DISPLAY_PREFIX_LENGTH = 12
_ROLE_RANK = {UserRole.VIEWER: 0, UserRole.OPERATOR: 1}


def has_role(user: User, role: UserRole) -> bool:
    return _ROLE_RANK[UserRole(user.role)] >= _ROLE_RANK[role]


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def normalize_username(username: str) -> str:
    name = username.strip().lower()
    if not _USERNAME_RE.fullmatch(name):
        raise InvalidRequestError(f"invalid username {username!r}")
    return name


def _check_password(password: str) -> None:
    if len(password) < passwords.MIN_PASSWORD_LENGTH:
        raise InvalidRequestError(
            f"password must be at least {passwords.MIN_PASSWORD_LENGTH} characters"
        )


@dataclass(frozen=True)
class IssuedToken:
    row: UserToken
    token: str  # the only time the plaintext exists server-side


class UserService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.users = UserRepository(session)

    # --- authentication ----------------------------------------------------------------

    async def authenticate(self, token: str, now: datetime) -> User | None:
        """The user a session/API token belongs to, if the token is live. Read-only."""
        found = await self.users.active_token(hash_token(token), now)
        return found[1] if found is not None else None

    async def login(
        self, username: str, password: str, now: datetime, ttl: timedelta
    ) -> IssuedToken | None:
        """A new session for valid credentials, else None. Unknown users, wrong passwords
        and disabled users are indistinguishable, in content and in timing."""
        try:
            name = normalize_username(username)
        except InvalidRequestError:
            name = None
        user = await self.users.by_username(name) if name else None
        stored = user.password_hash if user is not None else passwords.DUMMY_HASH
        ok = await asyncio.to_thread(passwords.verify_password, password, stored)
        if user is None or not ok or user.disabled_at is not None:
            return None
        issued = await self._issue(user, TokenKind.SESSION, SESSION_PREFIX, None, now + ttl)
        await self.session.commit()
        return issued

    async def logout(self, token: str, now: datetime) -> None:
        """Revoke a session token. API tokens are revoked explicitly, never by a logout."""
        row = await self.users.token_by_hash(hash_token(token))
        if row is not None and row.kind == TokenKind.SESSION:
            await self.users.revoke_token_by_hash(row.token_hash, now)
        await self.session.commit()

    # --- management (openfleet users ...) ------------------------------------------------

    async def create(self, username: str, password: str, role: UserRole) -> User:
        name = normalize_username(username)
        _check_password(password)
        digest = await asyncio.to_thread(passwords.hash_password, password)
        user = await self.users.insert_if_absent(name, digest, role)
        if user is None:
            raise ConflictError(f"user {name!r} already exists")
        await self.session.commit()
        return user

    async def get(self, username: str) -> User:
        user = await self.users.by_username(normalize_username(username))
        if user is None:
            raise NotFoundError(f"user {username!r} not found")
        return user

    async def list_users(self) -> list[User]:
        return await self.users.list_users()

    async def set_role(self, username: str, role: UserRole) -> User:
        user = await self.get(username)
        user.role = role
        await self.session.commit()
        return user

    async def set_password(self, username: str, password: str, now: datetime) -> User:
        """Also signs the user out everywhere (sessions only; API tokens are kept)."""
        user = await self.get(username)
        _check_password(password)
        user.password_hash = await asyncio.to_thread(passwords.hash_password, password)
        for token, _ in await self.users.tokens(user.id):
            if token.kind == TokenKind.SESSION and token.revoked_at is None:
                await self.users.revoke_token(token.id, now)
        await self.session.commit()
        return user

    async def set_disabled(self, username: str, disabled: bool, now: datetime) -> User:
        """Disabling revokes every token, so re-enabling doesn't revive old sessions."""
        user = await self.get(username)
        if disabled and user.disabled_at is None:
            user.disabled_at = now
            await self.users.revoke_all_tokens(user.id, now)
        elif not disabled:
            user.disabled_at = None
        await self.session.commit()
        return user

    async def issue_api_token(
        self, username: str, label: str | None, expires_at: datetime | None
    ) -> IssuedToken:
        user = await self.get(username)
        issued = await self._issue(user, TokenKind.API, API_TOKEN_PREFIX, label, expires_at)
        await self.session.commit()
        return issued

    async def tokens(self, username: str | None = None) -> list[tuple[UserToken, str]]:
        user_id = (await self.get(username)).id if username else None
        return await self.users.tokens(user_id)

    async def revoke_token(self, token_id: Any, now: datetime | None = None) -> UserToken:
        row = await self.users.revoke_token(token_id, now or datetime.now(UTC))
        if row is None:
            row = await self.users.get_token(token_id)
            if row is None:
                raise NotFoundError(f"token {token_id} not found")
        await self.session.commit()
        return row

    async def seed(self, spec: dict[str, Any]) -> int:
        """Ensure demo/dev users exist: {username: {"password", "role", "token"?}}.
        Never changes an existing user's password or role, and never un-revokes a token.
        Returns how many users were created."""
        created = 0
        for username, entry in spec.items():
            if not isinstance(entry, dict) or not isinstance(entry.get("password"), str):
                raise InvalidRequestError(f"seed user {username!r}: expected {{password, role}}")
            try:
                role = UserRole(str(entry.get("role", "")).upper())
            except ValueError as exc:
                raise InvalidRequestError(f"seed user {username!r}: invalid role") from exc
            name = normalize_username(username)
            _check_password(entry["password"])
            user = await self.users.by_username(name)
            if user is None:
                digest = await asyncio.to_thread(passwords.hash_password, entry["password"])
                user = await self.users.insert_if_absent(name, digest, role)
                if user is None:  # lost a race with another seeder
                    user = await self.users.by_username(name)
                    assert user is not None
                else:
                    created += 1
            token = entry.get("token")
            if token is not None:
                await self._seed_token(user, token)
        await self.session.commit()
        return created

    # --- helpers -------------------------------------------------------------------------

    async def _issue(
        self,
        user: User,
        kind: TokenKind,
        prefix: str,
        label: str | None,
        expires_at: datetime | None,
    ) -> IssuedToken:
        token = prefix + secrets.token_urlsafe(32)
        row = await self.users.insert_token(
            user.id, kind, hash_token(token), token[:DISPLAY_PREFIX_LENGTH], label, expires_at
        )
        assert row is not None  # a fresh 256-bit token never collides
        return IssuedToken(row=row, token=token)

    async def _seed_token(self, user: User, token: Any) -> None:
        if not isinstance(token, str) or len(token) < MIN_SEED_TOKEN_LENGTH:
            raise InvalidRequestError(
                f"seed token for {user.username!r} must be a string of at least "
                f"{MIN_SEED_TOKEN_LENGTH} characters"
            )
        digest = hash_token(token)
        row = await self.users.insert_token(
            user.id, TokenKind.API, digest, token[:DISPLAY_PREFIX_LENGTH], "seeded", None
        )
        if row is None:
            existing = await self.users.token_by_hash(digest)
            if existing is not None and existing.user_id != user.id:
                raise ConflictError(f"seed token for {user.username!r} belongs to another user")
