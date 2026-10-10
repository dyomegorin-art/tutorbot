from pydantic import BaseModel, Field, model_validator


class StudentOut(BaseModel):
    id: int
    user_id: int
    email: str
    name: str
    phone: str | None
    telegram_id: int | None


class StudentUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    phone: str | None = Field(default=None, max_length=32)
    telegram_id: int | None = None

    @model_validator(mode="after")
    def _not_empty(self) -> "StudentUpdate":
        if not self.model_fields_set:
            raise ValueError("Nothing to update")
        if "name" in self.model_fields_set and self.name is None:
            raise ValueError("name cannot be null")
        return self
