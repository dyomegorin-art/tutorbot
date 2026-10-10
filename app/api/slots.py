import logging
from datetime import UTC, date, datetime, time, timedelta

from fastapi import APIRouter, Depends, Query, Response
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import delete, exists, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_role
from app.cache import bump_version, get_version, slots_key
from app.config import get_settings
from app.db import get_session
from app.errors import AppError
from app.models import Booking, BookingStatus, Payment, PaymentStatus, Slot, User, UserRole
from app.redis import get_redis
from app.schemas.common import Page, Pagination
from app.schemas.errors import ErrorResponse
from app.schemas.slots import SlotCreate, SlotOut, SlotUpdate

log = logging.getLogger(__name__)

router = APIRouter(prefix="/slots", tags=["slots"])

_tutor_only = require_role(UserRole.tutor)

_has_active_booking = exists().where(
    Booking.slot_id == Slot.id, Booking.status == BookingStatus.active
)


def _day_start(d: date) -> datetime:
    return datetime.combine(d, time.min, tzinfo=UTC)


def _row_to_out(slot: Slot, is_free: bool) -> SlotOut:
    return SlotOut(
        id=slot.id,
        tutor_id=slot.tutor_id,
        starts_at=slot.starts_at,
        ends_at=slot.ends_at,
        is_free=is_free,
        created_at=slot.created_at,
        updated_at=slot.updated_at,
    )


def _is_overlap(err: IntegrityError) -> bool:
    return "ex_slots_no_overlap" in str(err.orig)


def _overlap_error() -> AppError:
    return AppError(409, "slot_overlap", "Slot overlaps with another slot of this tutor")


async def _load_owned(session: AsyncSession, slot_id: int, user: User) -> Slot:
    slot = await session.scalar(select(Slot).where(Slot.id == slot_id).with_for_update())
    if slot is None:
        raise AppError(404, "slot_not_found", "Slot not found")
    if slot.tutor_id != user.id:
        raise AppError(403, "forbidden", "Not your slot")
    return slot


async def _has_active(session: AsyncSession, slot_id: int) -> bool:
    return bool(
        await session.scalar(
            select(
                exists().where(Booking.slot_id == slot_id, Booking.status == BookingStatus.active)
            )
        )
    )


@router.get(
    "",
    response_model=Page[SlotOut],
    summary="Список слотов с фильтрами (кешируется, см. заголовок X-Cache)",
    responses={401: {"model": ErrorResponse}},
)
async def list_slots(
    date_: date | None = Query(None, alias="date", description="Один день (UTC)"),
    date_from: date | None = Query(None, description="С даты включительно (UTC)"),
    date_to: date | None = Query(None, description="По дату включительно (UTC)"),
    free: bool | None = Query(None, description="true — только свободные, false — только занятые"),
    tutor_id: int | None = None,
    page: Pagination = Depends(),
    _: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> Response:
    if date_ is not None and (date_from is not None or date_to is not None):
        raise AppError(422, "validation_error", "Use either date or date_from/date_to")
    if date_ is not None:
        date_from = date_to = date_

    # Версию читаем ДО запроса в БД: если расписание изменится в процессе, результат
    # осядет под уже устаревшим ключом и никто его не прочитает.
    key: str | None = None
    try:
        version = await get_version(redis, tutor_id)
        key = slots_key(
            version,
            tutor_id,
            date_from=date_from,
            date_to=date_to,
            free=free,
            limit=page.limit,
            offset=page.offset,
        )
        cached = await redis.get(key)
        if cached is not None:
            return Response(cached, media_type="application/json", headers={"X-Cache": "HIT"})
    except RedisError:
        log.warning("slots cache read failed")
        key = None

    conds = []
    if date_from is not None:
        conds.append(Slot.starts_at >= _day_start(date_from))
    if date_to is not None:
        conds.append(Slot.starts_at < _day_start(date_to) + timedelta(days=1))
    if tutor_id is not None:
        conds.append(Slot.tutor_id == tutor_id)
    if free is True:
        conds.append(~_has_active_booking)
    elif free is False:
        conds.append(_has_active_booking)

    total = await session.scalar(select(func.count()).select_from(Slot).where(*conds)) or 0
    rows = await session.execute(
        select(Slot, ~_has_active_booking)
        .where(*conds)
        .order_by(Slot.starts_at, Slot.id)
        .limit(page.limit)
        .offset(page.offset)
    )
    payload = Page[SlotOut](
        items=[_row_to_out(s, f) for s, f in rows],
        total=total,
        limit=page.limit,
        offset=page.offset,
    ).model_dump_json()
    if key is not None:
        try:
            await redis.set(key, payload, ex=get_settings().slots_cache_ttl_seconds)
        except RedisError:
            log.warning("slots cache write failed")
    return Response(payload, media_type="application/json", headers={"X-Cache": "MISS"})


@router.get(
    "/{slot_id}",
    response_model=SlotOut,
    summary="Один слот",
    responses={404: {"model": ErrorResponse}, 401: {"model": ErrorResponse}},
)
async def get_slot(
    slot_id: int,
    _: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> SlotOut:
    row = (
        await session.execute(select(Slot, ~_has_active_booking).where(Slot.id == slot_id))
    ).first()
    if row is None:
        raise AppError(404, "slot_not_found", "Slot not found")
    return _row_to_out(*row)


@router.post(
    "",
    status_code=201,
    response_model=SlotOut,
    summary="Создать слот (tutor)",
    responses={
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        409: {"model": ErrorResponse, "description": "Пересечение со слотом репетитора"},
    },
)
async def create_slot(
    body: SlotCreate,
    user: User = Depends(_tutor_only),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> SlotOut:
    slot = Slot(tutor_id=user.id, starts_at=body.starts_at, ends_at=body.ends_at)
    session.add(slot)
    try:
        await session.commit()
    except IntegrityError as e:
        await session.rollback()
        if _is_overlap(e):
            raise _overlap_error() from None
        raise
    await bump_version(redis, user.id)
    await session.refresh(slot)
    return _row_to_out(slot, True)


@router.patch(
    "/{slot_id}",
    response_model=SlotOut,
    summary="Изменить слот (tutor-владелец)",
    responses={
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse, "description": "Пересечение или у слота есть активная бронь"},
    },
)
async def update_slot(
    slot_id: int,
    body: SlotUpdate,
    user: User = Depends(_tutor_only),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> SlotOut:
    slot = await _load_owned(session, slot_id, user)
    if await _has_active(session, slot.id):
        raise AppError(409, "slot_has_active_booking", "Slot has an active booking")
    starts_at = body.starts_at or slot.starts_at
    ends_at = body.ends_at or slot.ends_at
    if ends_at <= starts_at:
        raise AppError(422, "validation_error", "ends_at must be later than starts_at")
    slot.starts_at, slot.ends_at = starts_at, ends_at
    try:
        await session.commit()
    except IntegrityError as e:
        await session.rollback()
        if _is_overlap(e):
            raise _overlap_error() from None
        raise
    await bump_version(redis, user.id)
    await session.refresh(slot)
    return _row_to_out(slot, True)


@router.delete(
    "/{slot_id}",
    status_code=204,
    summary="Удалить слот (tutor-владелец)",
    responses={
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse, "description": "Есть активная бронь или оплаты"},
    },
)
async def delete_slot(
    slot_id: int,
    user: User = Depends(_tutor_only),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> Response:
    slot = await _load_owned(session, slot_id, user)
    if await _has_active(session, slot.id):
        raise AppError(409, "slot_has_active_booking", "Slot has an active booking")
    paid = await session.scalar(
        select(
            exists().where(
                Payment.booking_id == Booking.id,
                Booking.slot_id == slot.id,
                Payment.status == PaymentStatus.paid,
            )
        )
    )
    if paid:
        raise AppError(409, "slot_has_payments", "Slot has paid bookings and cannot be deleted")
    # Остались только отменённые брони без оплат — они уходят вместе со слотом.
    booking_ids = select(Booking.id).where(Booking.slot_id == slot.id)
    await session.execute(delete(Payment).where(Payment.booking_id.in_(booking_ids)))
    await session.execute(delete(Booking).where(Booking.slot_id == slot.id))
    await session.delete(slot)
    await session.commit()
    await bump_version(redis, user.id)
    return Response(status_code=204)
