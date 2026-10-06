"""Many requests at the same moment, each in its own transaction, like real API traffic."""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

from sqlalchemy import func, select

from app.db.models import Account, JournalLine
from app.ledger.accounts import get_account
from app.ledger.errors import InsufficientFunds
from app.ledger.posting import post_entry
from tests.factories import deposit, open_bank, open_wallet, transfer, withdraw


def run_at_once(task, count):
    """Run `task(i)` for i in range(count) on `count` threads at the same time."""
    with ThreadPoolExecutor(max_workers=count) as pool:
        return list(pool.map(task, range(count)))


def post_in_own_transaction(session_factory, request):
    with session_factory() as session:
        try:
            with session.begin():  # commits at the end, or rolls back on an error
                post_entry(session, request, user="api")
            return "posted"
        except InsufficientFunds:
            return "refused"


def setup_wallets(session_factory, *funded):
    with session_factory() as session:
        open_bank(session)
        for number, amount in funded:
            open_wallet(session, number)
            post_entry(session, deposit(number, amount), user="api")
        session.commit()


def balance_from_lines(session, number):
    """The balance recomputed from the journal lines: the source of truth."""
    account = get_account(session, number)
    debits_minus_credits = session.scalar(
        select(func.coalesce(func.sum(JournalLine.debit - JournalLine.credit), 0)).where(
            JournalLine.account_id == account.id
        )
    )
    return -debits_minus_credits  # a wallet is a liability: credits increase it


def test_simultaneous_withdrawals_never_overdraw(session_factory):
    setup_wallets(session_factory, ("2001", "1000.00"))

    # 50 withdrawals of 80.00 from 1,000.00: exactly 12 fit.
    outcomes = run_at_once(lambda _: post_in_own_transaction(session_factory, withdraw("2001", "80.00")), 50)

    assert outcomes.count("posted") == 12
    assert outcomes.count("refused") == 38
    with session_factory() as session:
        assert get_account(session, "2001").normal_balance == Decimal("40.00")
        assert balance_from_lines(session, "2001") == Decimal("40.00")  # the running balance lost no update


def test_transfers_in_opposite_directions_do_not_deadlock(session_factory):
    setup_wallets(session_factory, ("2001", "1000.00"), ("2002", "1000.00"))

    def back_and_forth(i):
        request = transfer("2001", "2002", "1.00") if i % 2 == 0 else transfer("2002", "2001", "1.00")
        return post_in_own_transaction(session_factory, request)

    outcomes = run_at_once(back_and_forth, 40)

    assert outcomes == ["posted"] * 40  # no deadlock errors, nothing refused
    with session_factory() as session:
        balances = session.scalars(select(Account.balance).where(Account.number.in_(["2001", "2002"])))
        assert [-value for value in balances] == [Decimal("1000.00"), Decimal("1000.00")]
