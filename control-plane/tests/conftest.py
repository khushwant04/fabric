"""Shared test fixtures.

Tests run against a migrated database so the Alembic history is exercised on every
run. SQLite is the default because it needs no service. Setting
``FABRIC_TEST_DATABASE_URL`` to a PostgreSQL URL runs the same suite against the
engine production uses, which is the only way to exercise behaviour SQLite cannot
represent: row-level security, real savepoint semantics after a constraint
violation, and timezone-aware timestamps.

    FABRIC_TEST_DATABASE_URL=postgresql+asyncpg://user:pass@host/db?ssl=require \
        .venv/bin/python -m pytest -q

The schema is rebuilt at session start, so the target database must be one whose
contents can be discarded.

Auth0 is replaced by a deterministic verifier; bearer tokens of the form
``user:<subject>`` represent an authenticated human.
"""

from __future__ import annotations

import os
import pathlib
import tempfile
from collections.abc import AsyncIterator, Iterator

import pytest

TEST_DB_PATH = pathlib.Path(tempfile.gettempdir()) / "fabric_control_plane_test.db"

#: A PostgreSQL URL here runs the suite against the production engine instead.
TEST_DATABASE_URL = os.environ.get(
    "FABRIC_TEST_DATABASE_URL", f"sqlite+aiosqlite:///{TEST_DB_PATH}"
)
USING_POSTGRES = TEST_DATABASE_URL.startswith("postgresql")

os.environ["FABRIC_APP_ENV"] = "test"
os.environ["FABRIC_DATABASE_URL"] = TEST_DATABASE_URL
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
from app.core.tenancy import reset_context, system_context  # noqa: E402
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
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)

    if USING_POSTGRES:
        # A shared database may hold a schema from an earlier run, and migrating
        # onto it would test nothing. Rebuilding also exercises the downgrade path,
        # which the file-based SQLite run never does because it deletes the file.
        command.downgrade(config, "base")
        command.upgrade(config, "head")
        yield
        return

    if TEST_DB_PATH.exists():
        TEST_DB_PATH.unlink()
    command.upgrade(config, "head")
    yield
    if TEST_DB_PATH.exists():
        TEST_DB_PATH.unlink()


@pytest.fixture(autouse=True)
def reset_tenant_context() -> Iterator[None]:
    """Clear any tenant context a previous test left behind.

    The ASGI transport runs the application in the test's own task, so a context
    variable a request declared is still visible to the test afterwards. Production
    is unaffected, where each request is its own task, but a test that inherited the
    previous request's account would assert isolation while running as a tenant.
    """
    reset_context()
    yield
    reset_context()


@pytest.fixture(autouse=True)
async def clean_tables() -> AsyncIterator[None]:
    """Remove all rows between tests without dropping the migrated schema."""
    yield
    factory = get_session_factory()
    async with factory() as session:
        # Cleanup spans every account, and row-level security would otherwise delete
        # nothing at all and leave tests inheriting each other's rows.
        with system_context():
            for table in reversed(Base.metadata.sorted_tables):
                await session.execute(delete(table))
            await session.commit()

    if USING_POSTGRES:
        # Each test runs in its own event loop, and asyncpg connections are bound
        # to the loop that opened them. A pooled connection reused by the next test
        # fails with "attached to a different loop", so the pool is released here.
        # SQLite is unaffected because aiosqlite proxies through a thread.
        await dispose_engine()


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
