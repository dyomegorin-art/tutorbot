import asyncio

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from app.db import SessionLocal
from app.locks import acquire, release
from app.models import Booking
from app.redis import redis_client

URL = "/api/v1/bookings"


async def test_acquire_is_exclusive_and_release_checks_owner() -> None:
    key = "lock:test"
    t1 = await acquire(redis_client, key, 10)
    assert t1 is not None
    assert await acquire(redis_client, key, 10) is None
    assert await release(redis_client, key, "someone-else") is False
    assert await redis_client.get(key) == t1
    assert await release(redis_client, key, t1) is True
    assert await acquire(redis_client, key, 10) is not None


async def test_expired_holder_does_not_release_new_owners_lock() -> None:
    key = "lock:test"
    old = await acquire(redis_client, key, 1)
    assert old
    await asyncio.sleep(1.2)  # TTL истёк посреди «работы»
    new = await acquire(redis_client, key, 10)
    assert new is not None
    assert await release(redis_client, key, old) is False
    assert await redis_client.get(key) == new


async def booking_count() -> int:
    async with SessionLocal() as s:
        return await s.scalar(select(func.count()).select_from(Booking)) or 0


async def test_held_lock_gives_409_without_touching_db(
    client: AsyncClient, make_tutor, make_slot, make_student
) -> None:
    tutor = await make_tutor()
    slot = await make_slot(tutor.id)
    _, h = await make_student(1)
    await redis_client.set(f"lock:slot:{slot.id}", "other", ex=10)
    r = await client.post(URL, json={"slot_id": slot.id}, headers=h)
    assert r.status_code == 409 and r.json()["error"]["code"] == "slot_already_booked"
    assert await booking_count() == 0


async def test_lock_is_released_after_booking(
    client: AsyncClient, make_tutor, make_slot, make_student
) -> None:
    tutor = await make_tutor()
    slot = await make_slot(tutor.id)
    _, h = await make_student(1)
    assert (await client.post(URL, json={"slot_id": slot.id}, headers=h)).status_code == 201
    assert await redis_client.exists(f"lock:slot:{slot.id}") == 0


@pytest.mark.usefixtures("redis_down")
async def test_race_without_redis_db_constraint_still_holds(
    client: AsyncClient, make_tutor, make_slot, make_student
) -> None:
    tutor = await make_tutor()
    slot = await make_slot(tutor.id)
    students = [await make_student(i) for i in range(15)]
    responses = await asyncio.gather(
        *(client.post(URL, json={"slot_id": slot.id}, headers=h) for _, h in students)
    )
    assert sorted(r.status_code for r in responses) == [201] + [409] * 14
    assert await booking_count() == 1
