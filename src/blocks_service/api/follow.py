"""Proposed, separate latest Project-selection signal for already-open browsers."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .. import errors, follow
from .deps import TenantContext, tenant_context

router = APIRouter(prefix="/v1/tenants/{tenant_id}/follow/selection", tags=["follow"])


class FollowIntentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selection_sequence: int = Field(strict=True, ge=1, le=2**63 - 1)
    project_id: UUID
    intent_kind: Literal["explicit_button", "automatic_nfc"]
    dispatch_class: Literal["immediate", "deferred"]
    observed_revision: int | None = Field(default=None, strict=True, ge=0)
    age_ms: int = Field(strict=True, ge=0)

    @model_validator(mode="after")
    def class_has_correct_baseline(self) -> FollowIntentIn:
        if self.dispatch_class == "immediate" and self.observed_revision is not None:
            raise ValueError("immediate intent cannot carry an observed revision")
        if self.dispatch_class == "deferred" and self.observed_revision is None:
            raise ValueError("deferred intent needs its action-time revision")
        return self


class FollowReceiptOut(BaseModel):
    revision: int
    accepted_at: datetime
    expires_at: datetime
    replayed: bool
    stream_status: Literal["current", "expired", "superseded"]


class FollowSelectionOut(BaseModel):
    revision: int
    source_device_id: UUID
    intent_kind: Literal["explicit_button", "automatic_nfc"]
    project_id: UUID
    accepted_at: datetime
    expires_at: datetime


class FollowSnapshotOut(BaseModel):
    current_revision: int
    selection: FollowSelectionOut | None


class FollowRefusalDetail(BaseModel):
    code: Literal["invalid_request", "conflict", "not_found"]
    message: str
    reason: Literal[
        "intent_expired",
        "stale_candidate",
        "source_limit_reached",
        "stale_sequence",
        "intent_conflict",
        "target_unavailable",
    ]
    retryable: bool
    delivery: Literal["not_accepted"] | None = None
    current_revision: int | None = Field(default=None, ge=0)


class FollowRefusalOut(BaseModel):
    error: FollowRefusalDetail


@router.put(
    "/{source_device_id}",
    response_model=FollowReceiptOut,
    responses={
        **errors.error_responses(401, 403, 426),
        400: {"description": "Malformed or expired intent"},
        404: {"model": FollowRefusalOut, "description": "Target unavailable"},
        409: {
            "model": FollowRefusalOut,
            "description": "Sequence, stale intent or source cap",
        },
    },
    summary="Publish one bounded Project-navigation intent",
)
async def publish_selection(
    source_device_id: UUID,
    body: FollowIntentIn,
    ctx: TenantContext = Depends(tenant_context),
) -> FollowReceiptOut:
    result = await follow.publish(
        ctx.conn,
        tenant_id=ctx.tenant_id,
        user_id=ctx.actor_id,
        source_id=source_device_id,
        sequence=body.selection_sequence,
        project_id=body.project_id,
        kind=body.intent_kind,
        dispatch_class=body.dispatch_class,
        observed_revision=body.observed_revision,
        age_ms=body.age_ms,
    )
    return FollowReceiptOut(**result)


@router.get(
    "",
    response_model=FollowSnapshotOut,
    responses={
        **errors.error_responses(400, 401, 403, 426),
    },
    summary="Read the latest eligible Project-navigation hint without consuming it",
)
async def read_selection(
    automatic_source_device_id: UUID | None = Query(
        None, description="Optional source preference for automatic NFC only."
    ),
    ctx: TenantContext = Depends(tenant_context),
) -> FollowSnapshotOut:
    result = await follow.read_selection(
        ctx.conn,
        tenant_id=ctx.tenant_id,
        user_id=ctx.actor_id,
        automatic_source_device_id=automatic_source_device_id,
    )
    return FollowSnapshotOut(**result)
