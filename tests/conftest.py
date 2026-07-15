import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.core.config import AppConfig, PoolEntry, PriceEntry, ProviderConfig
from app.main import create_app

VALID_KEY = "test-key-123"


@pytest.fixture
def test_config(tmp_path) -> AppConfig:
    return AppConfig(
        api_key=VALID_KEY,
        db_path=str(tmp_path / "g.db"),
        default_pool="default",
        providers={
            "deepseek": ProviderConfig(base_url="https://api.deepseek.com/v1", api_key="sk-ds"),
            "anthropic": ProviderConfig(base_url="https://api.anthropic.com/v1", api_key="sk-an"),
        },
        pools={
            "cheap": [PoolEntry(provider="deepseek", model="deepseek-chat")],
            "default": [PoolEntry(provider="anthropic", model="claude-sonnet-5")],
            "large-context": [PoolEntry(provider="anthropic", model="claude-sonnet-5")],
        },
        prices={},
    )


@pytest_asyncio.fixture
async def app(test_config):
    application = create_app(test_config)
    async with application.router.lifespan_context(application):
        yield application


@pytest_asyncio.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def auth_headers():
    return {"Authorization": f"Bearer {VALID_KEY}"}
