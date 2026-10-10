from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_student, get_current_user
from app.cache import bump_version
from app.config import get_settings
from app.db import get_session
from app.locks import slot_lock
from app.models import Booking, BookingStatus, Slot, Student, User, UserRole
from app.redis import get_redis
from app.schemas.bookings import BookingCreate, BookingOut
from app.schemas.common import Page, Pagination
from app.schemas.errors import ErrorResponse
from app.services import bookings as service

router = APIRouter(prefix="/bookings", tags=["bookings"])

_auth_errors: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse, "description": "Нет или невалидный токен"}
}


def _out(booking: Booking, slot: Slot) -> BookingOut:
    return BookingOut(
        id=booking.id,
        slot_id=booking.slot_id,
        student_id=booking.student_id,
        tutor_id=slot.tutor_id,
        status=booking.status.value,
        starts_at=slot.starts_at,
        ends_at=slot.ends_at,
        created_at=booking.created_at,
        cancelled_at=booking.cancelled_at,
    )


@router.post(
    "",
    status_code=201,
    response_model=BookingOut,
    summary="Забронировать слот (student)",
    responses={
        **_auth_errors,
        403: {"model": ErrorResponse, "description": "Не ученик"},
        404: {"model": ErrorResponse, "description": "Слот не найден"},
        409: {"model": ErrorResponse, "description": "Слот уже занят"},
        422: {"model": ErrorResponse, "description": "Слот в прошлом / неверное тело"},
    },
)
async def create_booking(
    body: BookingCreate,
    student: Student = Depends(get_current_student),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> BookingOut:
    async with slot_lock(redis, body.slot_id, get_settings().booking_lock_ttl_seconds):
        booking = await service.create_booking(session, body.slot_id, student)
    slot = await session.get(Slot, booking.slot_id)
    assert slot is not None
    await bump_version(redis, slot.tutor_id)
    return _out(booking, slot)


@router.get(
    "",
    response_model=Page[BookingOut],
    summary="Свои брони (ученик) или брони на свои слоты (репетитор)",
    responses=_auth_errors,
)
async def list_bookings(
    status: Literal["active", "cancelled"] | None = Query(None),
    page: Pagination = Depends(),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Page[BookingOut]:
    conds = []
    if user.role == UserRole.student:
        conds.append(Student.user_id == user.id)
    else:
        conds.append(Slot.tutor_id == user.id)
    if status is not None:
        conds.append(Booking.status == BookingStatus(status))
    base = (
        select(Booking, Slot)
        .join(Slot, Slot.id == Booking.slot_id)
        .join(Student, Student.id == Booking.student_id)
        .where(*conds)
    )
    total = await session.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = await session.execute(
        base.order_by(Slot.starts_at.desc(), Booking.id.desc())
        .limit(page.limit)
        .offset(page.offset)
    )
    return Page[BookingOut](
        items=[_out(b, s) for b, s in rows], total=total, limit=page.limit, offset=page.offset
    )


@router.get(
    "/{booking_id}",
    response_model=BookingOut,
    summary="Одна бронь (участник)",
    responses={
        **_auth_errors,
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
    },
)
async def get_booking(
    booking_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> BookingOut:
    booking, slot = await service.get_participant_booking(session, booking_id, user)
    return _out(booking, slot)


@router.post(
    "/{booking_id}/cancel",
    response_model=BookingOut,
    summary="Отменить бронь (участник)",
    responses={
        **_auth_errors,
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse, "description": "Уже отменена"},
    },
)
async def cancel_booking(
    booking_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> BookingOut:
    booking, slot = await service.cancel_booking(session, booking_id, user)
    await bump_version(redis, slot.tutor_id)
    return _out(booking, slot)
