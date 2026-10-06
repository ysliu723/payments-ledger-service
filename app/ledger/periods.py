"""Closing accounting periods (months).

After a month is closed, nothing can be posted into it. This is the
preventive control behind the risk platform's POST_CLOSE_ENTRY rule.

Posting and closing at the same moment: every posting takes a SHARE lock
on its period row, which many postings can hold together. Closing takes an
exclusive lock, so it waits until postings already in progress have
committed; postings that start later wait for the close and then see
CLOSED. No entry can slip in halfway.
"""

import re
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.models import Period
from app.ledger.errors import InvalidEntry, InvalidState, PeriodClosed

PERIOD_FORMAT = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def period_of(day: date) -> str:
    return f"{day:%Y-%m}"


def ensure_open(session: Session, day: date) -> None:
    """Refuse to post into a closed period, and keep it from closing until this transaction ends."""
    period = _lock_period(session, period_of(day), exclusive=False)
    if period.status == "CLOSED":
        raise PeriodClosed(f"period {period.period} is closed; post to an open period instead")


def close_period(session: Session, period: str, user: str) -> Period:
    if not PERIOD_FORMAT.match(period):
        raise InvalidEntry("period must look like 2026-10")
    row = _lock_period(session, period, exclusive=True)
    if row.status == "CLOSED":
        raise InvalidState(f"period {period} is already closed")
    row.status = "CLOSED"
    row.closed_by = user
    row.closed_at = datetime.now(timezone.utc)
    session.flush()
    return row


def _lock_period(session: Session, period: str, exclusive: bool) -> Period:
    # Every period starts out open; create its row the first time it is used.
    session.execute(insert(Period).values(period=period, status="OPEN").on_conflict_do_nothing())
    query = (
        select(Period)
        .where(Period.period == period)
        .with_for_update(read=not exclusive)  # read=True means FOR SHARE
        .execution_options(populate_existing=True)
    )
    return session.scalars(query).one()
