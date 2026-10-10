import logging
from dataclasses import dataclass
from typing import Protocol

log = logging.getLogger("app.notifier")


@dataclass(frozen=True)
class Recipient:
    name: str
    email: str
    telegram_id: int | None = None


class Notifier(Protocol):
    async def send(self, recipient: Recipient, message: str) -> None: ...


class LogNotifier:
    """Заглушка MVP: пишет напоминание в лог. Позже здесь появится Telegram."""

    async def send(self, recipient: Recipient, message: str) -> None:
        log.info(
            "reminder: %s",
            message,
            extra={"recipient_email": recipient.email, "telegram_id": recipient.telegram_id},
        )
