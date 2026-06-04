"""Integration tests for auth flow (register + login)."""
import uuid

import pytest
from httpx import AsyncClient


def random_username() -> str:
    """Generate a unique username so tests don't collide."""
    return f"test_{uuid.uuid4().hex[:10]}"


@pytest.mark.asyncio
async def test_register_creates_user(client: AsyncClient):
    """POST /v1/users/ should create a new user and return 201."""
    username = random_username()

    response = await client.post(
        "/v1/users/",
        json={"username": username, "password": "testpass123"},
    )

    assert response.status_code ==201
    data = response.json()
    assert data["username"] == username
    assert data["is_active"] is True
    assert "id" in data
    assert "password" not in data


@pytest.mark.asyncio
async def test_login_returns_access_token(client: AsyncClient):
    """Login with correct credentials should return access_token."""
    username = random_username()
    password = "testpass123"

    register_response = await client.post(
        "/v1/users/",
        json={"username": username, "password": password },
    )
    assert register_response.status_code ==201

    login_response = await client.post(
        "/v1/auth/token",
        data={"username": username, "password": password},
    )

    assert login_response.status_code ==200
    data = login_response.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"
    assert isinstance(data["expires_in"], int)


@pytest.mark.asyncio
async def test_login_wrong_password_returns_401(client: AsyncClient):
    """Login with wrong password should return 401."""
    username = random_username()

    await client.post(
        "/v1/users/",
        json={"username": username, "password": "correctpass"},
    )

    response = await client.post(
        "/v1/auth/token",
        data={"username": username, "password": "wrongpass"},
    )

    assert response.status_code == 401
    