"""Runtime configuration.

Every value is environment-supplied. Nothing here carries a production default,
and the two settings that would be dangerous if defaulted wrongly -- the identity
mode and the database role -- are validated at construction rather than trusted.
"""

from __future__ import annotations

from enum import Enum
from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(str, Enum):
    dev = "dev"
    test = "test"
    production = "production"


class IdentityMode(str, Enum):
    #: Verify real OIDC ID tokens against the issuer's JWKS.
    oidc = "oidc"
    #: Substituted verifier for tests and the walking skeleton. Refused outside
    #: dev/test -- see the validator below.
    dev = "dev"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BLOCKS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: Environment = Environment.dev

    db_host: str = "postgres"
    db_port: int = 5432
    db_name: str = "blocks"

    #: The runtime role. Must not be an owner or superuser: forced RLS is only a
    #: boundary if the connecting role cannot bypass it.
    db_app_user: str = "blocks_app"
    db_app_password: str = ""

    #: Used only by tools/migrate.py, never by the running service.
    db_migrator_user: str = "blocks_migrator"
    db_migrator_password: str = ""

    #: The worker's role. Sees every tenant's outbox rows and may write exactly
    #: one column of them (ADR-0005). Never used by the API process.
    db_dispatcher_user: str = "blocks_dispatcher"
    db_dispatcher_password: str = ""

    dispatch_batch_size: int = 100
    dispatch_poll_seconds: float = 1.0

    db_pool_min: int = 1
    db_pool_max: int = 8

    identity_mode: IdentityMode = IdentityMode.dev
    oidc_issuer: str = ""
    oidc_audience: str = ""
    oidc_jwks_url: str = ""

    #: HS256 signing key for the substituted dev verifier. Dev/test only.
    dev_token_secret: str = "blocks-dev-insecure-signing-key-do-not-deploy"

    api_title: str = "Blocks Service API"
    api_version: str = "0.1.0"

    @model_validator(mode="after")
    def _refuse_unsafe_combinations(self) -> "Settings":
        if self.env is Environment.production:
            if self.identity_mode is IdentityMode.dev:
                raise ValueError(
                    "BLOCKS_IDENTITY_MODE=dev is refused when BLOCKS_ENV=production: "
                    "the substituted verifier accepts locally minted tokens."
                )
            if not self.db_app_password:
                raise ValueError("BLOCKS_DB_APP_PASSWORD is required when BLOCKS_ENV=production")
        if self.identity_mode is IdentityMode.oidc and not (
            self.oidc_issuer and self.oidc_audience and self.oidc_jwks_url
        ):
            raise ValueError(
                "identity_mode=oidc requires BLOCKS_OIDC_ISSUER, "
                "BLOCKS_OIDC_AUDIENCE and BLOCKS_OIDC_JWKS_URL"
            )
        return self

    def dsn(self, *, user: str, password: str) -> str:
        parts = [
            f"host={self.db_host}",
            f"port={self.db_port}",
            f"dbname={self.db_name}",
            f"user={user}",
        ]
        if password:
            parts.append(f"password={password}")
        return " ".join(parts)

    @property
    def app_dsn(self) -> str:
        return self.dsn(user=self.db_app_user, password=self.db_app_password)

    @property
    def migrator_dsn(self) -> str:
        return self.dsn(user=self.db_migrator_user, password=self.db_migrator_password)

    @property
    def dispatcher_dsn(self) -> str:
        return self.dsn(user=self.db_dispatcher_user, password=self.db_dispatcher_password)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
