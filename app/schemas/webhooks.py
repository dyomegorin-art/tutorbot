from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel


class WebhookAccepted(BaseModel):
    id: int
    external_id: str
    status: Literal["accepted"] = "accepted"


class WebhookDuplicate(BaseModel):
    external_id: str
    status: Literal["duplicate"] = "duplicate"


class WebhookRequestOut(BaseModel):
    id: int
    external_id: str
    payload: dict[str, Any]
    status: Literal["new", "processed", "rejected"]
    created_at: datetime


class WebhookRequestUpdate(BaseModel):
    status: Literal["processed", "rejected"]
