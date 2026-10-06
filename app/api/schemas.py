"""The shapes of the JSON the API accepts and returns.

Send amounts as strings ("100.00"). They come back as strings too, so no
client ever sees a rounded floating-point number.
"""

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.db.models import EntrySource


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class AccountIn(BaseModel):
    number: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=255)
    account_type: str
    currency: str = "USD"
    allow_negative: bool = False


class AccountOut(ApiModel):
    number: str
    name: str
    account_type: str
    currency: str
    allow_negative: bool
    balance: Decimal  # debit-positive, as in the journal
    normal_balance: Decimal  # as people read it: a wallet holding 100.00 shows 100.00


class LineIn(BaseModel):
    account_number: str
    debit: Decimal = Decimal("0")
    credit: Decimal = Decimal("0")


class EntryIn(BaseModel):
    posting_date: date
    description: str
    source: EntrySource = EntrySource.SYSTEM
    lines: list[LineIn]


class ReverseIn(BaseModel):
    posting_date: date


class LineOut(BaseModel):
    line_number: int
    account_number: str
    debit: Decimal
    credit: Decimal


class EntryOut(BaseModel):
    id: int
    posting_date: date
    description: str
    source: str
    currency: str
    amount: Decimal
    status: str
    created_by: str
    created_at: datetime
    approved_by: str | None
    approved_at: datetime | None
    rejected_by: str | None
    rejected_at: datetime | None
    reverses_entry_id: int | None
    reversed_by_entry_id: int | None
    lines: list[LineOut]


class PeriodOut(ApiModel):
    period: str
    status: str
    closed_by: str | None
    closed_at: datetime | None


class TrialBalanceLineOut(BaseModel):
    account_number: str
    account_name: str
    account_type: str
    balance: Decimal  # debit-positive


class TrialBalanceOut(BaseModel):
    as_of: date
    accounts: list[TrialBalanceLineOut]
    total_debit: Decimal
    total_credit: Decimal


class CheckOut(BaseModel):
    name: str
    passed: bool
    detail: str


class ReconciliationOut(BaseModel):
    ok: bool
    checks: list[CheckOut]
