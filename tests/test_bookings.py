import asyncio

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.db import SessionLocal
from app.models import Booking, BookingStatus

URL = "/api/v1/bookings"


async def active_count(slot_id: int) -> int:
    async with SessionLocal() as s:
        return (
            await s.scalar(
                select(func.count()).where(
                    Booking.slot_id == slot_id, Booking.status == BookingStatus.active
                )
            )
            or 0
        )


@pytest.mark.parametrize("n", [20])
async def test_concurrent_booking_one_winner(
    client: AsyncClient, make_tutor, make_slot, make_student, n: int
) -> None:
    tutor = await make_tutor()
    slot = await make_slot(tutor.id)
    students = [await make_student(i) for i in range(n)]

    responses = await asyncio.gather(
        *(client.post(URL, json={"slot_id": slot.id}, headers=h) for _, h in students)
    )

    codes = sorted(r.status_code for r in responses)
    assert codes == [201] + [409] * (n - 1), codes
    assert {r.json()["error"]["code"] for r in responses if r.status_code == 409} == {
        "slot_already_booked"
    }
    assert await active_count(slot.id) == 1


async def test_db_constraint_blocks_second_active_booking(make_tutor, make_slot, make_student):
    """Без какой-либо блокировки в приложении индекс в БД не даёт создать вторую бронь."""
    tutor = await make_tutor()
    slot = await make_slot(tutor.id)
    (s1, _), (s2, _) = await make_student(1), await make_student(2)
    async with SessionLocal() as s:
        s.add(Booking(slot_id=slot.id, student_id=s1.id))
        await s.commit()
    async with SessionLocal() as s:
        s.add(Booking(slot_id=slot.id, student_id=s2.id))
        with pytest.raises(IntegrityError):
            await s.commit()
    assert await active_count(slot.id) == 1


async def test_booking_errors(client: AsyncClient, make_tutor, make_slot, make_student, login):
    tutor = await make_tutor()
    t_headers = await login(tutor.email, "tutor-pass-1")
    _, h = await make_student(1)
    past = await make_slot(tutor.id, hours_from_now=-1)
    assert (await client.post(URL, json={"slot_id": 999}, headers=h)).status_code == 404
    r = await client.post(URL, json={"slot_id": past.id}, headers=h)
    assert r.status_code == 422 and r.json()["error"]["code"] == "slot_in_past"
    assert (await client.post(URL, json={"slot_id": 1})).status_code == 401
    assert (await client.post(URL, json={"slot_id": 1}, headers=t_headers)).status_code == 403
    assert (await client.post(URL, json={})).status_code in (401, 422)


async def test_cancel_and_rebook(client: AsyncClient, make_tutor, make_slot, make_student, login):
    tutor = await make_tutor()
    t_headers = await login(tutor.email, "tutor-pass-1")
    slot = await make_slot(tutor.id)
    _, h1 = await make_student(1)
    _, h2 = await make_student(2)

    b = (await client.post(URL, json={"slot_id": slot.id}, headers=h1)).json()
    assert (await client.post(URL, json={"slot_id": slot.id}, headers=h1)).status_code == 409
    assert (await client.post(f"{URL}/{b['id']}/cancel", headers=h2)).status_code == 403
    assert (await client.post(f"{URL}/999/cancel", headers=h2)).status_code == 404
    r = await client.post(f"{URL}/{b['id']}/cancel", headers=h1)
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    r = await client.post(f"{URL}/{b['id']}/cancel", headers=t_headers)
    assert r.status_code == 409 and r.json()["error"]["code"] == "booking_already_cancelled"
    # после отмены слот снова можно забронировать
    assert (await client.post(URL, json={"slot_id": slot.id}, headers=h2)).status_code == 201


async def test_visibility(client: AsyncClient, make_tutor, make_slot, make_student, login):
    tutor = await make_tutor()
    other_tutor = await make_tutor("t2@example.com")
    t_headers = await login(tutor.email, "tutor-pass-1")
    ot_headers = await login(other_tutor.email, "tutor-pass-1")
    slot = await make_slot(tutor.id)
    _, h1 = await make_student(1)
    _, h2 = await make_student(2)
    b = (await client.post(URL, json={"slot_id": slot.id}, headers=h1)).json()

    # ученик не видит чужую бронь
    assert (await client.get(f"{URL}/{b['id']}", headers=h2)).status_code == 403
    assert (await client.get(f"{URL}/{b['id']}", headers=h1)).status_code == 200
    assert (await client.get(f"{URL}/{b['id']}", headers=t_headers)).status_code == 200
    assert (await client.get(f"{URL}/{b['id']}", headers=ot_headers)).status_code == 403
    assert (await client.get(f"{URL}/999", headers=h1)).status_code == 404

    assert (await client.get(URL, headers=h1)).json()["total"] == 1
    assert (await client.get(URL, headers=h2)).json()["total"] == 0
    assert (await client.get(URL, headers=t_headers)).json()["total"] == 1
    assert (await client.get(URL, headers=ot_headers)).json()["total"] == 0
    r = await client.get(URL, headers=t_headers, params={"status": "cancelled"})
    assert r.json()["total"] == 0
