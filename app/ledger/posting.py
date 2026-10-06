"""Posting entries to the ledger.

Functions here never commit. The caller owns the transaction, so either
everything an operation writes is saved, or none of it is.
"""

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Account, EntryStatus, JournalEntry, JournalLine
from app.ledger.entries import EntryRequest, validate
from app.ledger.errors import AccountNotFound, InsufficientFunds, InvalidEntry
from app.ledger.periods import ensure_open


def post_entry(session: Session, request: EntryRequest, user: str) -> JournalEntry:
    validate(request)
    ensure_open(session, request.posting_date)  # lock order everywhere: period first, then accounts
    accounts = load_accounts(session, {line.account_number for line in request.lines})
    currencies = {account.currency for account in accounts.values()}
    if len(currencies) > 1:
        raise InvalidEntry(f"all accounts in an entry must use one currency, got {', '.join(sorted(currencies))}")

    entry = JournalEntry(
        posting_date=request.posting_date,
        description=request.description.strip(),
        source=str(request.source),
        currency=currencies.pop(),
        amount=request.amount,
        status=str(EntryStatus.POSTED),
        created_by=user,
    )
    session.add(entry)
    session.flush()  # entry.id is needed for its lines
    for number, line in enumerate(request.lines, start=1):
        account = accounts[line.account_number]
        session.add(
            JournalLine(entry_id=entry.id, line_number=number, account_id=account.id, debit=line.debit, credit=line.credit)
        )
    apply_to_balances(request, accounts)
    session.flush()
    return entry


def load_accounts(session: Session, numbers: set[str]) -> dict[str, Account]:
    """Load the accounts and lock them until this transaction ends.

    Without the lock, two withdrawals can both read a balance of 100, both
    pass the check, and both subtract: an overdraft, and one lost update.
    With it (SELECT ... FOR UPDATE), the second waits until the first has
    committed, then reads the new balance.

    Locks are always taken in the same order (by id). Otherwise a transfer
    A -> B and a transfer B -> A could each hold one lock and wait forever
    for the other: a deadlock.
    """
    query = (
        select(Account)
        .where(Account.number.in_(numbers))
        .order_by(Account.id)
        .with_for_update()
        # Re-read the row even if this session already loaded it, so we use the locked value.
        .execution_options(populate_existing=True)
    )
    accounts = {account.number: account for account in session.scalars(query)}
    missing = sorted(numbers - accounts.keys())
    if missing:
        raise AccountNotFound(f"account(s) not found: {', '.join(missing)}")
    return accounts


def apply_to_balances(request: EntryRequest, accounts: dict[str, Account]) -> None:
    """Update the running balances, then refuse if any protected account would go below zero."""
    for line in request.lines:
        account = accounts[line.account_number]
        account.balance = account.balance + line.debit - line.credit
    for account in accounts.values():
        if not account.allow_negative and account.normal_balance < 0:
            raise InsufficientFunds(
                f"account {account.number} would go to {account.normal_balance}; it may not go below zero"
            )
