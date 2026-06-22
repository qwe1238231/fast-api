"""Smoke test that the app boots and responds."""
import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_root_redirects_to_docs(client: AsyncClient):
    """GET / should redirect to /docs."""
    response = await client.get("/")
    assert response.status_code ==307
    assert response.headers["location"] == "/docs"