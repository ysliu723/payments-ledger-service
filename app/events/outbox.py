"""Recording events in the outbox, in the same transaction as the posting.

The dual-write problem: writing the entry to PostgreSQL and the event to
Kafka are two separate systems. Crash in between and you get money moved
with no event, or an event for money that never moved. Writing the event
into an outbox table inside the same database transaction makes the two
succeed or fail together; the relay then copies outbox rows to Kafka.
"""

import uuid

from sqlalchemy.orm import Session

from app.db.models import JournalEntry, OutboxEvent

ENTRY_POSTED = "entry.posted"


def record_posted(session: Session, entry: JournalEntry) -> OutboxEvent:
    payload = {
        "entry_id": entry.id,
        "posting_date": entry.posting_date.isoformat(),
        "description": entry.description,
        "currency": entry.currency,
        "amount": str(entry.amount),
        "lines": [
            {
                "account_number": line.account.number,
                "account_type": line.account.account_type,
                "debit": str(line.debit),
                "credit": str(line.credit),
            }
            for line in entry.lines
        ],
    }
    event = OutboxEvent(event_id=str(uuid.uuid4()), event_type=ENTRY_POSTED, entry_id=entry.id, payload=payload)
    session.add(event)
    return event


def as_message(event: OutboxEvent) -> dict:
    """What goes onto the topic: the payload plus the event's identity."""
    return {"event_id": event.event_id, "event_type": event.event_type, **event.payload}
