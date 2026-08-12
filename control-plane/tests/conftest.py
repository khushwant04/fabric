"""Shared test fixtures.

Tests run against a migrated SQLite database so the Alembic history is exercised
on every run. Auth0 is replaced by a deterministic verifier; bearer tokens of the
form ``user:<subject>`` represent an authenticated human.
"""

from __future__ import annotations

import os
import pathlib
import tempfile
from collections.abc import AsyncIterator, Iterator

import pytest

TEST_DB_PATH = pathlib.Path(tempfile.gettempdir()) / "fabric_control_plane_test.db"

os.environ["FABRIC_APP_ENV"] = "test"
os.environ["FABRIC_DATABASE_URL"] = f"sqlite+aiosqlite:///{TEST_DB_PATH}"
os.environ["FABRIC_AUTH0_ISSUER"] = "https://fabric.jp.auth0.com/"
os.environ["FABRIC_AUTH0_AUDIENCE"] = "https://api.fabric.test/control"
os.environ["FABRIC_JWT_ISSUER"] = "https://control.fabric.test"
os.environ["FABRIC_CREDENTIAL_PEPPER"] = "test-pepper-value"
os.environ["FABRIC_SYSTEM_ACCOUNT_SLUG"] = "fabric-system"

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import delete  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.core.auth0 import Auth0Identity, get_auth0_verifier  # noqa: E402
from app.core.database import Base, dispose_engine, get_session_factory  # noqa: E402
from app.core.errors import Unauthorized  # noqa: E402
from app.main import create_app  # noqa: E402

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]


class FakeAuth0Verifier:
    """Deterministic stand-in for the Auth0 verifier."""

    async def verify(self, token: str) -> Auth0Identity:
        if not token.startswith("user:"):
            raise Unauthorized("invalid_assertion", "Auth0 token is not valid")
        subject = token.split(":", 1)[1]
        if not subject:
            raise Unauthorized("invalid_assertion", "Auth0 token is missing a subject")
        return Auth0Identity(
            subject=subject,
            email=f"{subject}@example.test",
            name=subject,
            claims={"sub": subject},
        )


@pytest.fixture(scope="session", autouse=True)
def migrated_database() -> Iterator[None]:
    """Apply the Alembic migration history once per test session."""
    if TEST_DB_PATH.exists():
        TEST_DB_PATH.unlink()
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{TEST_DB_PATH}")
    command.upgrade(config, "head")
    yield
    if TEST_DB_PATH.exists():
        TEST_DB_PATH.unlink()


@pytest.fixture(autouse=True)
async def clean_tables() -> AsyncIterator[None]:
    """Remove all rows between tests without dropping the migrated schema."""
    yield
    factory = get_session_factory()
    async with factory() as session:
        for table in reversed(Base.metadata.sorted_tables):
            await session.execute(delete(table))
        await session.commit()


@pytest.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    """Direct database access for test setup that bypasses the HTTP API."""
    factory = get_session_factory()
    async with factory() as session:
        yield session


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """HTTP client bound to the ASGI app with Auth0 replaced."""
    app = create_app()
    app.dependency_overrides[get_auth0_verifier] = lambda: FakeAuth0Verifier()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://control.test") as http_client:
        yield http_client
    app.dependency_overrides.clear()


@pytest.fixture(scope="session", autouse=True)
def dispose_engine_at_end() -> Iterator[None]:
    """Release database connections when the session ends."""
    yield
    import asyncio

    asyncio.run(dispose_engine())
