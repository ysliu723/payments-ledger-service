"""Fixtures for tests that need PostgreSQL.

Start the database first:  docker compose up -d db
If it is not running, the tests that need it are skipped, unless
REQUIRE_DATABASE=1 is set (as in CI), where a missing database is an error.
"""

import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Base
from app.db.schema import create_schema

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://ledger:ledger@localhost:5433/ledger_test"
)


@pytest.fixture(scope="session")
def db_engine():
    # pool_size: the concurrency tests open many connections at once.
    engine = create_engine(TEST_DATABASE_URL, pool_size=20, max_overflow=40)
    try:
        with engine.connect():
            pass
    except OperationalError:
        if os.environ.get("REQUIRE_DATABASE") == "1":
            raise  # in CI, skipped database tests would hide a broken setup
        pytest.skip("PostgreSQL is not running (start it with: docker compose up -d db)")
    Base.metadata.drop_all(engine)
    create_schema(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session_factory(db_engine):
    """Makes independent sessions, like separate API requests. Every table is emptied after the test."""
    yield sessionmaker(db_engine, expire_on_commit=False)
    table_names = ", ".join(table.name for table in Base.metadata.sorted_tables)
    with db_engine.begin() as connection:
        connection.execute(text(f"TRUNCATE {table_names} RESTART IDENTITY CASCADE"))


@pytest.fixture
def db_session(session_factory):
    with session_factory() as session:
        yield session
