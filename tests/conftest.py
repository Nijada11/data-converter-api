import pytest
from fastapi.testclient import TestClient

from src.config import Settings
from src.main import create_app

API_KEY = "test-key"
AUTH = {"X-API-Key": API_KEY}


@pytest.fixture
def make_client():
    """Build a client on a fresh app, so rate-limit counters never leak between tests."""

    def build(**overrides) -> TestClient:
        settings = {"api_key": API_KEY, "rate_limit": "1000/minute", **overrides}
        return TestClient(create_app(Settings(**settings)), raise_server_exceptions=False)

    return build


@pytest.fixture
def client(make_client) -> TestClient:
    return make_client()
