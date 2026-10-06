"""Database connections."""

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings

engine = create_engine(
    settings.database_url, pool_size=settings.db_pool_size, max_overflow=settings.db_max_overflow
)
SessionLocal = sessionmaker(engine, expire_on_commit=False)


def get_session() -> Iterator[Session]:
    """One database session per API request, closed when the request ends."""
    with SessionLocal() as session:
        yield session
