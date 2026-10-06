"""Approving, rejecting, and reversing entries.

Approval enforces segregation of duties: the person who prepared an entry
can never approve it. That is the preventive control behind the risk
platform's SELF_APPROVAL and MISSING_APPROVAL rules.

Posted entries never change. A mistake is corrected with a reversal: a new
entry with debits and credits swapped, linked to the original. Each entry
can be reversed once.
"""

from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import EntrySource, EntryStatus, JournalEntry
from app.ledger.entries import EntryRequest, LineRequest
from app.ledger.errors import InvalidState, SegregationOfDuties
from app.ledger.periods import ensure_open
from app.ledger.posting import apply_to_balances, load_accounts, lock_entry, movements_of, post_entry


def same_person(first: str, second: str) -> bool:
    """User IDs can differ only in case or spacing: "alice" and " ALICE " are the same person."""
    return first.strip().lower() == second.strip().lower()


def approve_entry(session: Session, entry_id: int, user: str) -> JournalEntry:
    entry = lock_entry(session, entry_id)
    if entry.status != EntryStatus.PENDING:
        raise InvalidState(f"entry {entry_id} is {entry.status}; only a pending entry can be approved")
    if same_person(entry.created_by, user):
        raise SegregationOfDuties(f"{user} prepared entry {entry_id} and cannot also approve it")
    ensure_open(session, entry.posting_date)  # the period may have closed while the entry waited
    movements = movements_of(entry)
    accounts = load_accounts(session, {number for number, _, _ in movements})
    apply_to_balances(movements, accounts)  # funds are checked now, at approval, not when it was created
    entry.status = str(EntryStatus.POSTED)
    entry.approved_by = user
    entry.approved_at = datetime.now(timezone.utc)
    session.flush()
    return entry


def reject_entry(session: Session, entry_id: int, user: str) -> JournalEntry:
    """Anyone may reject a pending entry, including its preparer withdrawing it."""
    entry = lock_entry(session, entry_id)
    if entry.status != EntryStatus.PENDING:
        raise InvalidState(f"entry {entry_id} is {entry.status}; only a pending entry can be rejected")
    entry.status = str(EntryStatus.REJECTED)
    entry.rejected_by = user
    entry.rejected_at = datetime.now(timezone.utc)
    session.flush()
    return entry


def reverse_entry(session: Session, entry_id: int, user: str, posting_date: date) -> JournalEntry:
    """Post a new entry that undoes a posted one. A large reversal needs approval like any manual entry."""
    original = lock_entry(session, entry_id)
    if original.status != EntryStatus.POSTED:
        raise InvalidState(f"entry {entry_id} is {original.status}; only a posted entry can be reversed")
    existing = session.scalar(select(JournalEntry.id).where(JournalEntry.reverses_entry_id == entry_id))
    if existing is not None:
        raise InvalidState(f"entry {entry_id} was already reversed by entry {existing}")

    swapped = tuple(LineRequest(number, debit=credit, credit=debit) for number, debit, credit in movements_of(original))
    request = EntryRequest(
        posting_date=posting_date,
        description=f"Reversal of entry {entry_id}: {original.description}",
        lines=swapped,
        source=EntrySource.MANUAL,  # a person asked for it
    )
    try:
        return post_entry(session, request, user, reverses_entry_id=entry_id)
    except IntegrityError:
        # Another reversal of the same entry committed first (the unique constraint caught it).
        session.rollback()
        raise InvalidState(f"entry {entry_id} was already reversed") from None
