"""Copy outbox events to Kafka.

    python -m app.events.relay

Each batch: lock up to N unpublished events (SKIP LOCKED, so several relays
can run without taking the same events), send them, wait for the broker to
acknowledge, then mark them published, all in one transaction.

If the relay crashes after sending but before marking, the same events
are sent again when it restarts. Delivery is therefore at-least-once, and
consumers must ignore duplicates (see consumer.py).
"""

import time
from collections.abc import Callable
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import OutboxEvent

# Sends a batch of events; raises if the broker did not acknowledge all of them.
Publisher = Callable[[list[OutboxEvent]], None]


def publish_pending(session_factory: sessionmaker[Session], publish: Publisher, batch_size: int = 100) -> int:
    """Publish one batch of unpublished events, oldest first. Returns how many were published."""
    with session_factory() as session, session.begin():
        events = list(
            session.scalars(
                select(OutboxEvent)
                .where(OutboxEvent.published_at.is_(None))
                .order_by(OutboxEvent.id)
                .limit(batch_size)
                .with_for_update(skip_locked=True)
            )
        )
        if not events:
            return 0
        publish(events)  # if this raises, nothing is marked and the batch is retried later
        now = datetime.now(timezone.utc)
        for event in events:
            event.published_at = now
        return len(events)


def run_forever(session_factory: sessionmaker[Session], publish: Publisher, idle_seconds: float = 0.5) -> None:
    while True:
        if publish_pending(session_factory, publish) == 0:
            time.sleep(idle_seconds)


if __name__ == "__main__":
    from app.config import settings
    from app.db.session import SessionLocal
    from app.events.kafka import KafkaPublisher, ensure_topic

    ensure_topic(settings.kafka_bootstrap, settings.events_topic)
    print(f"relay: outbox -> {settings.events_topic} on {settings.kafka_bootstrap}", flush=True)
    run_forever(SessionLocal, KafkaPublisher(settings.kafka_bootstrap, settings.events_topic))
