import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Booking, BookingStatus, Slot, Student, User
from app.notifier import Notifier, Recipient

log = logging.getLogger(__name__)

REMIND_BEFORE = timedelta(minutes=60)


async def send_due_reminders(
    session_factory: async_sessionmaker[AsyncSession],
    notifier: Notifier,
    now: datetime | None = None,
) -> int:
    """Напоминает о занятиях, которые начнутся в ближайшие 60 минут. Возвращает число отправленных.

    Сначала бронь атомарно «занимается» (`UPDATE ... WHERE reminded_at IS NULL`, строки
    берутся через SKIP LOCKED), и только потом уходит уведомление. Поэтому два воркера
    или повторный запуск не пришлют напоминание дважды. Если отправка упала, флаг
    сбрасывается, и следующий запуск повторит попытку, пока занятие не началось.
    """
    now = now or datetime.now(UTC)
    async with session_factory() as session:
        due = (
            select(Booking.id)
            .join(Slot, Slot.id == Booking.slot_id)
            .where(
                Booking.status == BookingStatus.active,
                Booking.reminded_at.is_(None),
                Slot.starts_at > now,
                Slot.starts_at <= now + REMIND_BEFORE,
            )
            .with_for_update(of=Booking, skip_locked=True)
        )
        claimed = (
            await session.scalars(
                update(Booking)
                .where(Booking.id.in_(due), Booking.reminded_at.is_(None))
                .values(reminded_at=now)
                .returning(Booking.id)
            )
        ).all()
        await session.commit()
        if not claimed:
            return 0
        rows = (
            await session.execute(
                select(Booking.id, Slot.starts_at, Student.name, Student.telegram_id, User.email)
                .join(Slot, Slot.id == Booking.slot_id)
                .join(Student, Student.id == Booking.student_id)
                .join(User, User.id == Student.user_id)
                .where(Booking.id.in_(claimed))
            )
        ).all()

    sent = 0
    failed: list[int] = []
    for booking_id, starts_at, name, telegram_id, email in rows:
        message = f"{name}, напоминание: занятие начнётся в {starts_at:%Y-%m-%d %H:%M} UTC"
        try:
            await notifier.send(Recipient(name, email, telegram_id), message)
            sent += 1
        except Exception:
            log.exception("reminder failed", extra={"booking_id": booking_id})
            failed.append(booking_id)
    if failed:
        async with session_factory() as session:
            await session.execute(
                update(Booking).where(Booking.id.in_(failed)).values(reminded_at=None)
            )
            await session.commit()
    return sent
