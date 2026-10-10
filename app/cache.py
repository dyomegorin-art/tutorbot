import logging

from redis.asyncio import Redis
from redis.exceptions import RedisError

log = logging.getLogger(__name__)

ALL = "all"


def _version_key(tutor_id: int | str) -> str:
    return f"schedule_version:{tutor_id}"


async def get_version(redis: Redis, tutor_id: int | None) -> str:
    """Текущая версия расписания: репетитора или общая (запросы без tutor_id)."""
    return await redis.get(_version_key(tutor_id if tutor_id is not None else ALL)) or "0"


async def bump_version(redis: Redis, tutor_id: int) -> None:
    """Инвалидация одной атомарной операцией: старые ключи кеша просто перестают читаться.

    Увеличиваем версию репетитора и общую (для списков по всем репетиторам).
    Если Redis недоступен, кеш доживёт до TTL — данные в БД при этом не страдают.
    """
    try:
        async with redis.pipeline(transaction=True) as pipe:
            pipe.incr(_version_key(tutor_id))
            pipe.incr(_version_key(ALL))
            await pipe.execute()
    except RedisError:
        log.warning("failed to bump schedule version", extra={"tutor_id": tutor_id})


def slots_key(version: str, tutor_id: int | None, **params: object) -> str:
    parts = ":".join(f"{k}={'' if v is None else v}" for k, v in params.items())
    return f"slots:v{version}:tutor={tutor_id if tutor_id is not None else ALL}:{parts}"
