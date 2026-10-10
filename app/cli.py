"""Создание репетитора (регистрация через API создаёт только учеников).

python -m app.cli create-tutor EMAIL PASSWORD
"""

import asyncio
import sys

from app.db import SessionLocal, engine
from app.models import User, UserRole
from app.security import hash_password


async def create_tutor(email: str, password: str) -> None:
    async with SessionLocal() as session:
        session.add(
            User(
                email=email.lower(),
                password_hash=await hash_password(password),
                role=UserRole.tutor,
            )
        )
        await session.commit()
    await engine.dispose()


if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[1] != "create-tutor":
        sys.exit(__doc__)
    asyncio.run(create_tutor(sys.argv[2], sys.argv[3]))
