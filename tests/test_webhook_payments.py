import json

from httpx import AsyncClient

from app.api.webhooks import sign
from app.config import get_settings

W = "/api/v1/webhooks/requests"


def send(client: AsyncClient, payload: dict):
    body = json.dumps(payload).encode()
    return client.post(
        W, content=body, headers={"X-Signature": sign(body, get_settings().webhook_secret)}
    )


async def booked(client, make_tutor, make_slot, make_student, login):
    tutor = await make_tutor()
    th = await login(tutor.email, "tutor-pass-1")
    slot = await make_slot(tutor.id)
    _, sh = await make_student(1)
    bid = (await client.post("/api/v1/bookings", json={"slot_id": slot.id}, headers=sh)).json()[
        "id"
    ]
    return th, sh, bid


async def status_of(client, th, request_id: int) -> str:
    return (await client.get(f"{W}/{request_id}", headers=th)).json()["status"]


def pay(external_id: str, booking_id, amount="1500.00") -> dict:
    return {
        "external_id": external_id,
        "type": "payment",
        "booking_id": booking_id,
        "amount": amount,
    }


async def test_payment_webhook_marks_paid(client, make_tutor, make_slot, make_student, login):
    th, sh, bid = await booked(client, make_tutor, make_slot, make_student, login)
    r = await send(client, pay("p1", bid))
    assert r.status_code == 202
    assert await status_of(client, th, r.json()["id"]) == "processed"
    for headers in (th, sh):
        item = (await client.get("/api/v1/payments", headers=headers)).json()["items"][0]
        assert item["status"] == "paid" and item["source"] == "webhook"
        assert float(item["amount"]) == 1500.0 and item["booking_id"] == bid


async def test_duplicate_delivery_pays_once(client, make_tutor, make_slot, make_student, login):
    th, _, bid = await booked(client, make_tutor, make_slot, make_student, login)
    assert (await send(client, pay("p1", bid))).status_code == 202
    assert (await send(client, pay("p1", bid, "9999"))).status_code == 200
    payments = (await client.get("/api/v1/payments", headers=th)).json()
    assert payments["total"] == 1 and float(payments["items"][0]["amount"]) == 1500.0


async def test_invalid_payments_are_rejected_not_applied(
    client, make_tutor, make_slot, make_student, login
):
    th, sh, bid = await booked(client, make_tutor, make_slot, make_student, login)
    bad = [
        pay("a", 999),  # нет такой брони
        pay("b", bid, "0"),
        pay("c", bid, "-5"),
        pay("d", bid, "abc"),
        pay("e", bid, "10.123"),
        pay("f", "1"),  # booking_id не число
        {"external_id": "g", "type": "payment", "amount": "10"},  # нет booking_id
    ]
    for payload in bad:
        r = await send(client, payload)
        assert r.status_code == 202, payload
        assert await status_of(client, th, r.json()["id"]) == "rejected", payload
    assert (await client.get("/api/v1/payments", headers=th)).json()["total"] == 0


async def test_cancelled_and_already_paid_are_rejected(
    client, make_tutor, make_slot, make_student, login
):
    th, sh, bid = await booked(client, make_tutor, make_slot, make_student, login)
    await client.post(f"/api/v1/payments/{bid}/mark-paid", json={"amount": "700"}, headers=th)
    r = await send(client, pay("again", bid))
    assert await status_of(client, th, r.json()["id"]) == "rejected"
    item = (await client.get("/api/v1/payments", headers=th)).json()["items"][0]
    assert item["source"] == "manual" and float(item["amount"]) == 700.0

    await client.post(f"/api/v1/bookings/{bid}/cancel", headers=sh)
    r = await send(client, pay("cancelled", bid))
    assert await status_of(client, th, r.json()["id"]) == "rejected"


async def test_other_requests_stay_new(client, make_tutor, login):
    tutor = await make_tutor()
    th = await login(tutor.email, "tutor-pass-1")
    r = await send(client, {"external_id": "x", "name": "Ivan"})
    assert await status_of(client, th, r.json()["id"]) == "new"
