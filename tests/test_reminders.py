import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Booking, BookingStatus, Slot
from app.notifier import Recipient
from app.reminders import send_due_reminders
from app.worker import WorkerSettings, send_reminders


class FakeNotifier:
    def __init__(self, fail_for: set[str] | None = None) -> None:
        self.sent: list[Recipient] = []
        self.fail_for = fail_for or set()

    async def send(self, recipient: Recipient, message: str) -> None:
        if recipient.name in self.fail_for:
            raise RuntimeError("telegram is down")
        self.sent.append(recipient)


async def booking_at(
    tutor_id: int,
    student_id: int,
    minutes_from_now: int,
    status: BookingStatus = BookingStatus.active,
    reminded: bool = False,
) -> int:
    start = datetime.now(UTC) + timedelta(minutes=minutes_from_now)
    async with SessionLocal() as s:
        slot = Slot(tutor_id=tutor_id, starts_at=start, ends_at=start + timedelta(minutes=1))
        s.add(slot)
        await s.flush()
        b = Booking(
            slot_id=slot.id,
            student_id=student_id,
            status=status,
            reminded_at=datetime.now(UTC) if reminded else None,
        )
        s.add(b)
        await s.commit()
        return b.id


async def reminded_at(booking_id: int) -> datetime | None:
    async with SessionLocal() as s:
        return await s.scalar(select(Booking.reminded_at).where(Booking.id == booking_id))


async def test_only_suitable_bookings_and_no_repeats(make_tutor, make_student) -> None:
    tutor = await make_tutor()
    students = [(await make_student(i))[0] for i in range(1, 7)]
    due = await booking_at(tutor.id, students[0].id, 30)
    edge = await booking_at(tutor.id, students[1].id, 59)
    far = await booking_at(tutor.id, students[2].id, 180)
    cancelled = await booking_at(tutor.id, students[3].id, 31, BookingStatus.cancelled)
    already = await booking_at(tutor.id, students[4].id, 32, reminded=True)
    started = await booking_at(tutor.id, students[5].id, -10)

    notifier = FakeNotifier()
    assert await send_due_reminders(SessionLocal, notifier) == 2
    assert {r.name for r in notifier.sent} == {"Student 1", "Student 2"}
    assert await reminded_at(due) and await reminded_at(edge)
    for untouched in (far, cancelled, started):
        assert await reminded_at(untouched) is None

    assert await send_due_reminders(SessionLocal, notifier) == 0  # повторно не шлём
    assert len(notifier.sent) == 2
    assert already  # бронь с уже проставленным флагом не участвовала


async def test_far_booking_is_reminded_when_its_time_comes(make_tutor, make_student) -> None:
    tutor = await make_tutor()
    st, _ = await make_student(1)
    bid = await booking_at(tutor.id, st.id, 180)
    notifier = FakeNotifier()
    assert await send_due_reminders(SessionLocal, notifier) == 0
    later = datetime.now(UTC) + timedelta(minutes=150)
    assert await send_due_reminders(SessionLocal, notifier, now=later) == 1
    assert await reminded_at(bid)


async def test_failed_send_is_retried(make_tutor, make_student) -> None:
    tutor = await make_tutor()
    (a, _), (b, _) = await make_student(1), await make_student(2)
    ba = await booking_at(tutor.id, a.id, 20)
    bb = await booking_at(tutor.id, b.id, 21)

    assert await send_due_reminders(SessionLocal, FakeNotifier(fail_for={"Student 1"})) == 1
    assert await reminded_at(ba) is None and await reminded_at(bb)

    notifier = FakeNotifier()
    assert await send_due_reminders(SessionLocal, notifier) == 1
    assert [r.name for r in notifier.sent] == ["Student 1"]
    assert await reminded_at(ba)


async def test_parallel_workers_send_each_reminder_once(make_tutor, make_student) -> None:
    tutor = await make_tutor()
    for i in range(1, 6):
        st, _ = await make_student(i)
        await booking_at(tutor.id, st.id, 20 + i)
    notifier = FakeNotifier()
    results = await asyncio.gather(*(send_due_reminders(SessionLocal, notifier) for _ in range(4)))
    assert sum(results) == 5 and len(notifier.sent) == 5
    assert len({r.email for r in notifier.sent}) == 5


async def test_worker_job_and_schedule(make_tutor, make_student) -> None:
    job = WorkerSettings.cron_jobs[0]
    assert job.coroutine is send_reminders and job.minute == set(range(0, 60, 5))
    tutor = await make_tutor()
    st, _ = await make_student(1)
    await booking_at(tutor.id, st.id, 10)
    notifier = FakeNotifier()
    assert await send_reminders({"notifier": notifier}) == 1
    assert len(notifier.sent) == 1
