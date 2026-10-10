from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_role
from app.db import get_session
from app.errors import AppError
from app.models import (
    Booking,
    BookingStatus,
    Payment,
    PaymentSource,
    PaymentStatus,
    Slot,
    Student,
    User,
    UserRole,
)
from app.schemas.common import Page, Pagination
from app.schemas.errors import ErrorResponse
from app.schemas.payments import MarkPaidIn, PaymentOut

router = APIRouter(prefix="/payments", tags=["payments"])

_tutor_only = require_role(UserRole.tutor)
_auth_errors: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse, "description": "Нет или невалидный токен"}
}


def _out(p: Payment) -> PaymentOut:
    return PaymentOut(
        id=p.id,
        booking_id=p.booking_id,
        amount=p.amount,
        status=p.status.value,
        paid_at=p.paid_at,
        source=p.source.value if p.source else None,
    )


def _already_paid() -> AppError:
    return AppError(409, "payment_already_paid", "Booking is already paid")


@router.get(
    "",
    response_model=Page[PaymentOut],
    summary="Оплаты: репетитор — по своим слотам, ученик — свои",
    responses=_auth_errors,
)
async def list_payments(
    status: Literal["pending", "paid"] | None = Query(None),
    page: Pagination = Depends(),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Page[PaymentOut]:
    base = (
        select(Payment)
        .join(Booking, Booking.id == Payment.booking_id)
        .join(Slot, Slot.id == Booking.slot_id)
        .join(Student, Student.id == Booking.student_id)
    )
    if user.role == UserRole.student:
        base = base.where(Student.user_id == user.id)
    else:
        base = base.where(Slot.tutor_id == user.id)
    if status is not None:
        base = base.where(Payment.status == PaymentStatus(status))
    total = await session.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = await session.scalars(
        base.order_by(Payment.id.desc()).limit(page.limit).offset(page.offset)
    )
    return Page[PaymentOut](
        items=[_out(p) for p in rows], total=total, limit=page.limit, offset=page.offset
    )


@router.post(
    "/{booking_id}/mark-paid",
    response_model=PaymentOut,
    summary="Отметить оплату вручную (tutor-владелец слота)",
    responses={
        **_auth_errors,
        403: {"model": ErrorResponse, "description": "Не репетитор или чужая бронь"},
        404: {"model": ErrorResponse, "description": "Бронь не найдена"},
        409: {"model": ErrorResponse, "description": "Уже оплачена или бронь отменена"},
        422: {"model": ErrorResponse},
    },
)
async def mark_paid(
    booking_id: int,
    body: MarkPaidIn,
    user: User = Depends(_tutor_only),
    session: AsyncSession = Depends(get_session),
) -> PaymentOut:
    # Блокируем бронь: параллельные отметки оплаты выстраиваются в очередь.
    row = (
        await session.execute(
            select(Booking, Slot.tutor_id)
            .join(Slot, Slot.id == Booking.slot_id)
            .where(Booking.id == booking_id)
            .with_for_update(of=Booking)
        )
    ).first()
    if row is None:
        raise AppError(404, "booking_not_found", "Booking not found")
    booking, tutor_id = row
    if tutor_id != user.id:
        raise AppError(403, "forbidden", "Not your booking")
    if booking.status == BookingStatus.cancelled:
        raise AppError(409, "booking_cancelled", "Cannot mark a cancelled booking as paid")

    payment = await session.scalar(select(Payment).where(Payment.booking_id == booking.id))
    if payment is not None and payment.status == PaymentStatus.paid:
        raise _already_paid()
    if payment is None:
        payment = Payment(booking_id=booking.id)
        session.add(payment)
    payment.amount = body.amount
    payment.status = PaymentStatus.paid
    payment.paid_at = datetime.now(UTC)
    payment.source = PaymentSource.manual
    try:
        await session.commit()
    except IntegrityError:  # unique(booking_id): страховка от гонки
        await session.rollback()
        raise _already_paid() from None
    await session.refresh(payment)
    return _out(payment)
