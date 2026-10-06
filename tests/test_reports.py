import csv
import io
import re
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.db.models import EntrySource
from app.ledger.periods import close_period
from app.ledger.posting import post_entry
from app.ledger.reports import (
    GL_DETAIL_COLUMNS,
    TRIAL_BALANCE_COLUMNS,
    gl_detail_csv,
    reconcile,
    trial_balance,
    trial_balance_csv,
)
from tests.factories import deposit, open_bank, open_wallet, transfer, withdraw

SEPT_30 = date(2026, 9, 30)
OCT_5 = date(2026, 10, 5)


@pytest.fixture
def ledger(db_session):
    """September: a deposit. October: a transfer, a withdrawal, and one pending manual entry."""
    open_bank(db_session)
    open_wallet(db_session, "2001")
    open_wallet(db_session, "2002")
    post_entry(db_session, deposit("2001", "1000.00", posting_date=SEPT_30), user="api")
    post_entry(db_session, transfer("2001", "2002", "300.00", posting_date=OCT_5), user="api")
    post_entry(db_session, withdraw("2002", "50.00", posting_date=OCT_5), user="api")
    post_entry(db_session, transfer("2001", "2002", "20000.00", posting_date=OCT_5, source=EntrySource.MANUAL),
               user="alice")  # PENDING: needs approval, so it must not count anywhere
    db_session.commit()
    return db_session


def by_number(rows):
    return {row.account_number: row.balance for row in rows}


def read_csv(content):
    return list(csv.DictReader(io.StringIO(content)))


def test_trial_balance_counts_posted_entries_up_to_the_date(ledger):
    september = by_number(trial_balance(ledger, SEPT_30))
    october = by_number(trial_balance(ledger, OCT_5))

    assert september == {"1000": Decimal("1000.00"), "2001": Decimal("-1000.00"), "2002": Decimal("0.00")}
    assert october == {"1000": Decimal("950.00"), "2001": Decimal("-700.00"), "2002": Decimal("-250.00")}
    assert sum(october.values()) == 0  # a trial balance balances


def test_a_healthy_ledger_passes_every_check(ledger):
    checks = reconcile(ledger)
    assert [check.name for check in checks] == [
        "Total debits equal total credits",
        "Every entry balances",
        "Running balances match the journal",
        "No protected account below zero",
    ]
    assert all(check.passed for check in checks)


def test_reconciliation_catches_a_running_balance_that_drifted(ledger):
    # The balance column is a cache; someone edits it directly.
    ledger.execute(text("UPDATE accounts SET balance = balance - 5 WHERE number = '2002'"))
    ledger.commit()

    checks = {check.name: check for check in reconcile(ledger)}
    assert not checks["Running balances match the journal"].passed
    assert checks["Running balances match the journal"].detail == "accounts whose balance drifted: 2002"
    assert checks["Total debits equal total credits"].passed  # the journal itself is still fine


def test_reconciliation_catches_an_overdrawn_wallet(ledger):
    ledger.execute(text("UPDATE accounts SET balance = 10 WHERE number = '2001'"))  # a wallet holding -10
    ledger.commit()
    checks = {check.name: check for check in reconcile(ledger)}
    assert checks["No protected account below zero"].detail == "overdrawn accounts: 2001"


def test_gl_export_matches_the_risk_platform_format(ledger):
    rows = read_csv(gl_detail_csv(ledger, date(2026, 10, 1), date(2026, 10, 31)))

    assert tuple(rows[0].keys()) == GL_DETAIL_COLUMNS
    assert [row["entry_id"] for row in rows] == ["LE00000002", "LE00000002", "LE00000003", "LE00000003"]
    assert rows[0]["account_number"] == "2001" and rows[0]["debit"] == "300.00" and rows[0]["credit"] == ""
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", rows[0]["created_at"])
    assert rows[0]["prepared_by"] == "api"
    # Entry 4 is pending, so it is not exported; the risk platform will report LE00000004 as a gap.


def test_exports_roll_forward(ledger):
    """What the risk platform checks first: opening + activity = closing, for every account."""
    start, end = date(2026, 10, 1), date(2026, 10, 31)
    gl = read_csv(gl_detail_csv(ledger, start, end))
    tb = read_csv(trial_balance_csv(ledger, start, end))
    assert tuple(tb[0].keys()) == TRIAL_BALANCE_COLUMNS

    for account in tb:
        activity = sum(
            Decimal(row["debit"] or 0) - Decimal(row["credit"] or 0)
            for row in gl
            if row["account_number"] == account["account_number"]
        )
        assert Decimal(account["opening_balance"]) + activity == Decimal(account["closing_balance"])


def test_the_api_serves_reports(ledger, session_factory):
    from fastapi.testclient import TestClient

    from app.db.session import get_session
    from app.main import app

    def test_session():
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = test_session
    try:
        client = TestClient(app)
        tb = client.get("/trial-balance", params={"as_of": "2026-10-05"}).json()
        assert tb["total_debit"] == tb["total_credit"] == "950.00"
        assert client.get("/reconciliation").json()["ok"] is True
        export = client.get("/exports/gl-detail.csv", params={"start": "2026-10-01", "end": "2026-10-31"})
        assert export.headers["content-type"].startswith("text/csv")
        assert export.text.startswith("entry_id,line_number")
    finally:
        app.dependency_overrides.clear()


def test_closing_september_does_not_change_its_numbers(ledger):
    close_period(ledger, "2026-09", user="controller")
    ledger.commit()
    assert by_number(trial_balance(ledger, SEPT_30))["2001"] == Decimal("-1000.00")
