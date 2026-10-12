import logging
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Booking,
    BookingStatus,
    Payment,
    PaymentSource,
    PaymentStatus,
    WebhookStatus,
)

log = logging.getLogger(__name__)

PAYMENT_TYPE = "payment"


def _parse_amount(raw: Any) -> Decimal | None:
    if isinstance(raw, bool) or not isinstance(raw, int | float | str):
        return None
    try:
        amount = Decimal(str(raw))
    except InvalidOperation:
        return None
    if not amount.is_finite() or amount <= 0 or amount != amount.quantize(Decimal("0.01")):
        return None
    if amount >= Decimal(10) ** 8:
        return None
    return amount


async def apply_payment(session: AsyncSession, payload: dict[str, Any]) -> WebhookStatus:
    """Заявка `{"type": "payment", "booking_id": ..., "amount": ...}` отмечает оплату брони.

    Подпись уже проверена, поэтому платёж проводится сразу (`source=webhook`). Некорректную
    заявку не теряем, а помечаем `rejected` — репетитор увидит её в списке заявок.
    Выполняется в той же транзакции, что и сохранение заявки: дубль `external_id`
    откатывает и платёж.
    """
    booking_id = payload.get("booking_id")
    amount = _parse_amount(payload.get("amount"))
    if isinstance(booking_id, bool) or not isinstance(booking_id, int) or amount is None:
        return WebhookStatus.rejected

    booking = await session.scalar(
        select(Booking).where(Booking.id == booking_id).with_for_update()
    )
    if booking is None or booking.status != BookingStatus.active:
        return WebhookStatus.rejected
    payment = await session.scalar(select(Payment).where(Payment.booking_id == booking.id))
    if payment is not None and payment.status == PaymentStatus.paid:
        return WebhookStatus.rejected
    if payment is None:
        payment = Payment(booking_id=booking.id)
        session.add(payment)
    payment.amount = amount
    payment.status = PaymentStatus.paid
    payment.paid_at = datetime.now(UTC)
    payment.source = PaymentSource.webhook
    return WebhookStatus.processed
