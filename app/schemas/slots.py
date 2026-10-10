from datetime import datetime

from pydantic import AwareDatetime, BaseModel, model_validator


class SlotCreate(BaseModel):
    starts_at: AwareDatetime
    ends_at: AwareDatetime

    @model_validator(mode="after")
    def _check_order(self) -> "SlotCreate":
        if self.ends_at <= self.starts_at:
            raise ValueError("ends_at must be later than starts_at")
        return self


class SlotUpdate(BaseModel):
    starts_at: AwareDatetime | None = None
    ends_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _check(self) -> "SlotUpdate":
        if self.starts_at is None and self.ends_at is None:
            raise ValueError("Nothing to update")
        if self.starts_at and self.ends_at and self.ends_at <= self.starts_at:
            raise ValueError("ends_at must be later than starts_at")
        return self


class SlotOut(BaseModel):
    id: int
    tutor_id: int
    starts_at: datetime
    ends_at: datetime
    is_free: bool
    created_at: datetime
    updated_at: datetime
