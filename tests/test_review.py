from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal

import pytest

from app.db.models import EntrySource, JournalEntry
from app.ledger.accounts import get_account
from app.ledger.errors import (
    EntryNotFound,
    InsufficientFunds,
    InvalidState,
    PeriodClosed,
    SegregationOfDuties,
)
from app.ledger.periods import close_period
from app.ledger.posting import post_entry
from app.ledger.review import approve_entry, reject_entry, reverse_entry
from tests.factories import DAY, deposit, open_bank, open_wallet, transfer

MANUAL = EntrySource.MANUAL


@pytest.fixture
def ledger(session_factory):
    with session_factory() as session:
        open_bank(session)
        open_wallet(session, "2001")
        open_wallet(session, "2002")
        post_entry(session, deposit("2001", "50000.00"), user="api")
        session.commit()
    return session_factory


def wallet(session, number):
    return get_account(session, number).normal_balance


def big_manual_transfer(session, amount="20000.00", prepared_by="alice"):
    entry = post_entry(session, transfer("2001", "2002", amount, source=MANUAL), user=prepared_by)
    session.commit()
    return entry.id


class TestApproval:
    @pytest.mark.parametrize("amount, status", [("10000.00", "POSTED"), ("10000.01", "PENDING")])
    def test_manual_entries_above_the_threshold_wait_for_approval(self, ledger, amount, status):
        with ledger() as session:
            entry = post_entry(session, transfer("2001", "2002", amount, source=MANUAL), user="alice")
            assert entry.status == status

    def test_system_entries_do_not_need_approval(self, ledger):
        with ledger() as session:
            assert post_entry(session, transfer("2001", "2002", "20000.00"), user="api").status == "POSTED"

    def test_a_pending_entry_does_not_move_money(self, ledger):
        with ledger() as session:
            big_manual_transfer(session)
            assert (wallet(session, "2001"), wallet(session, "2002")) == (Decimal("50000.00"), Decimal("0"))

    @pytest.mark.parametrize("approver", ["alice", "ALICE", " Alice "])
    def test_the_preparer_cannot_approve(self, ledger, approver):
        with ledger() as session:
            entry_id = big_manual_transfer(session, prepared_by="alice")
            with pytest.raises(SegregationOfDuties, match="cannot also approve"):
                approve_entry(session, entry_id, user=approver)

    def test_someone_else_approves_and_the_money_moves(self, ledger):
        with ledger() as session:
            entry_id = big_manual_transfer(session)
            approved = approve_entry(session, entry_id, user="bob")
            session.commit()

            assert (approved.status, approved.approved_by) == ("POSTED", "bob")
            assert approved.approved_at is not None
            assert (wallet(session, "2001"), wallet(session, "2002")) == (Decimal("30000.00"), Decimal("20000.00"))

    def test_an_entry_is_approved_once(self, ledger):
        with ledger() as session:
            entry_id = big_manual_transfer(session)
            approve_entry(session, entry_id, user="bob")
            session.commit()
            with pytest.raises(InvalidState, match="only a pending entry"):
                approve_entry(session, entry_id, user="carol")

    def test_two_approvers_at_the_same_instant_post_it_once(self, ledger):
        with ledger() as session:
            entry_id = big_manual_transfer(session)

        def try_to_approve(i):
            with ledger() as session:
                try:
                    with session.begin():
                        approve_entry(session, entry_id, user=f"approver{i}")
                    return "approved"
                except InvalidState:
                    return "too late"

        with ThreadPoolExecutor(max_workers=10) as pool:
            outcomes = list(pool.map(try_to_approve, range(10)))

        assert outcomes.count("approved") == 1
        with ledger() as session:
            assert wallet(session, "2002") == Decimal("20000.00")  # moved once

    def test_funds_are_checked_at_approval(self, ledger):
        with ledger() as session:
            entry_id = big_manual_transfer(session, amount="40000.00")
            post_entry(session, transfer("2001", "2002", "30000.00"), user="api")  # leaves only 20,000
            session.commit()
            with pytest.raises(InsufficientFunds):
                approve_entry(session, entry_id, user="bob")
            session.rollback()
            assert session.get(JournalEntry, entry_id).status == "PENDING"

    def test_a_period_that_closed_while_waiting_cannot_be_posted_to(self, ledger):
        with ledger() as session:
            entry_id = big_manual_transfer(session)
            close_period(session, f"{DAY:%Y-%m}", user="controller")
            session.commit()
            with pytest.raises(PeriodClosed):
                approve_entry(session, entry_id, user="bob")

    def test_a_rejected_entry_never_posts(self, ledger):
        with ledger() as session:
            entry_id = big_manual_transfer(session)
            rejected = reject_entry(session, entry_id, user="bob")
            session.commit()
            assert (rejected.status, rejected.rejected_by) == ("REJECTED", "bob")
            with pytest.raises(InvalidState):
                approve_entry(session, entry_id, user="carol")

    def test_unknown_entries(self, ledger):
        with ledger() as session:
            with pytest.raises(EntryNotFound):
                approve_entry(session, 999, user="bob")


class TestReversal:
    def test_a_reversal_undoes_the_entry_and_points_back_to_it(self, ledger):
        with ledger() as session:
            original = post_entry(session, transfer("2001", "2002", "300.00"), user="api")
            session.commit()
            reversal = reverse_entry(session, original.id, user="alice", posting_date=DAY)
            session.commit()

            assert reversal.reverses_entry_id == original.id
            assert reversal.description.startswith(f"Reversal of entry {original.id}")
            assert [(l.debit, l.credit) for l in reversal.lines] == [
                (Decimal("0"), Decimal("300.00")),
                (Decimal("300.00"), Decimal("0")),
            ]
            assert (wallet(session, "2001"), wallet(session, "2002")) == (Decimal("50000.00"), Decimal("0"))

    def test_an_entry_is_reversed_once(self, ledger):
        with ledger() as session:
            original = post_entry(session, transfer("2001", "2002", "300.00"), user="api")
            reverse_entry(session, original.id, user="alice", posting_date=DAY)
            session.commit()
            with pytest.raises(InvalidState, match="already reversed"):
                reverse_entry(session, original.id, user="alice", posting_date=DAY)

    def test_only_posted_entries_can_be_reversed(self, ledger):
        with ledger() as session:
            entry_id = big_manual_transfer(session)
            with pytest.raises(InvalidState, match="only a posted entry"):
                reverse_entry(session, entry_id, user="bob", posting_date=DAY)

    def test_an_entry_in_a_closed_period_is_reversed_in_an_open_one(self, ledger):
        with ledger() as session:
            original = post_entry(session, transfer("2001", "2002", "300.00", posting_date=date(2026, 9, 30)), user="api")
            close_period(session, "2026-09", user="controller")
            session.commit()

            with pytest.raises(PeriodClosed):
                reverse_entry(session, original.id, user="alice", posting_date=date(2026, 9, 30))
            session.rollback()
            reversal = reverse_entry(session, original.id, user="alice", posting_date=date(2026, 10, 1))
            assert reversal.status == "POSTED"

    def test_money_already_spent_cannot_be_reversed_out(self, ledger):
        with ledger() as session:
            original = post_entry(session, transfer("2001", "2002", "300.00"), user="api")
            post_entry(session, transfer("2002", "2001", "300.00"), user="api")  # 2002 spent it
            session.commit()
            with pytest.raises(InsufficientFunds, match="2002"):
                reverse_entry(session, original.id, user="alice", posting_date=DAY)

    def test_a_large_reversal_needs_approval_too(self, ledger):
        with ledger() as session:
            original = post_entry(session, transfer("2001", "2002", "20000.00"), user="api")
            reversal = reverse_entry(session, original.id, user="alice", posting_date=DAY)
            assert reversal.status == "PENDING"
