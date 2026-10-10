from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field


class MarkPaidIn(BaseModel):
    amount: Decimal = Field(gt=0, max_digits=10, decimal_places=2)


class PaymentOut(BaseModel):
    id: int
    booking_id: int
    amount: Decimal
    status: Literal["pending", "paid"]
    paid_at: datetime | None
    source: Literal["manual", "webhook"] | None
