from datetime import UTC, datetime, timedelta

import jwt
from fastapi import Depends
from httpx import AsyncClient

from app.api.deps import require_role
from app.config import get_settings
from app.main import app
from app.models import User, UserRole

REG = {"email": "a@example.com", "password": "password-1", "name": "Anna"}


_tutor = require_role(UserRole.tutor)


@app.get("/_test/tutor-only")
async def _tutor_only(user: User = Depends(_tutor)) -> dict[str, int]:
    return {"id": user.id}


async def test_register_ok_and_duplicate(client: AsyncClient) -> None:
    r = await client.post("/api/v1/auth/register", json=REG)
    assert r.status_code == 201
    assert r.json()["role"] == "student"
    r = await client.post("/api/v1/auth/register", json={**REG, "email": "A@Example.com"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "email_taken"


async def test_register_validation(client: AsyncClient) -> None:
    r = await client.post("/api/v1/auth/register", json={**REG, "password": "short"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"


async def test_login_and_refresh(client: AsyncClient) -> None:
    await client.post("/api/v1/auth/register", json=REG)
    r = await client.post("/api/v1/auth/login", json={"email": REG["email"], "password": "bad"})
    assert r.status_code == 401
    r = await client.post(
        "/api/v1/auth/login", json={"email": REG["email"], "password": REG["password"]}
    )
    assert r.status_code == 200
    tokens = r.json()
    r = await client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert r.status_code == 200 and r.json()["access_token"]
    # access-токен нельзя использовать как refresh
    r = await client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["access_token"]})
    assert r.status_code == 401


async def test_no_or_bad_token_is_401(client: AsyncClient) -> None:
    assert (await client.get("/_test/tutor-only")).status_code == 401
    r = await client.get("/_test/tutor-only", headers={"Authorization": "Bearer garbage"})
    assert r.status_code == 401
    expired = jwt.encode(
        {"sub": "1", "type": "access", "exp": datetime.now(UTC) - timedelta(minutes=1)},
        get_settings().jwt_secret,
        algorithm="HS256",
    )
    r = await client.get("/_test/tutor-only", headers={"Authorization": f"Bearer {expired}"})
    assert r.status_code == 401


async def test_roles(client: AsyncClient, make_tutor, login) -> None:
    await client.post("/api/v1/auth/register", json=REG)
    student = await login(REG["email"], REG["password"])
    assert (await client.get("/_test/tutor-only", headers=student)).status_code == 403
    tutor = await make_tutor()
    headers = await login(tutor.email, "tutor-pass-1")
    assert (await client.get("/_test/tutor-only", headers=headers)).status_code == 200
