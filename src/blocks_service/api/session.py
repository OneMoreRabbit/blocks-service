"""Session bootstrap: the only route that may create a user."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from .. import repository
from ..db import Database
from ..identity import Principal
from .deps import get_db, verified_principal

router = APIRouter(prefix="/v1", tags=["session"])


class TenantOut(BaseModel):
    id: UUID
    kind: str = Field(description="`personal` or `organization`.")
    name: str


class SessionOut(BaseModel):
    user_id: UUID
    email: str | None
    personal_tenant_id: UUID = Field(
        description="The user's one personal tenant, created on first login."
    )
    tenants: list[TenantOut]


@router.post(
    "/session",
    response_model=SessionOut,
    summary="Exchange a verified identity token for a session",
    description=(
        "Idempotent. First call for a subject creates the user, their personal "
        "tenant, and the owner membership; later calls return the same ids."
    ),
)
async def bootstrap(
    principal: Principal = Depends(verified_principal),
    db: Database = Depends(get_db),
) -> SessionOut:
    session = await repository.bootstrap_session(db, principal)
    return SessionOut(
        user_id=session.user_id,
        email=session.email,
        personal_tenant_id=session.personal_tenant_id,
        tenants=[TenantOut(id=t.id, kind=t.kind, name=t.name) for t in session.tenants],
    )
