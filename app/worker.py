import logging
from typing import Any

from arq import cron
from arq.connections import RedisSettings

from app.config import get_settings
from app.db import SessionLocal, engine
from app.logging import setup_logging
from app.notifier import LogNotifier
from app.reminders import send_due_reminders

log = logging.getLogger("app.worker")


async def send_reminders(ctx: dict[str, Any]) -> int:
    sent = await send_due_reminders(SessionLocal, ctx["notifier"])
    log.info("reminders job finished", extra={"sent": sent})
    return sent


async def startup(ctx: dict[str, Any]) -> None:
    setup_logging(get_settings().log_level)
    ctx["notifier"] = LogNotifier()


async def shutdown(ctx: dict[str, Any]) -> None:
    await engine.dispose()


class WorkerSettings:
    functions = [send_reminders]
    # Периодическая задача раз в 5 минут: отмена брони не требует чистить очередь,
    # а рестарт воркера ничего не теряет.
    cron_jobs = [cron(send_reminders, minute=set(range(0, 60, 5)), run_at_startup=True)]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
