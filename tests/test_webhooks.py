import asyncio
import json

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from app.api.webhooks import sign
from app.config import get_settings
from app.db import SessionLocal
from app.models import WebhookRequest
from app.redis import redis_client

URL = "/api/v1/webhooks/requests"


def signed(payload: dict | None = None, **extra) -> dict:
    body = json.dumps(payload if payload is not None else {"external_id": "ext-1", "name": "Ivan"})
    body = body.encode()
    return {
        "content": body,
        "headers": {
            "Content-Type": "application/json",
            "X-Signature": sign(body, get_settings().webhook_secret),
            **extra,
        },
    }


async def rows() -> int:
    async with SessionLocal() as s:
        return await s.scalar(select(func.count()).select_from(WebhookRequest)) or 0


async def test_valid_request_accepted(client: AsyncClient) -> None:
    r = await client.post(URL, **signed())
    assert r.status_code == 202
    assert r.json()["status"] == "accepted" and r.json()["external_id"] == "ext-1"
    async with SessionLocal() as s:
        row = await s.scalar(select(WebhookRequest))
        assert row and row.status.value == "new" and row.payload["name"] == "Ivan"


async def test_bad_signature_is_401(client: AsyncClient) -> None:
    kwargs = signed()
    r = await client.post(URL, content=kwargs["content"], headers={"X-Signature": "deadbeef"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_signature"
    assert (await client.post(URL, content=kwargs["content"])).status_code == 401  # без заголовка
    # подпись от другого тела
    other = signed({"external_id": "ext-2"})
    r = await client.post(URL, content=kwargs["content"], headers=other["headers"])
    assert r.status_code == 401
    assert await rows() == 0


async def test_duplicate_does_not_create_second_row(client: AsyncClient) -> None:
    assert (await client.post(URL, **signed())).status_code == 202
    r = await client.post(URL, **signed())
    assert r.status_code == 200 and r.json()["status"] == "duplicate"
    assert await rows() == 1


async def test_duplicate_detected_by_db_when_redis_key_lost(client: AsyncClient) -> None:
    assert (await client.post(URL, **signed())).status_code == 202
    await redis_client.flushdb()
    assert (await client.post(URL, **signed())).status_code == 200
    assert await rows() == 1


async def test_concurrent_deliveries_create_one_row(client: AsyncClient) -> None:
    rs = await asyncio.gather(*(client.post(URL, **signed()) for _ in range(10)))
    assert sorted(r.status_code for r in rs) == [200] * 9 + [202]
    assert await rows() == 1


async def test_concurrent_deliveries_without_redis(client: AsyncClient, redis_down) -> None:
    rs = await asyncio.gather(*(client.post(URL, **signed()) for _ in range(10)))
    assert sorted(r.status_code for r in rs) == [200] * 9 + [202]
    assert await rows() == 1


@pytest.mark.parametrize("payload", [{"name": "x"}, {"external_id": ""}, {"external_id": 5}, [1]])
async def test_invalid_payload_is_422(client: AsyncClient, payload) -> None:
    r = await client.post(URL, **signed(payload))
    assert r.status_code == 422 and await rows() == 0


async def test_rate_limit(client: AsyncClient, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "rate_limit_webhook_per_minute", 3)
    for i in range(3):
        assert (await client.post(URL, **signed({"external_id": f"e{i}"}))).status_code == 202
    r = await client.post(URL, **signed({"external_id": "e3"}))
    assert r.status_code == 429 and "Retry-After" in r.headers
    assert await rows() == 3
