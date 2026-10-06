"""Trial balance, reconciliation, and exports in the risk platform's format.

Only POSTED entries count. Pending entries have not moved money, and
rejected ones never will.
"""

import csv
import io
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import Account, EntryStatus, JournalEntry, JournalLine

POSTED = str(EntryStatus.POSTED)

# The risk platform's input format (journal-entry-risk-platform, app/ingestion/csv_loader.py).
GL_DETAIL_COLUMNS = (
    "entry_id", "line_number", "posting_date", "created_at", "source", "description", "prepared_by",
    "approved_by", "currency", "account_number", "debit", "credit", "department", "vendor",
)
TRIAL_BALANCE_COLUMNS = ("account_number", "account_name", "account_type", "opening_balance", "closing_balance")


@dataclass(frozen=True)
class TrialBalanceRow:
    account_number: str
    account_name: str
    account_type: str
    balance: Decimal  # debit-positive


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str


def balances_as_of(session: Session, as_of: date, before: bool = False) -> dict[int, Decimal]:
    """Each account's debit-positive balance from posted lines dated up to `as_of` (or strictly before it)."""
    date_condition = JournalEntry.posting_date < as_of if before else JournalEntry.posting_date <= as_of
    query = (
        select(JournalLine.account_id, func.sum(JournalLine.debit - JournalLine.credit))
        .join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
        .where(JournalEntry.status == POSTED, date_condition)
        .group_by(JournalLine.account_id)
    )
    return {account_id: net for account_id, net in session.execute(query)}


def trial_balance(session: Session, as_of: date) -> list[TrialBalanceRow]:
    nets = balances_as_of(session, as_of)
    accounts = session.scalars(select(Account).order_by(Account.number))
    return [
        TrialBalanceRow(a.number, a.name, a.account_type, nets.get(a.id, Decimal("0.00"))) for a in accounts
    ]


def reconcile(session: Session) -> list[Check]:
    """The checks an auditor would run on this ledger. All must pass."""
    checks = []

    debits, credits = session.execute(
        select(func.coalesce(func.sum(JournalLine.debit), 0), func.coalesce(func.sum(JournalLine.credit), 0))
        .join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
        .where(JournalEntry.status == POSTED)
    ).one()
    checks.append(Check("Total debits equal total credits", debits == credits, f"debits {debits}, credits {credits}"))

    unbalanced = session.scalars(
        select(JournalLine.entry_id)
        .group_by(JournalLine.entry_id)
        .having(func.sum(JournalLine.debit) != func.sum(JournalLine.credit))
    ).all()
    checks.append(Check("Every entry balances", not unbalanced, _ids("unbalanced entries", unbalanced)))

    nets = balances_as_of(session, date.max)
    accounts = list(session.scalars(select(Account).order_by(Account.number)))
    drifted = [a.number for a in accounts if a.balance != nets.get(a.id, Decimal("0"))]
    checks.append(
        Check("Running balances match the journal", not drifted, _ids("accounts whose balance drifted", drifted))
    )

    overdrawn = [a.number for a in accounts if not a.allow_negative and a.normal_balance < 0]
    checks.append(Check("No protected account below zero", not overdrawn, _ids("overdrawn accounts", overdrawn)))
    return checks


def gl_detail_csv(session: Session, start: date, end: date) -> str:
    """Posted entries dated start..end as a CSV the risk platform can load directly.

    Entry IDs look like LE00000042. Pending and rejected entries are left
    out, so their numbers appear as gaps; the risk platform reports those
    gaps, which is what an auditor would want to ask about.
    """
    zone = ZoneInfo(settings.company_timezone)
    query = (
        select(JournalEntry, JournalLine, Account.number)
        .join(JournalLine, JournalLine.entry_id == JournalEntry.id)
        .join(Account, Account.id == JournalLine.account_id)
        .where(JournalEntry.status == POSTED, JournalEntry.posting_date.between(start, end))
        .order_by(JournalEntry.id, JournalLine.line_number)
    )
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(GL_DETAIL_COLUMNS)
    for entry, line, account_number in session.execute(query):
        writer.writerow([
            f"LE{entry.id:08d}",
            line.line_number,
            entry.posting_date.isoformat(),
            entry.created_at.astimezone(zone).strftime("%Y-%m-%d %H:%M:%S"),
            entry.source,
            entry.description,
            entry.created_by,
            entry.approved_by or "",
            entry.currency,
            account_number,
            _amount(line.debit),
            _amount(line.credit),
            "",
            "",
        ])
    return output.getvalue()


def trial_balance_csv(session: Session, start: date, end: date) -> str:
    """Opening balances (before start) and closing balances (through end), for the risk platform's rollforward."""
    opening = balances_as_of(session, start, before=True)
    closing = balances_as_of(session, end)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(TRIAL_BALANCE_COLUMNS)
    for account in session.scalars(select(Account).order_by(Account.number)):
        writer.writerow([
            account.number,
            account.name,
            account.account_type,
            f"{opening.get(account.id, Decimal('0')):.2f}",
            f"{closing.get(account.id, Decimal('0')):.2f}",
        ])
    return output.getvalue()


def _amount(value: Decimal) -> str:
    return f"{value:.2f}" if value else ""


def _ids(label: str, values: list) -> str:
    return f"{label}: {', '.join(str(v) for v in values)}" if values else "none"
