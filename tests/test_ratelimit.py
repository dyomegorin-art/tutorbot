import asyncio

import pytest
from httpx import AsyncClient

from app.errors import AppError
from app.ratelimit import check_rate_limit
from app.redis import redis_client

LOGIN = "/api/v1/auth/login"


def creds(email: str = "a@example.com") -> dict[str, str]:
    return {"email": email, "password": "wrong-password"}


async def test_login_limit_n_plus_one_is_429(client: AsyncClient) -> None:
    for _ in range(5):
        assert (await client.post(LOGIN, json=creds())).status_code == 401
    r = await client.post(LOGIN, json=creds())
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "rate_limited"
    assert 1 <= int(r.headers["Retry-After"]) <= 60


async def test_limit_is_per_email(client: AsyncClient) -> None:
    for _ in range(6):
        await client.post(LOGIN, json=creds())
    assert (await client.post(LOGIN, json=creds())).status_code == 429
    assert (await client.post(LOGIN, json=creds("other@example.com"))).status_code == 401
    # email нормализуется: смена регистра не обходит лимит
    assert (await client.post(LOGIN, json=creds("A@Example.com"))).status_code == 429


async def test_correct_password_is_also_limited(client: AsyncClient, make_tutor) -> None:
    t = await make_tutor()
    for _ in range(5):
        await client.post(LOGIN, json={"email": t.email, "password": "tutor-pass-1"})
    r = await client.post(LOGIN, json={"email": t.email, "password": "tutor-pass-1"})
    assert r.status_code == 429


async def test_window_resets_and_key_has_ttl() -> None:
    await check_rate_limit(redis_client, "t", "x", limit=1, window=1)
    assert 0 < await redis_client.ttl("rl:t:x") <= 1
    with pytest.raises(AppError) as e:
        await check_rate_limit(redis_client, "t", "x", limit=1, window=1)
    assert e.value.status_code == 429
    await asyncio.sleep(1.2)
    await check_rate_limit(redis_client, "t", "x", limit=1, window=1)


async def test_concurrent_requests_are_counted_exactly() -> None:
    results = await asyncio.gather(
        *(check_rate_limit(redis_client, "t", "c", limit=10) for _ in range(30)),
        return_exceptions=True,
    )
    assert sum(isinstance(r, AppError) for r in results) == 20


async def test_fail_open_without_redis(client: AsyncClient, redis_down) -> None:
    for _ in range(7):
        assert (await client.post(LOGIN, json=creds())).status_code == 401
