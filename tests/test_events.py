"""Outbox, relay, and the statement consumer, with an in-memory broker standing in for Kafka."""

import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.db.models import EntrySource, OutboxEvent, StatementLine
from app.events.consumer import apply_event
from app.events.outbox import as_message
from app.events.relay import publish_pending
from app.ledger.errors import InsufficientFunds
from app.ledger.posting import post_entry
from app.ledger.review import approve_entry
from tests.factories import deposit, open_bank, open_wallet, transfer, withdraw


class FakeBroker:
    """Collects what the relay sends. `fail_after` makes it crash partway through the first batch."""

    def __init__(self, fail_after=None):
        self.messages = []
        self.fail_after = fail_after
        self.lock = threading.Lock()

    def __call__(self, events):
        for number, event in enumerate(events):
            if self.fail_after is not None and number == self.fail_after:
                self.fail_after = None
                raise ConnectionError("broker went away")
            with self.lock:
                self.messages.append(as_message(event))


@pytest.fixture
def ledger(session_factory):
    with session_factory() as session:
        open_bank(session)
        open_wallet(session, "2001")
        open_wallet(session, "2002")
        session.commit()
    return session_factory


def outbox(session):
    return list(session.scalars(select(OutboxEvent).order_by(OutboxEvent.id)))


def statement(session, number):
    query = select(StatementLine.amount).where(StatementLine.account_number == number).order_by(StatementLine.id)
    return list(session.scalars(query))


class TestOutbox:
    def test_a_posting_writes_its_event_in_the_same_transaction(self, ledger):
        with ledger() as session:
            entry = post_entry(session, deposit("2001", "100.00"), user="api")
            session.commit()
            [event] = outbox(session)
            assert (event.event_type, event.entry_id, event.published_at) == ("entry.posted", entry.id, None)
            assert event.payload["amount"] == "100.00"
            assert [line["account_number"] for line in event.payload["lines"]] == ["1000", "2001"]

    def test_a_refused_posting_leaves_no_event(self, ledger):
        with ledger() as session:
            with pytest.raises(InsufficientFunds):
                post_entry(session, withdraw("2001", "5.00"), user="api")
            session.rollback()
            assert outbox(session) == []

    def test_a_pending_entry_has_no_event_until_it_is_approved(self, ledger):
        with ledger() as session:
            post_entry(session, deposit("2001", "50000.00"), user="api")
            pending = post_entry(session, transfer("2001", "2002", "20000.00", source=EntrySource.MANUAL), user="alice")
            session.commit()
            assert len(outbox(session)) == 1  # only the deposit

            approve_entry(session, pending.id, user="bob")
            session.commit()
            assert [event.entry_id for event in outbox(session)][-1] == pending.id


class TestRelay:
    def test_publishes_in_order_and_marks_events_published(self, ledger):
        with ledger() as session:
            post_entry(session, deposit("2001", "100.00"), user="api")
            post_entry(session, transfer("2001", "2002", "30.00"), user="api")
            session.commit()

        broker = FakeBroker()
        assert publish_pending(ledger, broker) == 2
        assert publish_pending(ledger, broker) == 0  # nothing left
        assert [message["entry_id"] for message in broker.messages] == [1, 2]
        with ledger() as session:
            assert all(event.published_at is not None for event in outbox(session))

    def test_a_crash_mid_batch_resends_everything_and_the_consumer_ignores_duplicates(self, ledger):
        with ledger() as session:
            post_entry(session, deposit("2001", "100.00"), user="api")
            post_entry(session, transfer("2001", "2002", "30.00"), user="api")
            post_entry(session, transfer("2002", "2001", "10.00"), user="api")
            session.commit()

        broker = FakeBroker(fail_after=2)  # two events reach the broker, then it fails
        with pytest.raises(ConnectionError):
            publish_pending(ledger, broker)
        assert publish_pending(ledger, broker) == 3  # nothing was marked, so all three go again
        assert len(broker.messages) == 5  # at-least-once: two duplicates

        # Delivered: 1, 2 (before the crash), then 1, 2, 3 (the resend). The resent 1 and 2 are skipped.
        applied = [apply_event(ledger, message) for message in broker.messages]
        assert applied == [True, True, False, False, True]
        with ledger() as session:
            assert statement(session, "2001") == [Decimal("100.00"), Decimal("-30.00"), Decimal("10.00")]
            assert statement(session, "2002") == [Decimal("30.00"), Decimal("-10.00")]

    def test_two_relays_running_together_send_each_event_once(self, ledger):
        with ledger() as session:
            post_entry(session, deposit("2001", "1000.00"), user="api")
            for _ in range(39):
                post_entry(session, transfer("2001", "2002", "1.00"), user="api")
            session.commit()

        broker = FakeBroker()

        def drain(_):
            while publish_pending(ledger, broker, batch_size=5):
                pass

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(drain, range(2)))

        entry_ids = [message["entry_id"] for message in broker.messages]
        assert sorted(entry_ids) == list(range(1, 41))  # all 40, none twice (SKIP LOCKED)


class TestConsumer:
    def test_statements_read_like_the_account_holder_sees_them(self, ledger):
        with ledger() as session:
            post_entry(session, deposit("2001", "100.00"), user="api")
            post_entry(session, withdraw("2001", "40.00"), user="api")
            session.commit()
        broker = FakeBroker()
        publish_pending(ledger, broker)
        for message in broker.messages:
            apply_event(ledger, message)

        with ledger() as session:
            assert statement(session, "2001") == [Decimal("100.00"), Decimal("-40.00")]  # wallet: money in is +
            assert statement(session, "1000") == [Decimal("100.00"), Decimal("-40.00")]  # bank asset: debit is +
            total = session.scalar(select(func.sum(StatementLine.amount)).where(StatementLine.account_number == "2001"))
            assert total == Decimal("60.00")
