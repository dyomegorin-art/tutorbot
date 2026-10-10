import asyncio

BOOKINGS = "/api/v1/bookings"
PAYMENTS = "/api/v1/payments"


async def setup(client, make_tutor, make_slot, make_student, login, n_students=1):
    tutor = await make_tutor()
    headers = await login(tutor.email, "tutor-pass-1")
    out = []
    for i in range(n_students):
        slot = await make_slot(tutor.id, hours_from_now=24 + i * 2)
        _, sh = await make_student(i + 1)
        b = (await client.post(BOOKINGS, json={"slot_id": slot.id}, headers=sh)).json()
        out.append((b["id"], sh))
    return tutor, headers, out


def pay(client, booking_id, headers, amount="1500.00"):
    return client.post(
        f"{PAYMENTS}/{booking_id}/mark-paid", json={"amount": amount}, headers=headers
    )


async def test_mark_paid(client, make_tutor, make_slot, make_student, login) -> None:
    _, th, [(bid, _)] = await setup(client, make_tutor, make_slot, make_student, login)
    r = await pay(client, bid, th)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "paid" and body["source"] == "manual" and body["paid_at"]
    assert float(body["amount"]) == 1500.0 and body["booking_id"] == bid
    r = await pay(client, bid, th)
    assert r.status_code == 409 and r.json()["error"]["code"] == "payment_already_paid"


async def test_mark_paid_errors(client, make_tutor, make_slot, make_student, login) -> None:
    _, th, [(bid, sh)] = await setup(client, make_tutor, make_slot, make_student, login)
    other = await make_tutor("t2@example.com")
    oh = await login(other.email, "tutor-pass-1")
    assert (await pay(client, bid, sh)).status_code == 403  # ученик
    assert (await pay(client, bid, oh)).status_code == 403  # чужой репетитор
    assert (await pay(client, 999, th)).status_code == 404
    assert (
        await client.post(f"{PAYMENTS}/{bid}/mark-paid", json={"amount": "1"})
    ).status_code == 401
    for bad in ("0", "-5", "abc", "10.123"):
        assert (await pay(client, bid, th, bad)).status_code == 422
    await client.post(f"{BOOKINGS}/{bid}/cancel", headers=sh)
    r = await pay(client, bid, th)
    assert r.status_code == 409 and r.json()["error"]["code"] == "booking_cancelled"


async def test_concurrent_mark_paid_single_payment(
    client, make_tutor, make_slot, make_student, login
) -> None:
    _, th, [(bid, _)] = await setup(client, make_tutor, make_slot, make_student, login)
    rs = await asyncio.gather(*(pay(client, bid, th) for _ in range(8)))
    assert sorted(r.status_code for r in rs) == [200] + [409] * 7
    assert (await client.get(PAYMENTS, headers=th)).json()["total"] == 1


async def test_list_visibility(client, make_tutor, make_slot, make_student, login) -> None:
    _, th, items = await setup(client, make_tutor, make_slot, make_student, login, 2)
    (b1, s1), (b2, s2) = items
    other = await make_tutor("t2@example.com")
    oh = await login(other.email, "tutor-pass-1")
    await pay(client, b1, th, "1000")
    await pay(client, b2, th, "2000")

    assert (await client.get(PAYMENTS, headers=th)).json()["total"] == 2
    assert (await client.get(PAYMENTS, headers=oh)).json()["total"] == 0
    mine = (await client.get(PAYMENTS, headers=s1)).json()
    assert mine["total"] == 1 and mine["items"][0]["booking_id"] == b1
    assert (await client.get(PAYMENTS, headers=s2)).json()["items"][0]["booking_id"] == b2
    r = await client.get(PAYMENTS, headers=th, params={"limit": 1, "offset": 1})
    assert len(r.json()["items"]) == 1 and r.json()["total"] == 2
    assert (await client.get(PAYMENTS, headers=th, params={"status": "pending"})).json()[
        "total"
    ] == 0
    assert (await client.get(PAYMENTS)).status_code == 401
