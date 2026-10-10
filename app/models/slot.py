from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, func, text
from sqlalchemy.dialects.postgresql import ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Slot(Base):
    __tablename__ = "slots"
    __table_args__ = (
        CheckConstraint("ends_at > starts_at", name="ck_slots_ends_after_starts"),
        # Слоты одного репетитора не пересекаются (нужно расширение btree_gist).
        ExcludeConstraint(
            ("tutor_id", "="),
            (text("tstzrange(starts_at, ends_at)"), "&&"),
            name="ex_slots_no_overlap",
            using="gist",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tutor_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
