import threading
from datetime import date

import pytest

from app.db.models import Period
from app.ledger.errors import InvalidEntry, InvalidState, PeriodClosed
from app.ledger.periods import close_period
from app.ledger.posting import post_entry
from tests.factories import deposit, open_bank, open_wallet


@pytest.fixture
def ledger(session_factory):
    with session_factory() as session:
        open_bank(session)
        open_wallet(session, "2001")
        session.commit()
    return session_factory


def test_nothing_can_be_posted_into_a_closed_period(ledger):
    with ledger() as session:
        post_entry(session, deposit("2001", "10.00", posting_date=date(2026, 9, 30)), user="api")
        close_period(session, "2026-09", user="controller")
        session.commit()

        with pytest.raises(PeriodClosed, match="2026-09 is closed"):
            post_entry(session, deposit("2001", "10.00", posting_date=date(2026, 9, 30)), user="api")
        session.rollback()
        post_entry(session, deposit("2001", "10.00", posting_date=date(2026, 10, 1)), user="api")  # next month is open


def test_closing_records_who_and_when(ledger):
    with ledger() as session:
        close_period(session, "2026-09", user="controller")
        session.commit()
        period = session.get(Period, "2026-09")
        assert (period.status, period.closed_by) == ("CLOSED", "controller")
        assert period.closed_at is not None


def test_a_period_closes_once(ledger):
    with ledger() as session:
        close_period(session, "2026-09", user="controller")
        session.commit()
        with pytest.raises(InvalidState, match="already closed"):
            close_period(session, "2026-09", user="controller")


def test_period_format_is_checked(ledger):
    with ledger() as session:
        with pytest.raises(InvalidEntry, match="2026-10"):
            close_period(session, "2026-13", user="controller")


def test_closing_waits_for_a_posting_already_in_progress(ledger):
    posted = threading.Event()  # the posting has written its entry but not yet committed
    release = threading.Event()  # let the posting commit
    closed = threading.Event()

    def slow_posting():
        with ledger() as session, session.begin():
            post_entry(session, deposit("2001", "10.00", posting_date=date(2026, 9, 30)), user="api")
            posted.set()
            release.wait(timeout=10)

    def close():
        posted.wait(timeout=10)
        with ledger() as session, session.begin():
            close_period(session, "2026-09", user="controller")
        closed.set()

    threads = [threading.Thread(target=slow_posting), threading.Thread(target=close)]
    for thread in threads:
        thread.start()

    assert not closed.wait(timeout=0.5)  # the close is blocked by the posting's share lock
    release.set()
    for thread in threads:
        thread.join(timeout=10)
    assert closed.is_set()  # it went through once the posting committed

    with ledger() as session:
        with pytest.raises(PeriodClosed):
            post_entry(session, deposit("2001", "10.00", posting_date=date(2026, 9, 30)), user="api")
