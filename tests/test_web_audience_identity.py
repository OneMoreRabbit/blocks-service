"""Service UC-04: one exact Google identity across Android and Web audiences.

The OIDC fixtures use disposable RSA keys and a real local JWKS endpoint.  No
test substitutes the verifier: every request crosses the same signature,
issuer, audience, lifetime, and required-claim checks as production.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import jwt
import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import ASGITransport, AsyncClient
from jwt.algorithms import RSAAlgorithm
from pydantic import ValidationError

from blocks_service.app import create_app
from blocks_service.config import Environment, IdentityMode, Settings
from blocks_service.identity import InvalidToken, OidcVerifier

ISSUER = "https://issuer.example.test"
ANDROID = "blocks-android.example.test"
WEB = "blocks-web.example.test"
THIRD = "untrusted-client.example.test"
KID = "disposable-uc-04-key"


@pytest.fixture(scope="module")
def oidc_keys():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = json.loads(RSAAlgorithm.to_jwk(private.public_key()))
    public_jwk.update({"kid": KID, "use": "sig", "alg": "RS256"})
    return private, other, public_jwk


@pytest.fixture(scope="module")
def jwks_url(oidc_keys) -> Iterator[str]:
    payload = json.dumps({"keys": [oidc_keys[2]]}).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - stdlib callback name
            if self.path != "/jwks.json":
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, _format, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/jwks.json"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.fixture
def oidc_token(oidc_keys):
    private, other, _ = oidc_keys

    def mint(
        *,
        audience=ANDROID,
        subject="same-google-subject",
        email="same@example.test",
        issuer=ISSUER,
        ttl=600,
        key=private,
        algorithm="RS256",
        omit=(),
        **extra,
    ):
        now = int(time.time())
        claims = {
            "iss": issuer,
            "aud": audience,
            "sub": subject,
            "email": email,
            "iat": now,
            "exp": now + ttl,
            **extra,
        }
        for name in omit:
            claims.pop(name, None)
        headers = {"kid": KID} if algorithm != "none" else {"kid": KID}
        if algorithm == "none":
            return jwt.encode(claims, key="", algorithm="none", headers=headers)
        if algorithm == "HS256":
            return jwt.encode(claims, "disposable-wrong-symmetric-key-32+", algorithm=algorithm,
                              headers=headers)
        return jwt.encode(claims, key, algorithm=algorithm, headers=headers)

    mint.other_key = other
    return mint


def _settings(base: Settings, jwks_url: str, *, audience: str = "", audiences=None) -> Settings:
    values = base.model_dump()
    values.update(
        env=Environment.test,
        identity_mode=IdentityMode.oidc,
        oidc_issuer=ISSUER,
        oidc_audience=audience,
        oidc_audiences=audiences,
        oidc_jwks_url=jwks_url,
    )
    return Settings(**values)


@asynccontextmanager
async def _client(settings: Settings) -> AsyncIterator[AsyncClient]:
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://oidc.test"
        ) as client:
            yield client


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _verifier(jwks_url: str, *audiences: str) -> OidcVerifier:
    return OidcVerifier(issuer=ISSUER, audiences=audiences, jwks_url=jwks_url)


def _identity_counts(settings: Settings, *, tenant_id: str, actor_id: str) -> tuple[int, int, int]:
    """Count the identity rows through the runtime role and its real RLS policy."""
    with psycopg.connect(settings.app_dsn) as conn:
        conn.execute("select set_config('blocks.tenant_id', %s, true)", (tenant_id,))
        conn.execute("select set_config('blocks.user_id', %s, true)", (actor_id,))
        return conn.execute(
            "select (select count(*) from blocks.users),"
            " (select count(*) from blocks.tenants),"
            " (select count(*) from blocks.memberships)"
        ).fetchone()


def test_legacy_single_audience_is_one_exact_entry(settings, jwks_url, oidc_token):
    configured = _settings(settings, jwks_url, audience=ANDROID)
    assert configured.accepted_oidc_audiences == (ANDROID,)
    verifier = _verifier(jwks_url, *configured.accepted_oidc_audiences)
    assert verifier.verify(oidc_token(audience=ANDROID)).subject == "same-google-subject"
    with pytest.raises(InvalidToken):
        verifier.verify(oidc_token(audience=WEB))
    with pytest.raises(InvalidToken):
        verifier.verify(oidc_token(audience=THIRD))

    restarted = _verifier(jwks_url, *_settings(
        settings, jwks_url, audience=ANDROID
    ).accepted_oidc_audiences)
    assert restarted.verify(oidc_token(audience=ANDROID)).subject == "same-google-subject"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"audiences": []},
        {"audiences": [""]},
        {"audiences": [" padded"]},
        {"audiences": ["client-*"]},
        {"audiences": [ANDROID, ANDROID]},
        {"audience": ANDROID, "audiences": [ANDROID, WEB]},
    ],
)
def test_invalid_or_ambiguous_audience_configuration_fails_before_serving(
    settings, jwks_url, kwargs
):
    with pytest.raises(ValidationError):
        _settings(settings, jwks_url, **kwargs)


def test_malformed_explicit_list_does_not_fall_back_to_legacy(monkeypatch):
    values = {
        "BLOCKS_ENV": "test",
        "BLOCKS_IDENTITY_MODE": "oidc",
        "BLOCKS_OIDC_ISSUER": ISSUER,
        "BLOCKS_OIDC_AUDIENCE": ANDROID,
        "BLOCKS_OIDC_AUDIENCES": "not-json",
        "BLOCKS_OIDC_JWKS_URL": "https://issuer.example.test/jwks",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_explicit_list_is_json_and_exact(monkeypatch):
    values = {
        "BLOCKS_ENV": "test",
        "BLOCKS_IDENTITY_MODE": "oidc",
        "BLOCKS_OIDC_ISSUER": ISSUER,
        "BLOCKS_OIDC_AUDIENCES": json.dumps([ANDROID, WEB]),
        "BLOCKS_OIDC_JWKS_URL": "https://issuer.example.test/jwks",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    configured = Settings(_env_file=None)
    assert configured.accepted_oidc_audiences == (ANDROID, WEB)


def test_audience_string_array_and_existing_azp_behavior(jwks_url, oidc_token):
    verifier = _verifier(jwks_url, ANDROID, WEB)
    assert verifier.verify(oidc_token(audience=ANDROID)).subject == "same-google-subject"
    assert verifier.verify(oidc_token(audience=[THIRD, WEB], azp=THIRD)).subject \
        == "same-google-subject"

    # The accepted 0.7 verifier has no separate authorized-party rule. A signed
    # azp claim neither grants access nor overrides an accepted aud claim.
    assert verifier.verify(oidc_token(audience=WEB, azp=THIRD)).subject \
        == "same-google-subject"
    with pytest.raises(InvalidToken):
        verifier.verify(oidc_token(audience=THIRD, azp=WEB))


def test_regression_second_configured_audience_is_accepted(jwks_url, oidc_token):
    """Also runs on 0.7: there it constructs the one-audience verifier and fails."""
    parameters = inspect.signature(OidcVerifier).parameters
    if "audiences" in parameters:
        verifier = OidcVerifier(
            issuer=ISSUER, audiences=(ANDROID, WEB), jwks_url=jwks_url
        )
    else:  # pragma: no cover - exercised only by the recorded old-head run
        verifier = OidcVerifier(issuer=ISSUER, audience=ANDROID, jwks_url=jwks_url)
    assert verifier.verify(oidc_token(audience=WEB)).subject == "same-google-subject"


@pytest.mark.parametrize(
    "changes",
    [
        {"audience": THIRD},
        {"issuer": "https://other-issuer.example.test"},
        {"ttl": -1},
        {"omit": ("aud",)},
        {"omit": ("exp",)},
        {"omit": ("iat",)},
        {"omit": ("sub",)},
        {"algorithm": "HS256"},
        {"algorithm": "none"},
    ],
)
def test_invalid_tokens_really_fail_verification(jwks_url, oidc_token, changes):
    with pytest.raises(InvalidToken):
        _verifier(jwks_url, ANDROID, WEB).verify(oidc_token(**changes))


def test_wrong_rsa_signature_really_fails_verification(jwks_url, oidc_token):
    with pytest.raises(InvalidToken):
        _verifier(jwks_url, ANDROID, WEB).verify(
            oidc_token(key=oidc_token.other_key)
        )


async def test_android_and_web_reuse_existing_identity_tenant_and_project(
    settings, jwks_url, oidc_token
):
    android = oidc_token(audience=ANDROID)
    web = oidc_token(audience=WEB)

    # Stage one: the new binary under the old one-audience setting.
    async with _client(_settings(settings, jwks_url, audience=ANDROID)) as client:
        first = await client.post("/v1/session", headers=_auth(android))
        assert first.status_code == 200, first.text
        retained = first.json()
        created = await client.post(
            f"/v1/tenants/{retained['personal_tenant_id']}/projects",
            headers=_auth(android),
            json={"name": "Retained Android project"},
        )
        assert created.status_code == 201, created.text
        project_id = created.json()["id"]
        assert (await client.post("/v1/session", headers=_auth(web))).status_code == 401

    before = _identity_counts(
        settings,
        tenant_id=retained["personal_tenant_id"],
        actor_id=retained["user_id"],
    )

    # Stage two: the same binary under the explicit Android + Web list.
    combined = _settings(settings, jwks_url, audiences=[ANDROID, WEB])
    async with _client(combined) as client:
        android_session, web_session = await asyncio.gather(
            client.post("/v1/session", headers=_auth(android)),
            client.post("/v1/session", headers=_auth(web)),
        )
        assert android_session.status_code == web_session.status_code == 200
        assert android_session.json()["user_id"] == web_session.json()["user_id"] \
            == retained["user_id"]
        assert android_session.json()["personal_tenant_id"] \
            == web_session.json()["personal_tenant_id"] \
            == retained["personal_tenant_id"]
        path = f"/v1/tenants/{retained['personal_tenant_id']}/projects/{project_id}"
        android_read = await client.get(path, headers=_auth(android))
        web_read = await client.get(path, headers=_auth(web))
        assert android_read.status_code == web_read.status_code == 200
        assert android_read.json() == web_read.json()
        assert android_read.json()["project"]["name"] == "Retained Android project"

    after = _identity_counts(
        settings,
        tenant_id=retained["personal_tenant_id"],
        actor_id=retained["user_id"],
    )
    assert after == before == (1, 1, 1)


async def test_concurrent_first_bootstrap_creates_one_identity(
    settings, jwks_url, oidc_token
):
    configured = _settings(settings, jwks_url, audiences=[ANDROID, WEB])
    async with _client(configured) as client:
        results = await asyncio.gather(
            client.post("/v1/session", headers=_auth(oidc_token(audience=ANDROID))),
            client.post("/v1/session", headers=_auth(oidc_token(audience=WEB))),
        )
    assert [response.status_code for response in results] == [200, 200]
    assert results[0].json()["user_id"] == results[1].json()["user_id"]
    assert results[0].json()["personal_tenant_id"] \
        == results[1].json()["personal_tenant_id"]

    counts = _identity_counts(
        settings,
        tenant_id=results[0].json()["personal_tenant_id"],
        actor_id=results[0].json()["user_id"],
    )
    assert counts == (1, 1, 1)


async def test_same_email_different_subject_stays_separate_and_cannot_read_project(
    settings, jwks_url, oidc_token
):
    configured = _settings(settings, jwks_url, audiences=[ANDROID, WEB])
    original = oidc_token(audience=ANDROID, subject="original", email="same@example.test")
    different = oidc_token(audience=WEB, subject="different", email="same@example.test")
    async with _client(configured) as client:
        first = (await client.post("/v1/session", headers=_auth(original))).json()
        project = await client.post(
            f"/v1/tenants/{first['personal_tenant_id']}/projects",
            headers=_auth(original), json={"name": "Original account only"},
        )
        second = await client.post("/v1/session", headers=_auth(different))
        assert second.status_code == 200
        assert second.json()["user_id"] != first["user_id"]
        path = f"/v1/tenants/{first['personal_tenant_id']}/projects/{project.json()['id']}"
        assert (await client.get(path, headers=_auth(different))).status_code == 403


async def test_invalid_tokens_fail_bootstrap_and_later_reads_after_prior_success(
    settings, jwks_url, oidc_token
):
    configured = _settings(settings, jwks_url, audiences=[ANDROID, WEB])
    valid = oidc_token(audience=ANDROID)
    invalid = [
        oidc_token(audience=THIRD),
        oidc_token(issuer="https://other-issuer.example.test"),
        oidc_token(omit=("aud",)),
        oidc_token(omit=("exp",)),
        oidc_token(omit=("iat",)),
        oidc_token(omit=("sub",)),
        oidc_token(key=oidc_token.other_key),
        oidc_token(algorithm="HS256"),
        oidc_token(algorithm="none"),
        oidc_token(ttl=-1),
    ]
    async with _client(configured) as client:
        session = await client.post("/v1/session", headers=_auth(valid))
        assert session.status_code == 200
        tenant = session.json()["personal_tenant_id"]
        for token in invalid:
            assert (await client.post("/v1/session", headers=_auth(token))).status_code == 401
            assert (await client.get(
                f"/v1/tenants/{tenant}/projects", headers=_auth(token)
            )).status_code == 401
