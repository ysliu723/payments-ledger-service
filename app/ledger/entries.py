"""What a request to post an entry looks like, and the checks that need no database."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from app.db.models import EntrySource
from app.ledger.errors import InvalidEntry

CENT = Decimal("0.01")


@dataclass(frozen=True)
class LineRequest:
    account_number: str
    debit: Decimal = Decimal("0")
    credit: Decimal = Decimal("0")


@dataclass(frozen=True)
class EntryRequest:
    posting_date: date
    description: str
    lines: tuple[LineRequest, ...]
    source: EntrySource = EntrySource.SYSTEM

    @property
    def amount(self) -> Decimal:
        return sum((line.debit for line in self.lines), Decimal("0"))


def validate(request: EntryRequest) -> None:
    """Raise InvalidEntry listing every problem, not just the first one."""
    problems = []
    if not request.description.strip():
        problems.append("description is blank")
    if len(request.lines) < 2:
        problems.append("an entry needs at least two lines")
    for number, line in enumerate(request.lines, start=1):
        for side in ("debit", "credit"):
            value = getattr(line, side)
            if not isinstance(value, Decimal):
                problems.append(f"line {number}: {side} must be a Decimal")
            elif value < 0:
                problems.append(f"line {number}: {side} cannot be negative")
            elif value != value.quantize(CENT):
                problems.append(f"line {number}: {side} has more than two decimal places")
        if isinstance(line.debit, Decimal) and isinstance(line.credit, Decimal):
            if (line.debit == 0) == (line.credit == 0):
                problems.append(f"line {number}: exactly one of debit and credit must be non-zero")
    if not problems:
        total_debit = request.amount
        total_credit = sum((line.credit for line in request.lines), Decimal("0"))
        if total_debit != total_credit:
            problems.append(f"debits {total_debit} do not equal credits {total_credit}")
    if problems:
        raise InvalidEntry("; ".join(problems))
