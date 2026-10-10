from httpx import AsyncClient

from app.db import SessionLocal
from app.models import Booking, BookingStatus, Student, User, UserRole

URL = "/api/v1/slots"


def slot(day: int, start: int = 10, end: int = 11) -> dict[str, str]:
    return {
        "starts_at": f"2026-11-{day:02d}T{start:02d}:00:00+00:00",
        "ends_at": f"2026-11-{day:02d}T{end:02d}:00:00+00:00",
    }


async def tutor_headers(make_tutor, login, email: str = "tutor@example.com") -> dict[str, str]:
    await make_tutor(email)
    return await login(email, "tutor-pass-1")


async def student_headers(client: AsyncClient, login) -> dict[str, str]:
    await client.post(
        "/api/v1/auth/register",
        json={"email": "s@example.com", "password": "password-1", "name": "S"},
    )
    return await login("s@example.com", "password-1")


async def book(slot_id: int, status: BookingStatus = BookingStatus.active) -> None:
    async with SessionLocal() as s:
        user = User(email="b@example.com", password_hash="x", role=UserRole.student)
        s.add(user)
        await s.flush()
        st = Student(user_id=user.id, name="B")
        s.add(st)
        await s.flush()
        s.add(Booking(slot_id=slot_id, student_id=st.id, status=status))
        await s.commit()


async def test_create_and_permissions(client: AsyncClient, make_tutor, login) -> None:
    t = await tutor_headers(make_tutor, login)
    s = await student_headers(client, login)
    assert (await client.post(URL, json=slot(1))).status_code == 401
    assert (await client.post(URL, json=slot(1), headers=s)).status_code == 403
    r = await client.post(URL, json=slot(1), headers=t)
    assert r.status_code == 201 and r.json()["is_free"] is True


async def test_validation(client: AsyncClient, make_tutor, login) -> None:
    t = await tutor_headers(make_tutor, login)
    bad = {"starts_at": "2026-11-01T11:00:00+00:00", "ends_at": "2026-11-01T10:00:00+00:00"}
    assert (await client.post(URL, json=bad, headers=t)).status_code == 422
    naive = {"starts_at": "2026-11-01T10:00:00", "ends_at": "2026-11-01T11:00:00"}
    assert (await client.post(URL, json=naive, headers=t)).status_code == 422


async def test_overlap_409_but_adjacent_ok(client: AsyncClient, make_tutor, login) -> None:
    t = await tutor_headers(make_tutor, login)
    assert (await client.post(URL, json=slot(1, 10, 12), headers=t)).status_code == 201
    r = await client.post(URL, json=slot(1, 11, 13), headers=t)
    assert r.status_code == 409 and r.json()["error"]["code"] == "slot_overlap"
    assert (await client.post(URL, json=slot(1, 12, 13), headers=t)).status_code == 201
    # у другого репетитора то же время — можно
    t2 = await tutor_headers(make_tutor, login, "t2@example.com")
    assert (await client.post(URL, json=slot(1, 10, 12), headers=t2)).status_code == 201


async def test_list_filters_and_pagination(client: AsyncClient, make_tutor, login) -> None:
    t = await tutor_headers(make_tutor, login)
    ids = [(await client.post(URL, json=slot(d), headers=t)).json()["id"] for d in (1, 2, 3)]
    await book(ids[1])

    r = await client.get(URL, headers=t, params={"date": "2026-11-02"})
    assert [i["id"] for i in r.json()["items"]] == [ids[1]]
    r = await client.get(
        URL, headers=t, params={"date_from": "2026-11-02", "date_to": "2026-11-03"}
    )
    assert r.json()["total"] == 2
    r = await client.get(URL, headers=t, params={"free": "true"})
    assert [i["id"] for i in r.json()["items"]] == [ids[0], ids[2]]
    r = await client.get(URL, headers=t, params={"free": "false"})
    assert [(i["id"], i["is_free"]) for i in r.json()["items"]] == [(ids[1], False)]
    r = await client.get(URL, headers=t, params={"limit": 2, "offset": 2})
    body = r.json()
    assert (body["total"], body["limit"], body["offset"], len(body["items"])) == (3, 2, 2, 1)
    assert (await client.get(URL, headers=t, params={"limit": 101})).status_code == 422
    assert (await client.get(URL, headers=t, params={"tutor_id": 999})).json()["total"] == 0
    assert (await client.get(URL)).status_code == 401


async def test_get_one(client: AsyncClient, make_tutor, login) -> None:
    t = await tutor_headers(make_tutor, login)
    sid = (await client.post(URL, json=slot(1), headers=t)).json()["id"]
    assert (await client.get(f"{URL}/{sid}", headers=t)).status_code == 200
    assert (await client.get(f"{URL}/999", headers=t)).status_code == 404


async def test_patch_rules(client: AsyncClient, make_tutor, login) -> None:
    t = await tutor_headers(make_tutor, login)
    other = await tutor_headers(make_tutor, login, "t2@example.com")
    a = (await client.post(URL, json=slot(1, 10, 11), headers=t)).json()["id"]
    b = (await client.post(URL, json=slot(1, 12, 13), headers=t)).json()["id"]
    assert (
        await client.patch(f"{URL}/{a}", json={"ends_at": slot(1, 10, 12)["ends_at"]}, headers=t)
    ).status_code == 200
    assert (await client.patch(f"{URL}/{a}", json=slot(1, 12, 13), headers=t)).status_code == 409
    assert (
        await client.patch(f"{URL}/{a}", json=slot(1, 10, 11), headers=other)
    ).status_code == 403
    assert (await client.patch(f"{URL}/999", json=slot(1), headers=t)).status_code == 404
    assert (
        await client.patch(f"{URL}/{a}", json={"ends_at": "2026-11-01T09:00:00+00:00"}, headers=t)
    ).status_code == 422
    await book(b)
    r = await client.patch(f"{URL}/{b}", json=slot(1, 14, 15), headers=t)
    assert r.status_code == 409 and r.json()["error"]["code"] == "slot_has_active_booking"


async def test_delete_rules(client: AsyncClient, make_tutor, login) -> None:
    t = await tutor_headers(make_tutor, login)
    other = await tutor_headers(make_tutor, login, "t2@example.com")
    free = (await client.post(URL, json=slot(1), headers=t)).json()["id"]
    booked = (await client.post(URL, json=slot(2), headers=t)).json()["id"]
    cancelled = (await client.post(URL, json=slot(3), headers=t)).json()["id"]
    await book(booked)
    async with SessionLocal() as s:
        st = await s.scalar(__import__("sqlalchemy").select(Student.id))
        s.add(Booking(slot_id=cancelled, student_id=st, status=BookingStatus.cancelled))
        await s.commit()

    assert (await client.delete(f"{URL}/{free}", headers=other)).status_code == 403
    assert (await client.delete(f"{URL}/{free}", headers=t)).status_code == 204
    assert (await client.delete(f"{URL}/{free}", headers=t)).status_code == 404
    assert (await client.delete(f"{URL}/{booked}", headers=t)).status_code == 409
    assert (await client.delete(f"{URL}/{cancelled}", headers=t)).status_code == 204
