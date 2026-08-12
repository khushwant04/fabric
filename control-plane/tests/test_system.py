"""System endpoints, migration/model parity, and API document parity."""

from __future__ import annotations

import json
import pathlib

import sqlalchemy as sa
from httpx import AsyncClient

from app.core.database import Base
from app.main import create_app
from tests.conftest import PROJECT_ROOT, TEST_DB_PATH


async def test_health_and_readiness(client: AsyncClient) -> None:
    assert (await client.get("/healthz")).json() == {"status": "ok"}
    assert (await client.get("/readyz")).json() == {"status": "ready"}


async def test_jwks_publishes_rsa_signing_key(client: AsyncClient) -> None:
    response = await client.get("/.well-known/jwks.json")
    assert response.status_code == 200
    keys = response.json()["keys"]
    assert keys, "at least one signing key must be published"
    entry = keys[0]
    assert entry["kty"] == "RSA"
    assert entry["alg"] == "RS256"
    assert entry["use"] == "sig"
    assert entry["kid"]
    # Private material must never be published.
    assert "d" not in entry and "p" not in entry


def test_migration_matches_model_metadata() -> None:
    """Every model table and column must exist in the migrated schema."""
    assert pathlib.Path(TEST_DB_PATH).exists(), "migrated test database is missing"
    engine = sa.create_engine(f"sqlite:///{TEST_DB_PATH}")
    try:
        inspector = sa.inspect(engine)
        actual_tables = set(inspector.get_table_names()) - {"alembic_version"}
        expected_tables = set(Base.metadata.tables)
        assert expected_tables == actual_tables

        for table_name, table in Base.metadata.tables.items():
            actual_columns = {column["name"] for column in inspector.get_columns(table_name)}
            expected_columns = {column.name for column in table.columns}
            assert expected_columns == actual_columns, table_name
    finally:
        engine.dispose()


def test_committed_openapi_document_is_current() -> None:
    """The frontend generates its client from openapi.json, so it must not drift."""
    committed = json.loads((PROJECT_ROOT / "openapi.json").read_text())
    assert create_app().openapi() == committed, (
        "openapi.json is stale; regenerate it after changing the API"
    )
