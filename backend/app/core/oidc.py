"""OpenID Connect sign-in (DECISIONS #038), built for Google; any standard issuer works.

Authorization code flow with PKCE, run by the backend (a confidential client): the browser
only ever sees the provider's login page and, at the end, our usual session cookie.

    /auth/oidc/login     state, nonce, PKCE verifier -> short-lived HttpOnly cookie;
                         302 to the provider's authorization endpoint
    /auth/oidc/callback  state must match the cookie; the code is exchanged (with the
                         verifier and the client secret) for an ID token, which is verified:
                         signature (the provider's JWKS), iss, aud, exp/iat, nonce

Discovery and keys are cached; an unknown key id refetches the keys once (key rotation).
"""

import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt

FLOW_COOKIE = "openfleet_oidc"
FLOW_TTL_S = 600  # the user has 10 minutes on the provider's page
CACHE_TTL_S = 3600
LEEWAY_S = 60  # clock skew between us and the provider


class OidcError(Exception):
    """Sign-in failed. The message is safe to show to the user."""


@dataclass(frozen=True)
class Flow:
    state: str
    nonce: str
    verifier: str

    @staticmethod
    def new() -> "Flow":
        return Flow(*(secrets.token_urlsafe(32) for _ in range(3)))

    def cookie(self) -> str:
        raw = json.dumps({"s": self.state, "n": self.nonce, "v": self.verifier})
        return base64.urlsafe_b64encode(raw.encode()).decode()

    @staticmethod
    def from_cookie(value: str | None) -> "Flow | None":
        if not value:
            return None
        try:
            d = json.loads(base64.urlsafe_b64decode(value.encode()))
            return Flow(str(d["s"]), str(d["n"]), str(d["v"]))
        except (ValueError, KeyError, TypeError):
            return None

    def challenge(self) -> str:
        digest = hashlib.sha256(self.verifier.encode()).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


@dataclass(frozen=True)
class Identity:
    subject: str  # "<issuer>|<sub>": stable across email changes, unique across issuers
    email: str | None
    email_verified: bool
    name: str | None


class OidcClient:
    def __init__(
        self,
        issuer: str,
        client_id: str,
        client_secret: str,
        redirect_url: str,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.issuer = issuer.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_url = redirect_url
        self.http = http or httpx.AsyncClient(timeout=10)
        self._discovery: tuple[float, dict[str, Any]] | None = None
        self._keys: tuple[float, dict[str, jwt.PyJWK]] | None = None

    async def close(self) -> None:
        await self.http.aclose()

    # --- discovery and keys ------------------------------------------------------------

    async def _get_json(self, url: str) -> dict[str, Any]:
        try:
            r = await self.http.get(url)
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise OidcError("the sign-in provider is unreachable; try again") from exc

    async def discovery(self) -> dict[str, Any]:
        if self._discovery and time.monotonic() - self._discovery[0] < CACHE_TTL_S:
            return self._discovery[1]
        doc = await self._get_json(f"{self.issuer}/.well-known/openid-configuration")
        if doc.get("issuer", "").rstrip("/") != self.issuer:
            raise OidcError("the sign-in provider's configuration doesn't match its issuer")
        self._discovery = (time.monotonic(), doc)
        return doc

    async def _key(self, kid: str | None) -> jwt.PyJWK:
        for refresh in (False, True):
            if refresh or not self._keys or time.monotonic() - self._keys[0] > CACHE_TTL_S:
                jwks = await self._get_json((await self.discovery())["jwks_uri"])
                keys = {}
                for k in jwks.get("keys", []):
                    try:
                        keys[k.get("kid")] = jwt.PyJWK(k)
                    except jwt.PyJWTError:
                        continue  # a key type we don't use
                self._keys = (time.monotonic(), keys)
            assert self._keys is not None
            if kid in self._keys[1]:
                return self._keys[1][kid]
        raise OidcError("the sign-in token was signed with an unknown key")

    # --- the flow ------------------------------------------------------------------------

    async def authorization_url(self, flow: Flow) -> str:
        endpoint = (await self.discovery())["authorization_endpoint"]
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_url,
            "scope": "openid email profile",
            "state": flow.state,
            "nonce": flow.nonce,
            "code_challenge": flow.challenge(),
            "code_challenge_method": "S256",
            "prompt": "select_account",
        }
        return f"{endpoint}?{urlencode(params)}"

    async def identity(self, code: str, flow: Flow) -> Identity:
        """Exchange the code and verify the ID token."""
        endpoint = (await self.discovery())["token_endpoint"]
        try:
            r = await self.http.post(
                endpoint,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": self.redirect_url,
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "code_verifier": flow.verifier,
                },
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise OidcError("the sign-in provider is unreachable; try again") from exc
        if r.status_code != 200:
            raise OidcError("the sign-in provider rejected the sign-in; try again")
        try:
            token = r.json()["id_token"]
            header = jwt.get_unverified_header(token)
        except (ValueError, KeyError, jwt.PyJWTError) as exc:
            raise OidcError("the sign-in provider sent no valid ID token") from exc
        if header.get("alg") in (None, "none", "HS256", "HS384", "HS512"):
            raise OidcError("the sign-in token is not signed with the provider's key")
        key = await self._key(header.get("kid"))
        try:
            claims = jwt.decode(
                token,
                key=key,
                algorithms=[header["alg"]],
                audience=self.client_id,
                issuer=self.issuer,
                leeway=LEEWAY_S,
                options={"require": ["iss", "aud", "exp", "iat", "sub", "nonce"]},
            )
        except jwt.PyJWTError as exc:
            raise OidcError(f"the sign-in token is invalid ({exc})") from exc
        if not secrets.compare_digest(str(claims.get("nonce")), flow.nonce):
            raise OidcError("the sign-in token is not for this sign-in (nonce)")
        verified = claims.get("email_verified")
        return Identity(
            subject=f"{self.issuer}|{claims['sub']}",
            email=(claims.get("email") or None),
            email_verified=verified is True or verified == "true",
            name=claims.get("name"),
        )
