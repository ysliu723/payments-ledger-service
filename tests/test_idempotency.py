from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.db.models import JournalEntry
from app.ledger.accounts import get_account
from app.ledger.errors import IdempotencyKeyReused, InsufficientFunds
from app.ledger.idempotency import Response, fingerprint, run_once
from app.ledger.posting import post_entry
from tests.factories import deposit, open_bank, open_wallet, transfer


def submit(session, key, request):
    """What the API does for a POST with an Idempotency-Key."""

    def operation():
        posted = post_entry(session, request, user="api")
        return 201, {"entry_id": posted.id}

    request_hash = fingerprint("POST", "/entries", {"description": request.description, "amount": str(request.amount)})
    try:
        return run_once(session, key, request_hash, operation)
    except Exception:
        session.rollback()
        raise


def entry_count(session):
    return session.scalar(select(func.count()).select_from(JournalEntry))


@pytest.fixture
def ledger(session_factory):
    with session_factory() as session:
        open_bank(session)
        open_wallet(session, "2001")
        open_wallet(session, "2002")
        post_entry(session, deposit("2001", "100.00"), user="api")
        session.commit()
    return session_factory


def test_a_retry_returns_the_first_response_and_posts_nothing(ledger):
    with ledger() as session:
        first = submit(session, "key-1", transfer("2001", "2002", "30.00"))
    with ledger() as session:
        retry = submit(session, "key-1", transfer("2001", "2002", "30.00"))
        assert retry == Response(first.status, first.body, replayed=True)
        assert get_account(session, "2001").normal_balance == Decimal("70.00")  # moved once, not twice
        assert entry_count(session) == 2  # the deposit and one transfer


def test_reusing_a_key_for_a_different_request_is_refused(ledger):
    with ledger() as session:
        submit(session, "key-1", transfer("2001", "2002", "30.00"))
    with ledger() as session:
        with pytest.raises(IdempotencyKeyReused, match="key-1"):
            submit(session, "key-1", transfer("2001", "2002", "99.00"))


def test_different_keys_are_different_requests(ledger):
    with ledger() as session:
        submit(session, "key-1", transfer("2001", "2002", "10.00"))
        submit(session, "key-2", transfer("2001", "2002", "10.00"))
        assert get_account(session, "2001").normal_balance == Decimal("80.00")


def test_a_refused_request_can_be_retried_with_the_same_key(ledger):
    with ledger() as session:
        with pytest.raises(InsufficientFunds):
            submit(session, "key-1", transfer("2001", "2002", "500.00"))
    with ledger() as session:
        post_entry(session, deposit("2001", "500.00"), user="api")
        session.commit()
        response = submit(session, "key-1", transfer("2001", "2002", "500.00"))
        assert response.replayed is False


def test_twenty_identical_requests_at_the_same_instant_post_once(ledger):
    def attempt(_):
        with ledger() as session:
            return submit(session, "same-key", transfer("2001", "2002", "25.00"))

    with ThreadPoolExecutor(max_workers=20) as pool:
        responses = list(pool.map(attempt, range(20)))

    assert len({response.body["entry_id"] for response in responses}) == 1  # everyone got the same entry
    assert sum(1 for response in responses if not response.replayed) == 1
    with ledger() as session:
        assert get_account(session, "2001").normal_balance == Decimal("75.00")
        assert entry_count(session) == 2
