"""Models exposed by Notifier's HTTP API."""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, field_validator

MATRIX_USER_ID_PATTERN = re.compile(r"^@[^:\s]+:.+$")


class ConversationStatus(StrEnum):
    """State of the authorization granted by the recipient."""

    PENDING = "pending"
    ACTIVE = "active"
    REVOKED = "revoked"
    ERROR = "error"


class DeliveryStatus(StrEnum):
    """State of a notification delivery."""

    AWAITING_RECIPIENT = "awaiting_recipient"
    QUEUED = "queued"
    SENDING = "sending"
    SENT = "sent"
    RECIPIENT_DECLINED = "recipient_declined"
    FAILED = "failed"


class DocumentChangeType(StrEnum):
    """Document changes supported by the standard message template."""

    CREATED = "created"
    UPDATED = "updated"
    RENAMED = "renamed"
    COMMENTED = "commented"
    SHARED = "shared"
    DELETED = "deleted"
    RESTORED = "restored"


class ApiModel(BaseModel):
    """Validation rules shared by API inputs."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")


class SubscriptionRequest(ApiModel):
    """Explicit request to enable Tchap notifications."""

    recipient: str = Field(min_length=4, max_length=255)

    @field_validator("recipient")
    @classmethod
    def validate_recipient(cls, value: str) -> str:
        if not MATRIX_USER_ID_PATTERN.fullmatch(value):
            raise ValueError(
                "a full Matrix ID is required, for example @alice:example.org"
            )
        return value


class DocumentNotificationRequest(SubscriptionRequest):
    """Structured notification produced for a document change."""

    idempotency_key: str = Field(min_length=1, max_length=200)
    actor_name: str = Field(min_length=1, max_length=200)
    document_id: str = Field(min_length=1, max_length=200)
    document_title: str = Field(min_length=1, max_length=500)
    change_type: DocumentChangeType
    document_url: AnyHttpUrl | None = None
    occurred_at: datetime | None = None

    @field_validator("actor_name", "document_title")
    @classmethod
    def collapse_line_breaks(cls, value: str) -> str:
        return " ".join(value.split())


class SubscriptionResponse(BaseModel):
    recipient: str
    status: ConversationStatus
    room_id: str | None = None


class DeliveryResponse(BaseModel):
    delivery_id: str
    idempotency_key: str
    recipient: str
    status: DeliveryStatus
    room_id: str | None = None
    event_id: str | None = None
    error: str | None = None


class HealthResponse(BaseModel):
    status: str
    matrix_connected: bool
