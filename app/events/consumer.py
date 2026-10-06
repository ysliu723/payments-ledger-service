"""Build account statements from ledger events.

    python -m app.events.consumer

Events arrive at least once, so the same event can come twice. Each event
is applied in one transaction that also records its event_id in
processed_events; a duplicate hits the primary key and is skipped. The
Kafka offset is committed only after that transaction, so a crash in
between means the event is delivered again and skipped as a duplicate:
the statement ends up exactly right either way.

Statements are eventually consistent: they lag the ledger by however long
the relay and this consumer take, typically well under a second.
"""

import json
from decimal import Decimal

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import DEBIT_NORMAL_TYPES, AccountType, ProcessedEvent, StatementLine


def apply_event(session_factory: sessionmaker[Session], message: dict) -> bool:
    """Apply one event to the statements. Returns False if it had already been applied."""
    with session_factory() as session:
        session.add(ProcessedEvent(event_id=message["event_id"]))
        try:
            session.flush()
        except IntegrityError:
            session.rollback()
            return False
        for line in message["lines"]:
            debit, credit = Decimal(line["debit"]), Decimal(line["credit"])
            # As the account holder reads it: money into a wallet (a credit) is positive.
            normal = debit - credit if AccountType(line["account_type"]) in DEBIT_NORMAL_TYPES else credit - debit
            session.add(
                StatementLine(
                    account_number=line["account_number"],
                    entry_id=message["entry_id"],
                    posting_date=message["posting_date"],
                    description=message["description"],
                    amount=normal,
                    event_id=message["event_id"],
                )
            )
        session.commit()
        return True


def run_forever(session_factory: sessionmaker[Session], consumer) -> None:
    while True:
        message = consumer.poll(timeout=1.0)
        if message is None:
            continue
        if message.error():
            print(f"consumer: {message.error()}", flush=True)
            continue
        apply_event(session_factory, json.loads(message.value()))
        consumer.commit(message=message, asynchronous=False)


if __name__ == "__main__":
    from app.config import settings
    from app.db.session import SessionLocal
    from app.events.kafka import ensure_topic, make_consumer

    ensure_topic(settings.kafka_bootstrap, settings.events_topic)
    print(f"consumer: {settings.events_topic} -> statements", flush=True)
    run_forever(SessionLocal, make_consumer(settings.kafka_bootstrap, settings.events_topic, "statements"))
