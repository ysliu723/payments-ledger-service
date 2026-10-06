"""Settings, read from environment variables so Docker and CI can change them."""

import os
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Settings:
    database_url: str = os.environ.get(
        "DATABASE_URL", "postgresql+psycopg://ledger:ledger@localhost:5433/ledger"
    )
    # Connections kept open to PostgreSQL, plus extra ones allowed under load (SQLAlchemy defaults: 5 + 10).
    db_pool_size: int = int(os.environ.get("DB_POOL_SIZE", "5"))
    db_max_overflow: int = int(os.environ.get("DB_MAX_OVERFLOW", "10"))
    kafka_bootstrap: str = os.environ.get("KAFKA_BOOTSTRAP", "localhost:19092")
    events_topic: str = os.environ.get("EVENTS_TOPIC", "ledger.entries")
    # Manual entries above this amount wait for a second person's approval.
    approval_threshold: Decimal = Decimal(os.environ.get("APPROVAL_THRESHOLD", "10000"))
    # Exports show times in the company's own time zone: "late at night" means late there.
    company_timezone: str = os.environ.get("COMPANY_TIMEZONE", "America/Los_Angeles")


settings = Settings()
