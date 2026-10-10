import hashlib
import hmac
import json
import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_role
from app.config import get_settings
from app.db import get_session
from app.errors import AppError
from app.models import User, UserRole, WebhookRequest, WebhookStatus
from app.ratelimit import check_rate_limit
from app.redis import get_redis
from app.schemas.common import Page, Pagination
from app.schemas.errors import ErrorResponse
from app.schemas.webhooks import (
    WebhookAccepted,
    WebhookDuplicate,
    WebhookRequestOut,
    WebhookRequestUpdate,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

IDEMPOTENCY_TTL = 86400


def sign(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _duplicate(external_id: str) -> JSONResponse:
    return JSONResponse(WebhookDuplicate(external_id=external_id).model_dump(), status_code=200)


@router.post(
    "/requests",
    status_code=202,
    response_model=WebhookAccepted,
    summary="Приём заявки от внешней системы",
    description=(
        "Тело — JSON-объект с обязательным строковым `external_id`. Заголовок `X-Signature` — "
        "hex HMAC-SHA256 от сырого тела запроса по общему секрету. Повторная доставка с тем же "
        "`external_id` возвращает 200 и ничего не создаёт."
    ),
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "required": ["external_id"],
                        "properties": {"external_id": {"type": "string"}},
                        "additionalProperties": True,
                    }
                }
            },
        }
    },
    responses={
        200: {"model": WebhookDuplicate, "description": "Дубль: заявка уже принята"},
        401: {"model": ErrorResponse, "description": "Неверная или отсутствующая подпись"},
        422: {"model": ErrorResponse, "description": "Нет external_id / не JSON-объект"},
        429: {"model": ErrorResponse, "description": "Слишком много запросов"},
    },
)
async def receive_request(
    request: Request,
    x_signature: str | None = Header(None),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> JSONResponse:
    settings = get_settings()
    ip = request.client.host if request.client else "unknown"
    # Лимит до проверки подписи, чтобы флуд с неверной подписью тоже отсекался.
    await check_rate_limit(redis, "webhook", ip, settings.rate_limit_webhook_per_minute)

    body = await request.body()
    expected = sign(body, settings.webhook_secret)
    if x_signature is None or not hmac.compare_digest(x_signature.strip().lower(), expected):
        raise AppError(401, "invalid_signature", "Invalid or missing signature")

    try:
        payload: Any = json.loads(body)
    except ValueError:
        payload = None
    external_id = payload.get("external_id") if isinstance(payload, dict) else None
    if not isinstance(external_id, str) or not external_id.strip() or len(external_id) > 255:
        raise AppError(422, "validation_error", "Body must be a JSON object with external_id")

    # Быстрая идемпотентность в Redis; источник истины — unique(external_id) в БД.
    key = f"webhook:{external_id}"
    redis_key_set = False
    try:
        if not await redis.set(key, 1, nx=True, ex=IDEMPOTENCY_TTL):
            return _duplicate(external_id)
        redis_key_set = True
    except RedisError:
        log.warning("webhook idempotency check skipped, redis unavailable")

    row = WebhookRequest(external_id=external_id, payload=payload)
    session.add(row)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return _duplicate(external_id)  # ключ в Redis истёк или потерян, но заявка уже есть
    except Exception:
        await session.rollback()
        if redis_key_set:  # заявка не сохранена: пусть повторная доставка сможет пройти
            try:
                await redis.delete(key)
            except RedisError:
                pass
        raise
    return JSONResponse(
        WebhookAccepted(id=row.id, external_id=external_id).model_dump(), status_code=202
    )


_tutor_only = require_role(UserRole.tutor)
_tutor_errors: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse, "description": "Нет или невалидный токен"},
    403: {"model": ErrorResponse, "description": "Не репетитор"},
}


def _out(row: WebhookRequest) -> WebhookRequestOut:
    return WebhookRequestOut(
        id=row.id,
        external_id=row.external_id,
        payload=row.payload,
        status=row.status.value,
        created_at=row.created_at,
    )


@router.get(
    "/requests",
    response_model=Page[WebhookRequestOut],
    summary="Заявки, пришедшие через вебхук (tutor)",
    responses=_tutor_errors,
)
async def list_requests(
    status: Literal["new", "processed", "rejected"] | None = Query(None),
    page: Pagination = Depends(),
    _: User = Depends(_tutor_only),
    session: AsyncSession = Depends(get_session),
) -> Page[WebhookRequestOut]:
    base = select(WebhookRequest)
    if status is not None:
        base = base.where(WebhookRequest.status == WebhookStatus(status))
    total = await session.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = await session.scalars(
        base.order_by(WebhookRequest.id.desc()).limit(page.limit).offset(page.offset)
    )
    return Page[WebhookRequestOut](
        items=[_out(r) for r in rows], total=total, limit=page.limit, offset=page.offset
    )


@router.get(
    "/requests/{request_id}",
    response_model=WebhookRequestOut,
    summary="Одна заявка (tutor)",
    responses={**_tutor_errors, 404: {"model": ErrorResponse}},
)
async def get_request(
    request_id: int,
    _: User = Depends(_tutor_only),
    session: AsyncSession = Depends(get_session),
) -> WebhookRequestOut:
    row = await session.get(WebhookRequest, request_id)
    if row is None:
        raise AppError(404, "webhook_request_not_found", "Request not found")
    return _out(row)


@router.patch(
    "/requests/{request_id}",
    response_model=WebhookRequestOut,
    summary="Обработать заявку: processed или rejected (tutor)",
    responses={
        **_tutor_errors,
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse, "description": "Заявка уже обработана"},
    },
)
async def update_request(
    request_id: int,
    body: WebhookRequestUpdate,
    _: User = Depends(_tutor_only),
    session: AsyncSession = Depends(get_session),
) -> WebhookRequestOut:
    # Атомарный переход new → processed/rejected: гонка двух репетиторов невозможна.
    updated = await session.scalar(
        update(WebhookRequest)
        .where(WebhookRequest.id == request_id, WebhookRequest.status == WebhookStatus.new)
        .values(status=WebhookStatus(body.status))
        .returning(WebhookRequest)
    )
    if updated is None:
        exists_ = await session.get(WebhookRequest, request_id)
        if exists_ is None:
            raise AppError(404, "webhook_request_not_found", "Request not found")
        raise AppError(409, "request_already_handled", "Request has already been handled")
    await session.commit()
    return _out(updated)
