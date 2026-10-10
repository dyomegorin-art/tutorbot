import jwt
from fastapi import APIRouter, Depends, Request
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.errors import AppError
from app.models import Student, User, UserRole
from app.ratelimit import check_rate_limit
from app.redis import get_redis
from app.schemas.auth import AccessToken, LoginIn, RefreshIn, RegisterIn, RegisterOut, TokenPair
from app.schemas.errors import ErrorResponse
from app.security import create_token, decode_token, hash_password, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/register",
    status_code=201,
    response_model=RegisterOut,
    summary="Регистрация ученика",
    responses={409: {"model": ErrorResponse, "description": "Email уже занят"}},
)
async def register(body: RegisterIn, session: AsyncSession = Depends(get_session)) -> RegisterOut:
    email = body.email.lower()
    exists = await session.scalar(select(User.id).where(User.email == email))
    if exists is not None:
        raise AppError(409, "email_taken", "Email is already registered")
    user = User(
        email=email, password_hash=await hash_password(body.password), role=UserRole.student
    )
    session.add(user)
    try:
        await session.flush()
        session.add(Student(user_id=user.id, name=body.name, phone=body.phone))
        await session.commit()
    except IntegrityError:  # гонка двух регистраций с одним email
        await session.rollback()
        raise AppError(409, "email_taken", "Email is already registered") from None
    return RegisterOut(id=user.id, email=user.email, role=user.role.value)


@router.post(
    "/login",
    response_model=TokenPair,
    summary="Получить пару токенов",
    responses={
        401: {"model": ErrorResponse, "description": "Неверный email или пароль"},
        429: {"model": ErrorResponse, "description": "Слишком много попыток"},
    },
)
async def login(
    body: LoginIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> TokenPair:
    ip = request.client.host if request.client else "unknown"
    await check_rate_limit(
        redis,
        "login",
        f"{ip}:{body.email.lower()}",
        get_settings().rate_limit_login_per_minute,
    )
    user = await session.scalar(select(User).where(User.email == body.email.lower()))
    if user is None or not await verify_password(body.password, user.password_hash):
        raise AppError(401, "invalid_credentials", "Invalid email or password")
    return TokenPair(
        access_token=create_token(user.id, user.role.value, "access"),
        refresh_token=create_token(user.id, user.role.value, "refresh"),
    )


@router.post(
    "/refresh",
    response_model=AccessToken,
    summary="Обновить access-токен",
    responses={401: {"model": ErrorResponse, "description": "Невалидный refresh-токен"}},
)
async def refresh(body: RefreshIn, session: AsyncSession = Depends(get_session)) -> AccessToken:
    try:
        payload = decode_token(body.refresh_token, "refresh")
        user_id = int(payload["sub"])
    except (jwt.PyJWTError, ValueError):
        raise AppError(401, "invalid_token", "Invalid or expired refresh token") from None
    user = await session.get(User, user_id)
    if user is None:
        raise AppError(401, "invalid_token", "Invalid or expired refresh token")
    return AccessToken(access_token=create_token(user.id, user.role.value, "access"))
