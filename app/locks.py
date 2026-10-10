import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.errors import AppError

log = logging.getLogger(__name__)

# Снимаем блокировку только если она всё ещё наша: после истечения TTL ключ может
# принадлежать уже другому запросу, и обычный DEL снял бы чужую блокировку.
RELEASE_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""


async def acquire(redis: Redis, key: str, ttl: int) -> str | None:
    """Возвращает токен владельца или None, если блокировка занята."""
    token = uuid.uuid4().hex
    return token if await redis.set(key, token, nx=True, ex=ttl) else None


async def release(redis: Redis, key: str, token: str) -> bool:
    return bool(await redis.eval(RELEASE_SCRIPT, 1, key, token))  # type: ignore[misc]


@asynccontextmanager
async def slot_lock(redis: Redis, slot_id: int, ttl: int) -> AsyncIterator[None]:
    """Быстрый отсев конкурентных бронирований одного слота.

    Занято → сразу 409, в БД не ходим. Redis недоступен → работаем без блокировки:
    корректность всё равно гарантирует частичный уникальный индекс в БД.
    """
    key = f"lock:slot:{slot_id}"
    try:
        token = await acquire(redis, key, ttl)
    except RedisError:
        log.warning("redis unavailable, booking without lock", extra={"slot_id": slot_id})
        yield
        return
    if token is None:
        raise AppError(409, "slot_already_booked", "Slot is being booked by someone else")
    try:
        yield
    finally:
        try:
            await release(redis, key, token)
        except RedisError:
            log.warning("failed to release slot lock", extra={"slot_id": slot_id})
