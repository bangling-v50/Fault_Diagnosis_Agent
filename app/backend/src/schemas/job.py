from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class JobPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    tenant_id: int | None
    kind: str
    resource_type: str
    resource_id: int
    status: str
    attempt: int
    max_attempts: int
    timeout_seconds: int
    payload: dict
    available_at: datetime
    lease_owner: str | None
    lease_expires_at: datetime | None
    cancel_requested_at: datetime | None
    error_message: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class JobEventPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    job_id: int
    sequence: int
    event_type: str
    payload: dict
    created_at: datetime
