from collections.abc import Callable

import jwt
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.errors import AppError
from app.models import Student, User, UserRole
from app.security import decode_token

_bearer = HTTPBearer(auto_error=False)


def _unauthorized(message: str = "Not authenticated") -> AppError:
    return AppError(401, "unauthorized", message, {"WWW-Authenticate": "Bearer"})


async def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: AsyncSession = Depends(get_session),
) -> User:
    if creds is None:
        raise _unauthorized()
    try:
        payload = decode_token(creds.credentials, "access")
        user_id = int(payload["sub"])
    except (jwt.PyJWTError, ValueError):
        raise _unauthorized("Invalid or expired token") from None
    user = await session.get(User, user_id)
    if user is None:
        raise _unauthorized("User not found")
    return user


def require_role(*roles: UserRole) -> Callable[[User], User]:
    def checker(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise AppError(403, "forbidden", "Insufficient permissions")
        return user

    return checker


_student_only = require_role(UserRole.student)


async def get_current_student(
    user: User = Depends(_student_only),
    session: AsyncSession = Depends(get_session),
) -> Student:
    student = await session.scalar(select(Student).where(Student.user_id == user.id))
    if student is None:
        raise AppError(403, "forbidden", "Student profile not found")
    return student
