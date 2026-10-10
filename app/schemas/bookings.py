from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class BookingCreate(BaseModel):
    slot_id: int = Field(gt=0)


class BookingOut(BaseModel):
    id: int
    slot_id: int
    student_id: int
    tutor_id: int
    status: Literal["active", "cancelled"]
    starts_at: datetime
    ends_at: datetime
    created_at: datetime
    cancelled_at: datetime | None
