import asyncio
import json

from httpx import AsyncClient

from app.api.webhooks import sign
from app.config import get_settings

S = "/api/v1/students"
W = "/api/v1/webhooks/requests"


async def two_students(client, make_tutor, make_slot, make_student, login):
    tutor = await make_tutor()
    th = await login(tutor.email, "tutor-pass-1")
    other = await make_tutor("t2@example.com")
    oh = await login(other.email, "tutor-pass-1")
    (s1, h1), (s2, h2) = await make_student(1), await make_student(2)
    slot = await make_slot(tutor.id)
    await client.post("/api/v1/bookings", json={"slot_id": slot.id}, headers=h1)
    return th, oh, (s1, h1), (s2, h2)


async def test_me_get_and_patch(client: AsyncClient, make_student, make_tutor, login) -> None:
    st, h = await make_student(1)
    r = await client.get(f"{S}/me", headers=h)
    assert r.status_code == 200 and r.json()["name"] == "Student 1" and r.json()["email"]
    r = await client.patch(f"{S}/me", json={"phone": "+79990001122", "telegram_id": 42}, headers=h)
    assert r.status_code == 200 and r.json()["phone"] == "+79990001122"
    assert (await client.get(f"{S}/me", headers=h)).json()["telegram_id"] == 42
    assert (await client.patch(f"{S}/me", json={"name": "New"}, headers=h)).json()["name"] == "New"
    # очистка необязательного поля разрешена, имени — нет
    assert (await client.patch(f"{S}/me", json={"phone": None}, headers=h)).json()["phone"] is None
    for bad in ({}, {"name": ""}, {"name": None}, {"telegram_id": "x"}):
        assert (await client.patch(f"{S}/me", json=bad, headers=h)).status_code == 422

    tutor = await make_tutor()
    th = await login(tutor.email, "tutor-pass-1")
    assert (await client.get(f"{S}/me", headers=th)).status_code == 403
    assert (await client.get(f"{S}/me")).status_code == 401


async def test_tutor_sees_only_his_students(
    client: AsyncClient, make_tutor, make_slot, make_student, login
) -> None:
    th, oh, (s1, h1), (s2, h2) = await two_students(
        client, make_tutor, make_slot, make_student, login
    )
    r = await client.get(S, headers=th)
    assert [i["id"] for i in r.json()["items"]] == [s1.id] and r.json()["total"] == 1
    assert (await client.get(S, headers=oh)).json()["total"] == 0
    assert (await client.get(S, headers=h1)).status_code == 403
    assert (await client.get(S)).status_code == 401
    assert (await client.get(S, headers=th, params={"limit": 0})).status_code == 422

    assert (await client.get(f"{S}/{s1.id}", headers=th)).status_code == 200
    assert (await client.get(f"{S}/{s2.id}", headers=th)).status_code == 403  # не записан к нему
    assert (await client.get(f"{S}/{s1.id}", headers=oh)).status_code == 403
    assert (await client.get(f"{S}/999", headers=th)).status_code == 404


async def test_student_sees_only_himself(
    client: AsyncClient, make_tutor, make_slot, make_student, login
) -> None:
    _, _, (s1, h1), (s2, h2) = await two_students(
        client, make_tutor, make_slot, make_student, login
    )
    assert (await client.get(f"{S}/{s1.id}", headers=h1)).status_code == 200
    assert (await client.get(f"{S}/{s2.id}", headers=h1)).status_code == 403


def signed(external_id: str) -> dict:
    body = json.dumps({"external_id": external_id}).encode()
    return {
        "content": body,
        "headers": {"X-Signature": sign(body, get_settings().webhook_secret)},
    }


async def test_tutor_handles_webhook_requests(
    client: AsyncClient, make_tutor, make_student, login
) -> None:
    tutor = await make_tutor()
    th = await login(tutor.email, "tutor-pass-1")
    _, sh = await make_student(1)
    rid = (await client.post(W, **signed("a"))).json()["id"]
    await client.post(W, **signed("b"))

    assert (await client.get(W, headers=sh)).status_code == 403
    assert (await client.get(W)).status_code == 401
    assert (await client.get(W, headers=th)).json()["total"] == 2
    assert (await client.get(f"{W}/{rid}", headers=th)).json()["payload"] == {"external_id": "a"}
    assert (await client.get(f"{W}/999", headers=th)).status_code == 404

    r = await client.patch(f"{W}/{rid}", json={"status": "processed"}, headers=th)
    assert r.status_code == 200 and r.json()["status"] == "processed"
    r = await client.patch(f"{W}/{rid}", json={"status": "rejected"}, headers=th)
    assert r.status_code == 409 and r.json()["error"]["code"] == "request_already_handled"
    assert (
        await client.patch(f"{W}/999", json={"status": "rejected"}, headers=th)
    ).status_code == 404
    assert (await client.patch(f"{W}/{rid}", json={"status": "new"}, headers=th)).status_code == 422
    assert (
        await client.patch(f"{W}/{rid}", json={"status": "processed"}, headers=sh)
    ).status_code == 403
    assert (await client.get(W, headers=th, params={"status": "new"})).json()["total"] == 1
    assert (await client.get(W, headers=th, params={"status": "processed"})).json()["total"] == 1


async def test_concurrent_handling_has_one_winner(client: AsyncClient, make_tutor, login) -> None:
    tutor = await make_tutor()
    th = await login(tutor.email, "tutor-pass-1")
    rid = (await client.post(W, **signed("a"))).json()["id"]
    rs = await asyncio.gather(
        *(client.patch(f"{W}/{rid}", json={"status": "processed"}, headers=th) for _ in range(6))
    )
    assert sorted(r.status_code for r in rs) == [200] + [409] * 5
