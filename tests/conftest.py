import os
import subprocess
import sys
from collections.abc import AsyncIterator
from urllib.parse import urlsplit, urlunsplit

# Тесты идут против настоящих PostgreSQL и Redis, но в отдельных БД.
_base = urlsplit(os.environ["DATABASE_URL"])
TEST_DB = "tutorbot_test"
os.environ["DATABASE_URL"] = urlunsplit(_base._replace(path=f"/{TEST_DB}"))
_redis = urlsplit(os.environ["REDIS_URL"])
os.environ["REDIS_URL"] = urlunsplit(_redis._replace(path="/1"))
ADMIN_DSN = urlunsplit(_base._replace(scheme="postgresql", path="/postgres"))

import asyncpg  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _database() -> None:
    import asyncio

    async def recreate() -> None:
        conn = await asyncpg.connect(ADMIN_DSN)
        await conn.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
        await conn.execute(f"CREATE DATABASE {TEST_DB}")
        await conn.close()

    asyncio.run(recreate())
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True)


@pytest_asyncio.fixture(autouse=True)
async def _clean() -> AsyncIterator[None]:
    from app.db import engine
    from app.redis import redis_client

    yield
    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            "TRUNCATE payments, bookings, slots, students, webhook_requests, users "
            "RESTART IDENTITY CASCADE"
        )
    await redis_client.flushdb()


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
def make_tutor():
    from app.db import SessionLocal
    from app.models import User, UserRole
    from app.security import hash_password

    async def _make(email: str = "tutor@example.com", password: str = "tutor-pass-1") -> User:
        async with SessionLocal() as s:
            u = User(email=email, password_hash=await hash_password(password), role=UserRole.tutor)
            s.add(u)
            await s.commit()
            return u

    return _make


@pytest.fixture
def login(client: AsyncClient):
    async def _login(email: str, password: str) -> dict[str, str]:
        r = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
        assert r.status_code == 200, r.text
        return {"Authorization": f"Bearer {r.json()['access_token']}"}

    return _login


@pytest.fixture
def make_student():
    """Ученик напрямую в БД + готовые заголовки (без медленного argon2 на каждого)."""
    from app.db import SessionLocal
    from app.models import Student, User, UserRole
    from app.security import create_token

    async def _make(n: int = 1) -> tuple[Student, dict[str, str]]:
        async with SessionLocal() as s:
            u = User(email=f"student{n}@example.com", password_hash="x", role=UserRole.student)
            s.add(u)
            await s.flush()
            st = Student(user_id=u.id, name=f"Student {n}")
            s.add(st)
            await s.commit()
            token = create_token(u.id, "student", "access")
            return st, {"Authorization": f"Bearer {token}"}

    return _make


@pytest.fixture
def make_slot():
    from datetime import UTC, datetime, timedelta

    from app.db import SessionLocal
    from app.models import Slot

    async def _make(tutor_id: int, hours_from_now: float = 24) -> Slot:
        start = datetime.now(UTC) + timedelta(hours=hours_from_now)
        async with SessionLocal() as s:
            slot = Slot(tutor_id=tutor_id, starts_at=start, ends_at=start + timedelta(hours=1))
            s.add(slot)
            await s.commit()
            return slot

    return _make


@pytest.fixture
def redis_down():
    from redis.asyncio import Redis

    from app.main import app
    from app.redis import get_redis

    dead = Redis.from_url("redis://127.0.0.1:1/0", decode_responses=True, socket_connect_timeout=1)
    app.dependency_overrides[get_redis] = lambda: dead
    yield
    app.dependency_overrides.pop(get_redis, None)
