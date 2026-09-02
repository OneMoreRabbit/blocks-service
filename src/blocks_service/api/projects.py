"""Projects: the first tenant-scoped domain resource."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field

from .. import errors, repository
from .deps import TenantContext, tenant_context

router = APIRouter(prefix="/v1/tenants/{tenant_id}/projects", tags=["projects"])


class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class ProjectOut(BaseModel):
    id: UUID
    tenant_id: UUID
    name: str


@router.get(
    "",
    response_model=list[ProjectOut],
    responses=errors.error_responses(401, 403, 426),
    summary="List the tenant's projects",
)
async def list_projects(ctx: TenantContext = Depends(tenant_context)) -> list[ProjectOut]:
    # No tenant filter in the query and none needed: the connection is scoped,
    # and the policy is what makes that sufficient.
    projects = await repository.list_projects(ctx.conn)
    return [ProjectOut(id=p.id, tenant_id=p.tenant_id, name=p.name) for p in projects]


@router.post(
    "",
    response_model=ProjectOut,
    status_code=status.HTTP_201_CREATED,
    responses=errors.error_responses(400, 401, 403, 426),
    summary="Create a project in the tenant",
)
async def create_project(
    body: ProjectIn, ctx: TenantContext = Depends(tenant_context)
) -> ProjectOut:
    project = await repository.create_project(ctx.conn, ctx.tenant_id, body.name)
    return ProjectOut(id=project.id, tenant_id=project.tenant_id, name=project.name)
