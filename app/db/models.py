"""Database tables.

    accounts ── journal_lines ── journal_entries
    periods            which months are closed
    idempotency_keys   the first response to each idempotency key

Balances are debit-positive (debits - credits), the same convention as the
risk platform. The `balance` column on accounts is a running total kept for
fast reads; the journal lines are the source of truth, and the
reconciliation report checks that the two always agree.
"""

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

Money = Numeric(18, 2)


class AccountType(StrEnum):
    ASSET = "ASSET"
    LIABILITY = "LIABILITY"
    EQUITY = "EQUITY"
    REVENUE = "REVENUE"
    EXPENSE = "EXPENSE"


# Assets and expenses normally have debit balances; the others normally have credit balances.
DEBIT_NORMAL_TYPES = {AccountType.ASSET, AccountType.EXPENSE}


class EntryStatus(StrEnum):
    PENDING = "PENDING"  # waiting for approval; does not affect balances yet
    POSTED = "POSTED"
    REJECTED = "REJECTED"


class EntrySource(StrEnum):
    MANUAL = "MANUAL"  # keyed in by a person
    SYSTEM = "SYSTEM"  # created by another system, e.g. a payment


class Base(DeclarativeBase):
    pass


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    account_type: Mapped[str] = mapped_column(String(16))
    currency: Mapped[str] = mapped_column(String(3))
    # A customer's wallet must never go below zero; a clearing account at the bank may.
    allow_negative: Mapped[bool] = mapped_column(default=False)
    balance: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    @property
    def normal_balance(self) -> Decimal:
        """The balance as people read it: a wallet holding 100.00 shows 100.00, not -100.00."""
        if AccountType(self.account_type) in DEBIT_NORMAL_TYPES:
            return self.balance
        return -self.balance


class Period(Base):
    __tablename__ = "periods"

    period: Mapped[str] = mapped_column(String(7), primary_key=True)  # e.g. "2026-10"
    status: Mapped[str] = mapped_column(String(8), default="OPEN")
    closed_by: Mapped[str | None] = mapped_column(String(64))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class JournalEntry(Base):
    __tablename__ = "journal_entries"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    posting_date: Mapped[date]
    description: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(16))
    currency: Mapped[str] = mapped_column(String(3))
    amount: Mapped[Decimal] = mapped_column(Money)  # total debits
    status: Mapped[str] = mapped_column(String(16))
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    approved_by: Mapped[str | None] = mapped_column(String(64))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Corrections are new entries that reverse an old one; each entry can be reversed once.
    reverses_entry_id: Mapped[int | None] = mapped_column(ForeignKey("journal_entries.id"), unique=True)

    lines: Mapped[list["JournalLine"]] = relationship(order_by="JournalLine.line_number")


class JournalLine(Base):
    __tablename__ = "journal_lines"
    __table_args__ = (
        CheckConstraint("debit >= 0 AND credit >= 0", name="amounts_not_negative"),
        CheckConstraint("(debit = 0) <> (credit = 0)", name="exactly_one_side"),
        UniqueConstraint("entry_id", "line_number"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    entry_id: Mapped[int] = mapped_column(ForeignKey("journal_entries.id"), index=True)
    line_number: Mapped[int]
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    debit: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    credit: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))

    account: Mapped[Account] = relationship()


class IdempotencyRecord(Base):
    """The response to the first request made with a key, replayed for any retry with that key."""

    __tablename__ = "idempotency_keys"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    # Filled in the same transaction as the insert, so a committed row always has them.
    response_status: Mapped[int | None]
    response_body: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
