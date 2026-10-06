"""Posting entries to the ledger.

Functions here never commit. The caller owns the transaction, so either
everything an operation writes is saved, or none of it is.

Locks are always taken in the same order: an existing entry, then the
period, then the accounts (by id). The same order everywhere means two
operations can never each hold a lock the other is waiting for.
"""

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import Account, EntrySource, EntryStatus, JournalEntry, JournalLine
from app.ledger.entries import EntryRequest, validate
from app.ledger.errors import AccountNotFound, EntryNotFound, InsufficientFunds, InvalidEntry
from app.ledger.periods import ensure_open


def needs_approval(request: EntryRequest) -> bool:
    """Large manual entries wait for a second person. System entries come from other controlled systems."""
    return request.source == EntrySource.MANUAL and request.amount > settings.approval_threshold


def post_entry(
    session: Session, request: EntryRequest, user: str, reverses_entry_id: int | None = None
) -> JournalEntry:
    """Record an entry. It is POSTED right away, or PENDING if it needs approval first."""
    validate(request)
    ensure_open(session, request.posting_date)
    accounts = load_accounts(session, {line.account_number for line in request.lines})
    currencies = {account.currency for account in accounts.values()}
    if len(currencies) > 1:
        raise InvalidEntry(f"all accounts in an entry must use one currency, got {', '.join(sorted(currencies))}")

    pending = needs_approval(request)
    entry = JournalEntry(
        posting_date=request.posting_date,
        description=request.description.strip(),
        source=str(request.source),
        currency=currencies.pop(),
        amount=request.amount,
        status=str(EntryStatus.PENDING if pending else EntryStatus.POSTED),
        created_by=user,
        reverses_entry_id=reverses_entry_id,
    )
    session.add(entry)
    session.flush()  # entry.id is needed for its lines
    for number, line in enumerate(request.lines, start=1):
        account = accounts[line.account_number]
        session.add(
            JournalLine(entry_id=entry.id, line_number=number, account_id=account.id, debit=line.debit, credit=line.credit)
        )
    if not pending:  # a pending entry does not touch balances until it is approved
        movements = [(line.account_number, line.debit, line.credit) for line in request.lines]
        apply_to_balances(movements, accounts)
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


def apply_to_balances(movements: list[tuple[str, Decimal, Decimal]], accounts: dict[str, Account]) -> None:
    """Add each (account number, debit, credit) to the running balances, then refuse any overdraft."""
    for number, debit, credit in movements:
        account = accounts[number]
        account.balance = account.balance + debit - credit
    for account in accounts.values():
        if not account.allow_negative and account.normal_balance < 0:
            raise InsufficientFunds(
                f"account {account.number} would go to {account.normal_balance}; it may not go below zero"
            )


def lock_entry(session: Session, entry_id: int) -> JournalEntry:
    """Load an existing entry and lock it, so two people cannot act on it at the same moment."""
    query = (
        select(JournalEntry)
        .where(JournalEntry.id == entry_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    entry = session.scalars(query).one_or_none()
    if entry is None:
        raise EntryNotFound(f"entry {entry_id} not found")
    return entry


def movements_of(entry: JournalEntry) -> list[tuple[str, Decimal, Decimal]]:
    return [(line.account.number, line.debit, line.credit) for line in entry.lines]
