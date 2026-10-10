import enum
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Enum, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class WebhookStatus(enum.StrEnum):
    new = "new"
    processed = "processed"
    rejected = "rejected"


class WebhookRequest(Base):
    __tablename__ = "webhook_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    external_id: Mapped[str] = mapped_column(String(255), unique=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[WebhookStatus] = mapped_column(
        Enum(WebhookStatus, native_enum=False, length=16), default=WebhookStatus.new
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
