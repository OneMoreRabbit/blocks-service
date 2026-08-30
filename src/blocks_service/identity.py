"""The OIDC boundary.

The service never implements identity; it verifies an assertion produced by an
external issuer and maps it to a local user. That boundary is one Protocol with
two implementations, chosen by configuration:

  OidcVerifier  real issuer, real JWKS, RS256/ES256, audience and issuer checked.
  DevVerifier   locally minted HS256 tokens, for tests and the walking skeleton.

Substituting the verifier is the whole point: it lets tenancy, membership and
RLS be proven now, on real PostgreSQL, without an identity provider on the
critical path -- and without those tests later needing to be rewritten against
the real one. `Settings` refuses DevVerifier when BLOCKS_ENV=production.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import jwt
from jwt import PyJWKClient

from .config import IdentityMode, Settings

DEV_ISSUER = "https://dev.blocks.invalid"
DEV_AUDIENCE = "blocks-service-dev"


class InvalidToken(Exception):
    """The presented credential is absent, malformed, expired, or not trusted."""


@dataclass(frozen=True, slots=True)
class Principal:
    """A verified external identity, before it is mapped to a local user."""

    issuer: str
    subject: str
    email: str | None


class IdentityVerifier(Protocol):
    def verify(self, token: str) -> Principal: ...


class OidcVerifier:
    def __init__(self, *, issuer: str, audience: str, jwks_url: str) -> None:
        self._issuer = issuer
        self._audience = audience
        self._jwks = PyJWKClient(jwks_url, cache_keys=True)

    def verify(self, token: str) -> Principal:
        try:
            key = self._jwks.get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256", "ES256"],
                issuer=self._issuer,
                audience=self._audience,
                options={"require": ["exp", "iat", "iss", "sub", "aud"]},
            )
        except Exception as exc:  # PyJWT raises a family; the caller needs one
            raise InvalidToken(str(exc)) from exc
        return _principal_from(claims)


class DevVerifier:
    """Verifies HS256 tokens signed with a shared development secret.

    Still a real signature check against a real expiry -- an unsigned or expired
    token is rejected here exactly as it would be by the OIDC verifier -- so the
    authorization tests exercise the same failure paths.
    """

    def __init__(self, *, secret: str) -> None:
        self._secret = secret

    def verify(self, token: str) -> Principal:
        try:
            claims = jwt.decode(
                token,
                self._secret,
                algorithms=["HS256"],
                issuer=DEV_ISSUER,
                audience=DEV_AUDIENCE,
                options={"require": ["exp", "iat", "iss", "sub", "aud"]},
            )
        except Exception as exc:
            raise InvalidToken(str(exc)) from exc
        return _principal_from(claims)

    @staticmethod
    def mint(secret: str, *, subject: str, email: str | None = None, ttl: int = 3600) -> str:
        """Mint a development token. Test and local-tooling helper only."""
        import time

        now = int(time.time())
        payload = {
            "iss": DEV_ISSUER,
            "aud": DEV_AUDIENCE,
            "sub": subject,
            "iat": now,
            "exp": now + ttl,
        }
        if email:
            payload["email"] = email
        return jwt.encode(payload, secret, algorithm="HS256")


def _principal_from(claims: dict) -> Principal:
    subject = claims.get("sub")
    issuer = claims.get("iss")
    if not subject or not issuer:
        raise InvalidToken("token is missing 'sub' or 'iss'")
    return Principal(issuer=str(issuer), subject=str(subject), email=claims.get("email"))


def build_verifier(settings: Settings) -> IdentityVerifier:
    if settings.identity_mode is IdentityMode.oidc:
        return OidcVerifier(
            issuer=settings.oidc_issuer,
            audience=settings.oidc_audience,
            jwks_url=settings.oidc_jwks_url,
        )
    return DevVerifier(secret=settings.dev_token_secret)
