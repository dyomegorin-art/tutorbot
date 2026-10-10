import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from app.config import get_settings

_hasher = PasswordHasher()
TokenType = Literal["access", "refresh"]
ALGORITHM = "HS256"


async def hash_password(password: str) -> str:
    return await asyncio.to_thread(_hasher.hash, password)


async def verify_password(password: str, password_hash: str) -> bool:
    try:
        return await asyncio.to_thread(_hasher.verify, password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def create_token(user_id: int, role: str, token_type: TokenType) -> str:
    s = get_settings()
    ttl = (
        timedelta(minutes=s.jwt_access_ttl_minutes)
        if token_type == "access"
        else timedelta(days=s.jwt_refresh_ttl_days)
    )
    now = datetime.now(UTC)
    payload = {"sub": str(user_id), "role": role, "type": token_type, "iat": now, "exp": now + ttl}
    return jwt.encode(payload, s.jwt_secret, algorithm=ALGORITHM)


def decode_token(token: str, expected_type: TokenType) -> dict[str, Any]:
    """Бросает jwt.PyJWTError, если токен невалиден, просрочен или другого типа."""
    payload: dict[str, Any] = jwt.decode(
        token,
        get_settings().jwt_secret,
        algorithms=[ALGORITHM],
        options={"require": ["exp", "sub"]},
    )
    if payload.get("type") != expected_type:
        raise jwt.InvalidTokenError("wrong token type")
    return payload
