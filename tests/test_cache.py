from httpx import AsyncClient

from app.redis import redis_client

SLOTS = "/api/v1/slots"
BOOKINGS = "/api/v1/bookings"


def slot_body(day: int, start: int = 10, end: int = 11) -> dict[str, str]:
    return {
        "starts_at": f"2030-01-{day:02d}T{start:02d}:00:00+00:00",
        "ends_at": f"2030-01-{day:02d}T{end:02d}:00:00+00:00",
    }


async def get(client: AsyncClient, headers: dict[str, str], **params):
    r = await client.get(SLOTS, headers=headers, params=params)
    assert r.status_code == 200
    return r.headers["X-Cache"], r.json()


async def tutor(make_tutor, login, email="tutor@example.com"):
    t = await make_tutor(email)
    return t, await login(email, "tutor-pass-1")


async def test_miss_then_hit_with_ttl(client: AsyncClient, make_tutor, login) -> None:
    t, h = await tutor(make_tutor, login)
    await client.post(SLOTS, json=slot_body(1), headers=h)
    assert (await get(client, h, tutor_id=t.id))[0] == "MISS"
    cache, body = await get(client, h, tutor_id=t.id)
    assert cache == "HIT" and body["total"] == 1
    keys = [k async for k in redis_client.scan_iter("slots:v*")]
    assert len(keys) == 1 and "tutor=" in keys[0]
    assert 0 < await redis_client.ttl(keys[0]) <= 300


async def test_different_filters_do_not_share_cache(client: AsyncClient, make_tutor, login) -> None:
    t, h = await tutor(make_tutor, login)
    await client.post(SLOTS, json=slot_body(1), headers=h)
    await client.post(SLOTS, json=slot_body(2), headers=h)
    await get(client, h, tutor_id=t.id)
    cache, body = await get(client, h, tutor_id=t.id, date="2030-01-02")
    assert cache == "MISS" and body["total"] == 1
    assert (await get(client, h, tutor_id=t.id, limit=1))[1]["total"] == 2


async def test_slot_changes_invalidate(client: AsyncClient, make_tutor, login) -> None:
    t, h = await tutor(make_tutor, login)
    assert (await get(client, h, tutor_id=t.id))[1]["total"] == 0
    assert (await get(client, h))[1]["total"] == 0  # общий список тоже закеширован

    sid = (await client.post(SLOTS, json=slot_body(1), headers=h)).json()["id"]
    for params in ({"tutor_id": t.id}, {}):
        cache, body = await get(client, h, **params)
        assert cache == "MISS" and body["total"] == 1

    await client.patch(f"{SLOTS}/{sid}", json=slot_body(1, 12, 13), headers=h)
    cache, body = await get(client, h, tutor_id=t.id)
    assert cache == "MISS" and body["items"][0]["starts_at"].startswith("2030-01-01T12:00")

    await client.delete(f"{SLOTS}/{sid}", headers=h)
    cache, body = await get(client, h, tutor_id=t.id)
    assert cache == "MISS" and body["total"] == 0


async def test_booking_and_cancel_invalidate(
    client: AsyncClient, make_tutor, make_slot, make_student, login
) -> None:
    t, h = await tutor(make_tutor, login)
    slot = await make_slot(t.id)
    _, sh = await make_student(1)
    assert (await get(client, h, tutor_id=t.id, free="true"))[1]["total"] == 1

    b = (await client.post(BOOKINGS, json={"slot_id": slot.id}, headers=sh)).json()
    cache, body = await get(client, h, tutor_id=t.id, free="true")
    assert cache == "MISS" and body["total"] == 0

    await client.post(f"{BOOKINGS}/{b['id']}/cancel", headers=sh)
    cache, body = await get(client, h, tutor_id=t.id, free="true")
    assert cache == "MISS" and body["total"] == 1


async def test_other_tutors_change_keeps_my_cache(client: AsyncClient, make_tutor, login) -> None:
    t1, h1 = await tutor(make_tutor, login)
    _, h2 = await tutor(make_tutor, login, "t2@example.com")
    await get(client, h1, tutor_id=t1.id)
    await get(client, h1)
    await client.post(SLOTS, json=slot_body(1), headers=h2)
    assert (await get(client, h1, tutor_id=t1.id))[0] == "HIT"  # мои слоты не менялись
    cache, body = await get(client, h1)
    assert cache == "MISS" and body["total"] == 1  # общий список обновился


async def test_works_without_redis(client: AsyncClient, make_tutor, login, redis_down) -> None:
    t, h = await tutor(make_tutor, login)
    r = await client.post(SLOTS, json=slot_body(1), headers=h)
    assert r.status_code == 201
    cache, body = await get(client, h, tutor_id=t.id)
    assert cache == "MISS" and body["total"] == 1
    assert (await get(client, h, tutor_id=t.id))[0] == "MISS"
