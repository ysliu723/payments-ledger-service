"""The same path through a real Kafka-compatible broker (Redpanda).

Start it first:  docker compose up -d redpanda
Skipped if no broker is reachable, unless REQUIRE_KAFKA=1 (as in CI).
"""

import json
import os
import time
import uuid
from decimal import Decimal

import pytest
from confluent_kafka.admin import AdminClient
from sqlalchemy import select

from app.db.models import StatementLine
from app.events.consumer import apply_event
from app.events.kafka import KafkaPublisher, ensure_topic, make_consumer
from app.events.relay import publish_pending
from app.ledger.posting import post_entry
from tests.factories import deposit, open_bank, open_wallet, transfer

BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP", "localhost:19092")


@pytest.fixture
def topic():
    try:
        AdminClient({"bootstrap.servers": BOOTSTRAP, "socket.timeout.ms": 2000}).list_topics(timeout=3)
    except Exception:
        if os.environ.get("REQUIRE_KAFKA") == "1":
            raise
        pytest.skip("no Kafka broker (start one with: docker compose up -d redpanda)")
    name = f"test.ledger.{uuid.uuid4().hex[:8]}"  # a fresh topic, so earlier runs cannot interfere
    ensure_topic(BOOTSTRAP, name)
    return name


def test_events_flow_from_the_outbox_through_kafka_to_statements(session_factory, topic):
    with session_factory() as session:
        open_bank(session)
        open_wallet(session, "2001")
        open_wallet(session, "2002")
        post_entry(session, deposit("2001", "100.00"), user="api")
        post_entry(session, transfer("2001", "2002", "25.00"), user="api")
        session.commit()

    assert publish_pending(session_factory, KafkaPublisher(BOOTSTRAP, topic)) == 2

    consumer = make_consumer(BOOTSTRAP, topic, group=f"test-{uuid.uuid4().hex[:8]}")
    received = []
    deadline = time.time() + 30
    try:
        while len(received) < 2 and time.time() < deadline:
            message = consumer.poll(timeout=1.0)
            if message is None or message.error():
                continue
            apply_event(session_factory, json.loads(message.value()))
            consumer.commit(message=message, asynchronous=False)
            received.append(message)
    finally:
        consumer.close()

    assert len(received) == 2
    with session_factory() as session:
        amounts = session.scalars(
            select(StatementLine.amount).where(StatementLine.account_number == "2001").order_by(StatementLine.id)
        )
        assert list(amounts) == [Decimal("100.00"), Decimal("-25.00")]
