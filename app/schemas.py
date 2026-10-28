from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, Field, field_validator

Slug = Annotated[str, Field(pattern=r"^[a-z][-a-z0-9]{1,26}[a-z0-9]$")]


class TenantCreate(BaseModel):
    slug: Slug
    name: str = Field(min_length=1, max_length=255)

    @field_validator("name")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name must not be blank")
        return value.strip()


class TenantPatch(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    version: int = Field(ge=1)

    @field_validator("name")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name must not be blank")
        return value.strip()


class TenantView(BaseModel):
    id: str
    slug: str
    name: str
    status: str
    version: int
    created_at: datetime
    updated_at: datetime
    model_config = {"from_attributes": True}


class TaskView(BaseModel):
    id: str
    type: str
    tenant_id: str
    status: str
    created_at: datetime
    updated_at: datetime
    model_config = {"from_attributes": True}


class MutationView(BaseModel):
    tenant: TenantView
    task: TaskView


class ResultEvent(BaseModel):
    event_id: str
    task_id: str
    tenant_id: str
    status: str = Field(pattern=r"^(in_progress|done|failed)$")
