"""HTTP endpoints.

Writes that move money (POST /entries, POST /entries/{id}/reverse) need an
Idempotency-Key header. Every write needs an X-User header naming who acts;
this demo has no real authentication.

    POST /accounts                      open an account
    GET  /accounts, /accounts/{number}  balances
    GET  /accounts/{number}/statement   activity built from ledger events (may lag by a moment)
    POST /entries                       post an entry (or create it PENDING if it needs approval)
    GET  /entries/{id}                  an entry with its lines and audit trail
    POST /entries/{id}/approve          approve a pending entry (not by its preparer)
    POST /entries/{id}/reject           reject a pending entry
    POST /entries/{id}/reverse          reverse a posted entry
    POST /periods/{period}/close        close a month
    GET  /periods                       periods and their status
    GET  /trial-balance?as_of=          every account's balance on a date
    GET  /reconciliation                the checks an auditor would run; all must pass
    GET  /exports/gl-detail.csv         posted entries in the risk platform's format
    GET  /exports/trial-balance.csv     opening and closing balances for its rollforward
"""

from datetime import date

from fastapi import APIRouter, Depends, Header, status
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.schemas import (
    AccountIn,
    AccountOut,
    CheckOut,
    EntryIn,
    EntryOut,
    LineOut,
    PeriodOut,
    ReconciliationOut,
    ReverseIn,
    StatementLineOut,
    TrialBalanceLineOut,
    TrialBalanceOut,
)
from app.db.models import Account, JournalEntry, Period, StatementLine
from app.db.session import get_session
from app.ledger.accounts import get_account, open_account
from app.ledger.entries import EntryRequest, LineRequest
from app.ledger.errors import EntryNotFound
from app.ledger.idempotency import fingerprint, run_once
from app.ledger.periods import close_period
from app.ledger.posting import post_entry
from app.ledger.reports import gl_detail_csv, reconcile, trial_balance, trial_balance_csv
from app.ledger.review import approve_entry, reject_entry, reverse_entry

router = APIRouter()

User = Header(alias="X-User", min_length=1, max_length=64, description="who is acting")
IdempotencyKey = Header(alias="Idempotency-Key", min_length=1, max_length=128)


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/accounts", response_model=AccountOut, status_code=status.HTTP_201_CREATED)
def create_account(body: AccountIn, session: Session = Depends(get_session)):
    account = open_account(session, body.number, body.name, body.account_type, body.currency, body.allow_negative)
    session.commit()
    return account


@router.get("/accounts", response_model=list[AccountOut])
def list_accounts(session: Session = Depends(get_session)):
    return list(session.scalars(select(Account).order_by(Account.number)))


@router.get("/accounts/{number}", response_model=AccountOut)
def read_account(number: str, session: Session = Depends(get_session)):
    return get_account(session, number)


@router.get("/accounts/{number}/statement", response_model=list[StatementLineOut])
def read_statement(number: str, session: Session = Depends(get_session)):
    get_account(session, number)  # 404 for unknown accounts
    query = select(StatementLine).where(StatementLine.account_number == number).order_by(StatementLine.id)
    return list(session.scalars(query))


@router.post("/entries", response_model=EntryOut, status_code=status.HTTP_201_CREATED)
def create_entry(
    body: EntryIn,
    user: str = User,
    idempotency_key: str = IdempotencyKey,
    session: Session = Depends(get_session),
):
    request = EntryRequest(
        posting_date=body.posting_date,
        description=body.description,
        lines=tuple(LineRequest(line.account_number, line.debit, line.credit) for line in body.lines),
        source=body.source,
    )
    return _idempotent(
        session, user, idempotency_key, "/entries", body.model_dump(mode="json"),
        lambda: post_entry(session, request, user),
    )


@router.get("/entries/{entry_id}", response_model=EntryOut)
def read_entry(entry_id: int, session: Session = Depends(get_session)):
    entry = session.get(JournalEntry, entry_id)
    if entry is None:
        raise EntryNotFound(f"entry {entry_id} not found")
    return entry_out(session, entry)


@router.post("/entries/{entry_id}/approve", response_model=EntryOut)
def approve(entry_id: int, user: str = User, session: Session = Depends(get_session)):
    entry = approve_entry(session, entry_id, user)
    session.commit()
    return entry_out(session, entry)


@router.post("/entries/{entry_id}/reject", response_model=EntryOut)
def reject(entry_id: int, user: str = User, session: Session = Depends(get_session)):
    entry = reject_entry(session, entry_id, user)
    session.commit()
    return entry_out(session, entry)


@router.post("/entries/{entry_id}/reverse", response_model=EntryOut, status_code=status.HTTP_201_CREATED)
def reverse(
    entry_id: int,
    body: ReverseIn,
    user: str = User,
    idempotency_key: str = IdempotencyKey,
    session: Session = Depends(get_session),
):
    return _idempotent(
        session, user, idempotency_key, f"/entries/{entry_id}/reverse", body.model_dump(mode="json"),
        lambda: reverse_entry(session, entry_id, user, body.posting_date),
    )


@router.post("/periods/{period}/close", response_model=PeriodOut)
def close(period: str, user: str = User, session: Session = Depends(get_session)):
    closed = close_period(session, period, user)
    session.commit()
    return closed


@router.get("/periods", response_model=list[PeriodOut])
def list_periods(session: Session = Depends(get_session)):
    return list(session.scalars(select(Period).order_by(Period.period)))


@router.get("/trial-balance", response_model=TrialBalanceOut)
def read_trial_balance(as_of: date | None = None, session: Session = Depends(get_session)):
    as_of = as_of or date.today()
    rows = trial_balance(session, as_of)
    return TrialBalanceOut(
        as_of=as_of,
        accounts=[TrialBalanceLineOut(**row.__dict__) for row in rows],
        total_debit=sum((row.balance for row in rows if row.balance > 0), start=0),
        total_credit=-sum((row.balance for row in rows if row.balance < 0), start=0),
    )


@router.get("/reconciliation", response_model=ReconciliationOut)
def read_reconciliation(session: Session = Depends(get_session)):
    checks = reconcile(session)
    return ReconciliationOut(
        ok=all(check.passed for check in checks), checks=[CheckOut(**check.__dict__) for check in checks]
    )


@router.get("/exports/gl-detail.csv")
def export_gl_detail(start: date, end: date, session: Session = Depends(get_session)):
    return Response(gl_detail_csv(session, start, end), media_type="text/csv")


@router.get("/exports/trial-balance.csv")
def export_trial_balance(start: date, end: date, session: Session = Depends(get_session)):
    return Response(trial_balance_csv(session, start, end), media_type="text/csv")


def _idempotent(session: Session, user: str, key: str, path: str, body: dict, write) -> JSONResponse:
    """Run `write` once per (user, key); a retry gets the first response back."""

    def operation():
        entry = write()
        return status.HTTP_201_CREATED, entry_out(session, entry).model_dump(mode="json")

    # Keys belong to a user: two clients that happen to pick the same key never see each other's results.
    scoped_key = f"{user.strip().lower()}:{key}"
    try:
        response = run_once(session, scoped_key, fingerprint("POST", path, body), operation)
    except Exception:
        session.rollback()
        raise
    headers = {"Idempotent-Replayed": "true"} if response.replayed else {}
    return JSONResponse(status_code=response.status, content=response.body, headers=headers)


def entry_out(session: Session, entry: JournalEntry) -> EntryOut:
    reversed_by = session.scalar(select(JournalEntry.id).where(JournalEntry.reverses_entry_id == entry.id))
    return EntryOut(
        id=entry.id,
        posting_date=entry.posting_date,
        description=entry.description,
        source=entry.source,
        currency=entry.currency,
        amount=entry.amount,
        status=entry.status,
        created_by=entry.created_by,
        created_at=entry.created_at,
        approved_by=entry.approved_by,
        approved_at=entry.approved_at,
        rejected_by=entry.rejected_by,
        rejected_at=entry.rejected_at,
        reverses_entry_id=entry.reverses_entry_id,
        reversed_by_entry_id=reversed_by,
        lines=[
            LineOut(line_number=line.line_number, account_number=line.account.number, debit=line.debit, credit=line.credit)
            for line in entry.lines
        ],
    )
