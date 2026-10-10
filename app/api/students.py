from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_student, get_current_user, require_role
from app.db import get_session
from app.errors import AppError
from app.models import Booking, Slot, Student, User, UserRole
from app.schemas.common import Page, Pagination
from app.schemas.errors import ErrorResponse
from app.schemas.students import StudentOut, StudentUpdate

router = APIRouter(prefix="/students", tags=["students"])

_tutor_only = require_role(UserRole.tutor)
_errors: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse, "description": "Нет или невалидный токен"},
    403: {"model": ErrorResponse},
}


def _out(student: Student, email: str) -> StudentOut:
    return StudentOut(
        id=student.id,
        user_id=student.user_id,
        email=email,
        name=student.name,
        phone=student.phone,
        telegram_id=student.telegram_id,
    )


def _booked_with(tutor_id: int) -> Any:
    """Ученик записан к репетитору, если у него есть бронь на слот этого репетитора."""
    return exists().where(
        Booking.student_id == Student.id,
        Slot.id == Booking.slot_id,
        Slot.tutor_id == tutor_id,
    )


@router.get(
    "",
    response_model=Page[StudentOut],
    summary="Ученики, записанные к репетитору (tutor)",
    responses=_errors,
)
async def list_students(
    page: Pagination = Depends(),
    tutor: User = Depends(_tutor_only),
    session: AsyncSession = Depends(get_session),
) -> Page[StudentOut]:
    cond = _booked_with(tutor.id)
    total = await session.scalar(select(func.count()).select_from(Student).where(cond)) or 0
    rows = await session.execute(
        select(Student, User.email)
        .join(User, User.id == Student.user_id)
        .where(cond)
        .order_by(Student.id)
        .limit(page.limit)
        .offset(page.offset)
    )
    return Page[StudentOut](
        items=[_out(s, e) for s, e in rows], total=total, limit=page.limit, offset=page.offset
    )


@router.get("/me", response_model=StudentOut, summary="Свой профиль (student)", responses=_errors)
async def get_me(
    student: Student = Depends(get_current_student),
    user: User = Depends(get_current_user),
) -> StudentOut:
    return _out(student, user.email)


@router.patch(
    "/me",
    response_model=StudentOut,
    summary="Изменить свой профиль (student)",
    responses={**_errors, 422: {"model": ErrorResponse}},
)
async def update_me(
    body: StudentUpdate,
    student: Student = Depends(get_current_student),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> StudentOut:
    for field in body.model_fields_set:
        setattr(student, field, getattr(body, field))
    session.add(student)
    await session.commit()
    return _out(student, user.email)


@router.get(
    "/{student_id}",
    response_model=StudentOut,
    summary="Карточка ученика (tutor, если ученик записан к нему; или сам ученик)",
    responses={**_errors, 404: {"model": ErrorResponse}},
)
async def get_student(
    student_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> StudentOut:
    row = (
        await session.execute(
            select(Student, User.email)
            .join(User, User.id == Student.user_id)
            .where(Student.id == student_id)
        )
    ).first()
    if row is None:
        raise AppError(404, "student_not_found", "Student not found")
    student, email = row
    if user.role == UserRole.student:
        allowed = student.user_id == user.id
    else:
        allowed = bool(
            await session.scalar(select(_booked_with(user.id).where(Student.id == student_id)))
        )
    if not allowed:
        raise AppError(403, "forbidden", "Access to this student is not allowed")
    return _out(student, email)
