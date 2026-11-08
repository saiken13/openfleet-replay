"""Sign in with Google / OIDC (DECISIONS #038), against a fake provider.

The fake serves discovery, JWKS and a token endpoint over httpx.MockTransport, signs real
RS256 ID tokens, and checks the PKCE verifier against the challenge it was given. Requests
pass `auth=None` so only the credentials under test are sent.
"""

import base64
import hashlib
import json
import time
import uuid
from collections.abc import AsyncIterator
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import CLIENT_HEADER, SESSION_COOKIE
from app.core.config import Settings
from app.core.oidc import OidcClient
from app.domain.enums import UserRole
from app.services.user_service import UserService
from tests.conftest import ClientFactory

ISSUER = "https://idp.test"
CLIENT_ID = "openfleet-test"
REDIRECT = "http://test/api/v1/auth/oidc/callback"


def _key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


class FakeProvider:
    def __init__(self) -> None:
        self.key = _key()
        self.kid = "k1"
        self.pending: dict[str, dict] = {}  # code -> {challenge, nonce, claims}
        self.claims: dict = {}  # what the next sign-in returns
        self.sign_with: rsa.RSAPrivateKey | None = None  # a different key: forged token
        self.override: dict = {}  # claims forced into the token (wrong aud, nonce...)

    def approve(self, authorize_url: str, **claims) -> str:
        """The user signs in on the provider's page: a code for that request."""
        q = {k: v[0] for k, v in parse_qs(urlparse(authorize_url).query).items()}
        assert q["code_challenge_method"] == "S256" and q["client_id"] == CLIENT_ID
        code = uuid.uuid4().hex
        self.pending[code] = {"challenge": q["code_challenge"], "nonce": q["nonce"], **claims}
        return code

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "authorization_endpoint": f"{ISSUER}/authorize",
                    "token_endpoint": f"{ISSUER}/token",
                    "jwks_uri": f"{ISSUER}/jwks",
                },
            )
        if path == "/jwks":
            jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
            return httpx.Response(200, json={"keys": [{**jwk, "kid": self.kid, "alg": "RS256"}]})
        if path == "/token":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            p = self.pending.pop(form.get("code", ""), None)
            if p is None:
                return httpx.Response(400, json={"error": "invalid_grant"})
            digest = hashlib.sha256(form["code_verifier"].encode()).digest()
            if base64.urlsafe_b64encode(digest).rstrip(b"=").decode() != p["challenge"]:
                return httpx.Response(400, json={"error": "invalid_grant", "why": "pkce"})
            now = int(time.time())
            claims = {
                "iss": ISSUER,
                "aud": CLIENT_ID,
                "iat": now,
                "exp": now + 300,
                "nonce": p["nonce"],
                **{k: v for k, v in p.items() if k not in ("challenge", "nonce")},
                **self.override,
            }
            token = jwt.encode(
                claims, self.sign_with or self.key, algorithm="RS256", headers={"kid": self.kid}
            )
            return httpx.Response(200, json={"id_token": token, "access_token": "x"})
        return httpx.Response(404)


@pytest.fixture
def provider() -> FakeProvider:
    return FakeProvider()


@pytest.fixture
def oidc_settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "oidc_issuer": ISSUER,
            "oidc_client_id": CLIENT_ID,
            "oidc_client_secret": "secret",
            "oidc_redirect_url": REDIRECT,
            "oidc_allowed_domains": ["robots.example"],
        }
    )


@pytest.fixture
async def sso(
    make_client: ClientFactory,
    oidc_settings: Settings,
    db_session: AsyncSession,
    provider: FakeProvider,
) -> AsyncIterator[httpx.AsyncClient]:
    async with make_client(oidc_settings, db_session) as c:
        await c.app.state.oidc.close()  # type: ignore[attr-defined]
        c.app.state.oidc = OidcClient(  # type: ignore[attr-defined]
            ISSUER,
            CLIENT_ID,
            "secret",
            REDIRECT,
            http=httpx.AsyncClient(transport=httpx.MockTransport(provider.handler)),
        )
        yield c


async def sign_in(c: httpx.AsyncClient, provider: FakeProvider, **claims) -> httpx.Response:
    """The browser's round trip: our login -> provider -> our callback."""
    start = await c.get("/api/v1/auth/oidc/login", auth=None)
    assert start.status_code == 302
    code = provider.approve(start.headers["location"], **claims)
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    return await c.get(
        "/api/v1/auth/oidc/callback", params={"code": code, "state": state}, auth=None
    )


def refused(r: httpx.Response, fragment: str) -> bool:
    loc = r.headers.get("location", "")
    return (
        r.status_code == 302
        and loc.startswith("/login?error=")
        and fragment in parse_qs(urlparse(loc).query)["error"][0]
        and SESSION_COOKIE not in r.cookies
    )


GOOD = {"sub": "g-123", "email": "ada@robots.example", "email_verified": True, "name": "Ada"}


async def test_config_advertises_the_provider_only_when_configured(
    make_client: ClientFactory, settings: Settings, oidc_settings: Settings
) -> None:
    async with make_client(oidc_settings) as c:
        cfg = (await c.get("/api/v1/auth/config")).json()
        assert cfg["oidc"] == {"name": "Google", "login_url": "/api/v1/auth/oidc/login"}
    async with make_client(settings) as c:
        assert (await c.get("/api/v1/auth/config")).json()["oidc"] is None
        assert (await c.get("/api/v1/auth/oidc/login")).status_code == 404


async def test_login_redirects_with_pkce_state_and_nonce(
    sso: httpx.AsyncClient,
) -> None:
    r = await sso.get("/api/v1/auth/oidc/login", auth=None)
    assert r.status_code == 302
    url = urlparse(r.headers["location"])
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert f"{url.scheme}://{url.netloc}{url.path}" == f"{ISSUER}/authorize"
    assert q["redirect_uri"] == REDIRECT and q["scope"] == "openid email profile"
    assert len(q["state"]) > 30 and len(q["nonce"]) > 30 and q["code_challenge_method"] == "S256"
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "samesite=lax" in cookie.lower()


async def test_allowed_domain_signs_in_and_creates_a_viewer(
    sso: httpx.AsyncClient, provider: FakeProvider, db_session: AsyncSession
) -> None:
    r = await sign_in(sso, provider, **GOOD)
    assert r.status_code == 302 and r.headers["location"] == "/"
    assert SESSION_COOKIE in r.cookies
    me = await sso.get("/api/v1/auth/me", auth=None)
    assert me.json() == {"username": "ada@robots.example", "role": "VIEWER"}
    user = await UserService(db_session).get("ada@robots.example")
    assert user.oidc_subject == f"{ISSUER}|g-123"
    # The same identity again: the same account, no duplicate.
    await sso.post("/api/v1/auth/logout", headers={CLIENT_HEADER: "t"}, auth=None)
    r = await sign_in(sso, provider, **GOOD)
    assert r.headers["location"] == "/"
    assert len([u for u in await UserService(db_session).list_users() if "ada@" in u.username]) == 1


async def test_an_sso_only_account_can_never_sign_in_with_a_password(
    sso: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await UserService(db_session).create("bob@elsewhere.example", None, UserRole.OPERATOR)
    for pw in ("!", "", "anything-at-all"):
        r = await sso.post(
            "/api/v1/auth/login",
            json={"username": "bob@elsewhere.example", "password": pw or "x"},
            headers={CLIENT_HEADER: "t"},
            auth=None,
        )
        assert r.status_code == 401


async def test_an_invited_user_is_linked_on_first_sign_in(
    sso: httpx.AsyncClient, provider: FakeProvider, db_session: AsyncSession
) -> None:
    # Not an allowed domain, but invited: `openfleet users create ... --sso`.
    await UserService(db_session).create("bob@elsewhere.example", None, UserRole.OPERATOR)
    claims = {"sub": "g-bob", "email": "Bob@Elsewhere.example", "email_verified": True}
    r = await sign_in(sso, provider, **claims)
    assert r.headers["location"] == "/"
    assert (await sso.get("/api/v1/auth/me", auth=None)).json()["role"] == "OPERATOR"
    # Another provider account claiming the same email can't take it over.
    r = await sign_in(sso, provider, **{**claims, "sub": "g-mallory"})
    assert refused(r, "linked to a different sign-in account")


@pytest.mark.parametrize(
    ("claims", "why"),
    [
        ({**GOOD, "email": "eve@evil.example"}, "has no access"),
        ({**GOOD, "email_verified": False}, "no verified email"),
        ({k: v for k, v in GOOD.items() if k != "email"}, "no verified email"),
    ],
)
async def test_unknown_or_unverified_accounts_are_refused(
    sso: httpx.AsyncClient, provider: FakeProvider, claims: dict, why: str
) -> None:
    assert refused(await sign_in(sso, provider, **claims), why)


async def test_a_disabled_user_is_refused(
    sso: httpx.AsyncClient, provider: FakeProvider, db_session: AsyncSession, clock
) -> None:
    assert (await sign_in(sso, provider, **GOOD)).headers["location"] == "/"
    await UserService(db_session).set_disabled("ada@robots.example", True, clock.now)
    assert refused(await sign_in(sso, provider, **GOOD), "disabled")


@pytest.mark.parametrize(
    ("tamper", "why"),
    [
        ({"sign_with": "other-key"}, "invalid"),
        ({"override": {"aud": "someone-else"}}, "invalid"),
        ({"override": {"iss": "https://evil.test"}}, "invalid"),
        ({"override": {"exp": 1000}}, "invalid"),
        ({"override": {"nonce": "replayed"}}, "nonce"),
    ],
)
async def test_forged_or_mismatched_id_tokens_are_refused(
    sso: httpx.AsyncClient, provider: FakeProvider, tamper: dict, why: str
) -> None:
    if "sign_with" in tamper:
        provider.sign_with = _key()
    provider.override = tamper.get("override", {})
    assert refused(await sign_in(sso, provider, **GOOD), why)


async def test_state_must_match_this_browsers_flow(
    sso: httpx.AsyncClient, provider: FakeProvider, make_client: ClientFactory
) -> None:
    start = await sso.get("/api/v1/auth/oidc/login", auth=None)
    code = provider.approve(start.headers["location"], **GOOD)
    r = await sso.get(
        "/api/v1/auth/oidc/callback", params={"code": code, "state": "forged"}, auth=None
    )
    assert refused(r, "expired or came from elsewhere")
    # No flow cookie at all (a link someone else started): refused too.
    sso.cookies.clear()
    r = await sso.get("/api/v1/auth/oidc/callback", params={"code": code, "state": "x"}, auth=None)
    assert refused(r, "expired or came from elsewhere")


async def test_the_user_cancelling_at_the_provider(sso: httpx.AsyncClient) -> None:
    await sso.get("/api/v1/auth/oidc/login", auth=None)
    r = await sso.get("/api/v1/auth/oidc/callback", params={"error": "access_denied"}, auth=None)
    assert refused(r, "cancelled")
