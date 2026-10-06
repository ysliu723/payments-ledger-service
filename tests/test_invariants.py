"""Random sequences of operations, some of which are refused. Whatever happens, the ledger stays correct.

Invariants:
- total debits equal total credits, and every entry balances
- every running balance equals the sum of its journal lines
- no protected account is below zero
- the trial balance sums to zero
"""

import random
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal

from sqlalchemy import select

from app.db.models import EntrySource, JournalEntry
from app.ledger.errors import LedgerError
from app.ledger.periods import close_period
from app.ledger.posting import post_entry
from app.ledger.reports import reconcile, trial_balance
from app.ledger.review import approve_entry, reject_entry, reverse_entry
from tests.factories import deposit, open_bank, open_wallet, transfer, withdraw

WALLETS = [f"20{n:02d}" for n in range(1, 9)]
USERS = ["alice", "bob", "carol"]
SEPTEMBER, OCTOBER = date(2026, 9, 15), date(2026, 10, 15)


def setup(session_factory):
    with session_factory() as session:
        open_bank(session)
        for number in WALLETS:
            open_wallet(session, number)
        session.commit()


def random_operation(session, rng: random.Random, day: date) -> None:
    """One random operation; it may well be refused. The caller commits or rolls back."""
    amount = f"{rng.choice([1, 5, 20, 100, 750, 12000, 30000])}.{rng.randint(0, 99):02d}"
    a, b = rng.sample(WALLETS, 2)
    user = rng.choice(USERS)
    choice = rng.random()
    if choice < 0.25:
        post_entry(session, deposit(a, amount, posting_date=day), user="api")
    elif choice < 0.55:
        post_entry(session, transfer(a, b, amount, posting_date=day), user="api")
    elif choice < 0.65:
        post_entry(session, withdraw(a, amount, posting_date=day), user="api")
    elif choice < 0.75:
        post_entry(session, transfer(a, b, amount, posting_date=day, source=EntrySource.MANUAL), user=user)
    else:
        entry_ids = session.scalars(select(JournalEntry.id)).all()
        if not entry_ids:
            return
        entry_id = rng.choice(entry_ids)
        action = rng.choice(["approve", "approve", "reject", "reverse"])
        if action == "approve":
            approve_entry(session, entry_id, user=user)  # sometimes by the preparer: refused
        elif action == "reject":
            reject_entry(session, entry_id, user=user)
        else:
            reverse_entry(session, entry_id, user=user, posting_date=day)


def run_operations(session_factory, seed: int, count: int, day: date) -> dict[str, int]:
    rng = random.Random(seed)
    outcomes = {"done": 0, "refused": 0}
    for _ in range(count):
        with session_factory() as session:
            try:
                random_operation(session, rng, day)
                session.commit()
                outcomes["done"] += 1
            except LedgerError:
                session.rollback()
                outcomes["refused"] += 1
    return outcomes


def assert_invariants(session_factory) -> None:
    with session_factory() as session:
        failed = [check for check in reconcile(session) if not check.passed]
        assert failed == []
        assert sum(row.balance for row in trial_balance(session, date.max)) == Decimal("0")


def test_a_random_sequence_keeps_every_invariant(session_factory):
    setup(session_factory)
    september = run_operations(session_factory, seed=1, count=150, day=SEPTEMBER)
    with session_factory() as session:
        close_period(session, "2026-09", user="controller")
        session.commit()
    october = run_operations(session_factory, seed=2, count=150, day=OCTOBER)

    assert september["done"] > 50 and september["refused"] > 10  # both kinds really happened
    assert october["refused"] > 10  # overdrafts, self-approvals, entries no longer pending, double reversals
    assert_invariants(session_factory)


def test_random_operations_from_eight_threads_at_once_keep_every_invariant(session_factory):
    """A deadlock would surface as a database error and fail this test."""
    setup(session_factory)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda seed: run_operations(session_factory, seed, 40, OCTOBER), range(8)))
    assert sum(result["done"] for result in results) > 100
    assert_invariants(session_factory)
