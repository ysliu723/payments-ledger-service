"""The database itself refuses to break the ledger's rules, even when the application is bypassed."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.db.models import Account, JournalEntry, JournalLine


def make_accounts(session):
    cash = Account(number="1000", name="Cash", account_type="ASSET", currency="USD")
    revenue = Account(number="4000", name="Revenue", account_type="REVENUE", currency="USD")
    session.add_all([cash, revenue])
    session.commit()
    return cash, revenue


def add_entry(session, lines, status="POSTED"):
    """Write an entry straight into the tables, skipping every check in the application."""
    entry = JournalEntry(
        posting_date=date(2026, 10, 5),
        description="test entry",
        source="MANUAL",
        currency="USD",
        amount=sum(Decimal(debit) for _, debit, _ in lines),
        status=status,
        created_by="alice",
    )
    session.add(entry)
    session.flush()
    for number, (account, debit, credit) in enumerate(lines, start=1):
        session.add(
            JournalLine(
                entry_id=entry.id, line_number=number, account_id=account.id,
                debit=Decimal(debit), credit=Decimal(credit),
            )
        )
    return entry


def test_a_balanced_entry_is_accepted(db_session):
    cash, revenue = make_accounts(db_session)
    add_entry(db_session, [(cash, "100.00", "0"), (revenue, "0", "100.00")])
    db_session.commit()


def test_an_unbalanced_entry_is_refused_at_commit(db_session):
    cash, revenue = make_accounts(db_session)
    add_entry(db_session, [(cash, "100.00", "0"), (revenue, "0", "90.00")])
    with pytest.raises(DBAPIError, match="not balanced"):
        db_session.commit()


def test_a_one_line_entry_is_refused(db_session):
    cash, _ = make_accounts(db_session)
    add_entry(db_session, [(cash, "100.00", "0")])
    with pytest.raises(DBAPIError, match="not balanced"):
        db_session.commit()


@pytest.mark.parametrize(
    "debit, credit, constraint",
    [("100.00", "100.00", "exactly_one_side"), ("0", "0", "exactly_one_side"), ("-5.00", "0", "amounts_not_negative")],
)
def test_malformed_lines_are_refused(db_session, debit, credit, constraint):
    cash, revenue = make_accounts(db_session)
    add_entry(db_session, [(cash, debit, credit), (revenue, "0", "100.00")])
    with pytest.raises(IntegrityError, match=constraint):
        db_session.flush()


def test_journal_lines_cannot_be_changed_or_deleted(db_session):
    cash, revenue = make_accounts(db_session)
    add_entry(db_session, [(cash, "100.00", "0"), (revenue, "0", "100.00")])
    db_session.commit()

    with pytest.raises(DBAPIError, match="append-only: UPDATE"):
        db_session.execute(text("UPDATE journal_lines SET debit = 999 WHERE line_number = 1"))
    db_session.rollback()
    with pytest.raises(DBAPIError, match="append-only: DELETE"):
        db_session.execute(text("DELETE FROM journal_lines"))


def test_a_posted_entry_cannot_change_or_be_deleted(db_session):
    cash, revenue = make_accounts(db_session)
    add_entry(db_session, [(cash, "100.00", "0"), (revenue, "0", "100.00")])
    db_session.commit()

    with pytest.raises(DBAPIError, match="is POSTED; it can no longer change"):
        db_session.execute(text("UPDATE journal_entries SET description = 'edited'"))
    db_session.rollback()
    with pytest.raises(DBAPIError, match="DELETE is not allowed"):
        db_session.execute(text("DELETE FROM journal_entries"))


def test_a_pending_entry_may_only_change_its_status_and_approval(db_session):
    cash, revenue = make_accounts(db_session)
    add_entry(db_session, [(cash, "100.00", "0"), (revenue, "0", "100.00")], status="PENDING")
    db_session.commit()

    db_session.execute(text("UPDATE journal_entries SET status = 'POSTED', approved_by = 'bob'"))
    db_session.commit()

    add_entry(db_session, [(cash, "50.00", "0"), (revenue, "0", "50.00")], status="PENDING")
    db_session.commit()
    with pytest.raises(DBAPIError, match="only the status and approval"):
        db_session.execute(text("UPDATE journal_entries SET amount = 1 WHERE status = 'PENDING'"))
