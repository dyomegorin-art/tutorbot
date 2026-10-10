from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AppError
from app.models import Booking, BookingStatus, Slot, Student, User, UserRole

ACTIVE_SLOT_INDEX = "uq_bookings_active_slot"


def already_booked() -> AppError:
    return AppError(409, "slot_already_booked", "Slot is already booked")


async def create_booking(session: AsyncSession, slot_id: int, student: Student) -> Booking:
    """Источник истины против двойной брони — частичный уникальный индекс в БД."""
    slot = await session.get(Slot, slot_id)
    if slot is None:
        raise AppError(404, "slot_not_found", "Slot not found")
    if slot.starts_at <= datetime.now(UTC):
        raise AppError(422, "slot_in_past", "Cannot book a slot that has already started")
    booking = Booking(slot_id=slot.id, student_id=student.id)
    session.add(booking)
    try:
        await session.commit()
    except IntegrityError as e:
        await session.rollback()
        if ACTIVE_SLOT_INDEX in str(e.orig):
            raise already_booked() from None
        if "foreign key" in str(e.orig).lower():  # слот удалили между проверкой и вставкой
            raise AppError(404, "slot_not_found", "Slot not found") from None
        raise
    await session.refresh(booking)
    return booking


async def get_participant_booking(
    session: AsyncSession, booking_id: int, user: User, *, lock: bool = False
) -> tuple[Booking, Slot]:
    """Бронь + слот; 404, если нет, 403, если пользователь не участник."""
    stmt = (
        select(Booking, Slot, Student.user_id)
        .join(Slot, Slot.id == Booking.slot_id)
        .join(Student, Student.id == Booking.student_id)
        .where(Booking.id == booking_id)
    )
    if lock:
        stmt = stmt.with_for_update(of=Booking)
    row = (await session.execute(stmt)).first()
    if row is None:
        raise AppError(404, "booking_not_found", "Booking not found")
    booking, slot, student_user_id = row
    is_participant = (
        student_user_id == user.id if user.role == UserRole.student else slot.tutor_id == user.id
    )
    if not is_participant:
        raise AppError(403, "forbidden", "Not your booking")
    return booking, slot


async def cancel_booking(
    session: AsyncSession, booking_id: int, user: User
) -> tuple[Booking, Slot]:
    booking, slot = await get_participant_booking(session, booking_id, user, lock=True)
    if booking.status == BookingStatus.cancelled:
        raise AppError(409, "booking_already_cancelled", "Booking is already cancelled")
    booking.status = BookingStatus.cancelled
    booking.cancelled_at = datetime.now(UTC)
    await session.commit()
    return booking, slot
