import uuid

import pytest
from fastapi.testclient import TestClient

from app.db.session import get_session
from app.main import app

DAY = "2026-10-05"


@pytest.fixture
def client(session_factory):
    def test_session():
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = test_session
    # Not used as a context manager, so the app's startup does not touch the real database.
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def api(client):
    """A client with a bank, two wallets, and 1,000.00 in wallet 2001."""
    for number, name, account_type, allow_negative in [
        ("1000", "Bank clearing", "ASSET", True),
        ("2001", "Alice wallet", "LIABILITY", False),
        ("2002", "Bob wallet", "LIABILITY", False),
    ]:
        response = client.post(
            "/accounts",
            json={"number": number, "name": name, "account_type": account_type, "allow_negative": allow_negative},
        )
        assert response.status_code == 201, response.text
    post(client, lines(("1000", "1000.00", "0"), ("2001", "0", "1000.00")), description="Deposit")
    return client


def lines(*items):
    return [{"account_number": number, "debit": debit, "credit": credit} for number, debit, credit in items]


def post(client, entry_lines, description="Transfer", source="SYSTEM", user="alice", key=None):
    body = {"posting_date": DAY, "description": description, "source": source, "lines": entry_lines}
    headers = {"X-User": user, "Idempotency-Key": key or str(uuid.uuid4())}
    return client.post("/entries", json=body, headers=headers)


def transfer(client, amount, **kwargs):
    return post(client, lines(("2001", amount, "0"), ("2002", "0", amount)), **kwargs)


def wallet(client, number):
    return client.get(f"/accounts/{number}").json()["normal_balance"]


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_a_transfer_moves_money_and_amounts_stay_exact(api):
    response = transfer(api, "250.00")

    assert response.status_code == 201
    entry = response.json()
    assert (entry["status"], entry["amount"], entry["created_by"]) == ("POSTED", "250.00", "alice")
    assert [line["account_number"] for line in entry["lines"]] == ["2001", "2002"]
    assert (wallet(api, "2001"), wallet(api, "2002")) == ("750.00", "250.00")


def test_a_retry_with_the_same_key_posts_once(api):
    first = transfer(api, "100.00", key="retry-me")
    retry = transfer(api, "100.00", key="retry-me")

    assert retry.status_code == 201
    assert retry.json() == first.json()
    assert retry.headers["Idempotent-Replayed"] == "true"
    assert "Idempotent-Replayed" not in first.headers
    assert wallet(api, "2001") == "900.00"


def test_a_key_reused_for_a_different_request_is_refused(api):
    transfer(api, "100.00", key="k1")
    response = transfer(api, "999.00", key="k1")
    assert response.status_code == 409
    assert response.json()["error"] == "IDEMPOTENCY_KEY_REUSED"


def test_keys_belong_to_each_user(api):
    transfer(api, "100.00", key="k1", user="alice")
    response = transfer(api, "100.00", key="k1", user="bob")
    assert "Idempotent-Replayed" not in response.headers  # bob's request ran on its own
    assert wallet(api, "2001") == "800.00"


def test_writes_need_a_user_and_a_key(api):
    body = {"posting_date": DAY, "description": "x", "lines": lines(("2001", "1.00", "0"), ("2002", "0", "1.00"))}
    assert api.post("/entries", json=body, headers={"Idempotency-Key": "k"}).status_code == 422
    assert api.post("/entries", json=body, headers={"X-User": "alice"}).status_code == 422


@pytest.mark.parametrize(
    "entry_lines, status, error",
    [
        (lines(("2001", "5000.00", "0"), ("2002", "0", "5000.00")), 422, "INSUFFICIENT_FUNDS"),
        (lines(("2001", "10.00", "0"), ("2002", "0", "9.00")), 422, "INVALID_ENTRY"),
        (lines(("2001", "10.00", "0"), ("9999", "0", "10.00")), 404, "ACCOUNT_NOT_FOUND"),
    ],
)
def test_refused_entries_explain_why(api, entry_lines, status, error):
    response = post(api, entry_lines)
    assert response.status_code == status
    assert response.json()["error"] == error
    assert response.json()["detail"]
    assert wallet(api, "2001") == "1000.00"


def test_approval_flow(api):
    big = post(
        api, lines(("1000", "20000.00", "0"), ("2002", "0", "20000.00")), description="Manual top-up",
        source="MANUAL", user="alice",
    ).json()
    assert big["status"] == "PENDING"  # manual and above the 10,000 threshold
    assert wallet(api, "2002") == "0.00"

    same_person = api.post(f"/entries/{big['id']}/approve", headers={"X-User": " ALICE "})
    assert (same_person.status_code, same_person.json()["error"]) == (403, "SEGREGATION_OF_DUTIES")

    approved = api.post(f"/entries/{big['id']}/approve", headers={"X-User": "bob"}).json()
    assert (approved["status"], approved["approved_by"]) == ("POSTED", "bob")
    assert wallet(api, "2002") == "20000.00"

    again = api.post(f"/entries/{big['id']}/approve", headers={"X-User": "carol"})
    assert (again.status_code, again.json()["error"]) == (409, "INVALID_STATE")


def test_rejecting_a_pending_entry(api):
    big = post(
        api, lines(("1000", "20000.00", "0"), ("2002", "0", "20000.00")), source="MANUAL", user="alice"
    ).json()
    rejected = api.post(f"/entries/{big['id']}/reject", headers={"X-User": "bob"}).json()
    assert (rejected["status"], rejected["rejected_by"]) == ("REJECTED", "bob")
    assert wallet(api, "2002") == "0.00"


def test_reversal_links_both_ways(api):
    original = transfer(api, "100.00").json()
    reversal = api.post(
        f"/entries/{original['id']}/reverse",
        json={"posting_date": DAY},
        headers={"X-User": "alice", "Idempotency-Key": "undo-1"},
    )
    assert reversal.status_code == 201
    assert reversal.json()["reverses_entry_id"] == original["id"]
    assert api.get(f"/entries/{original['id']}").json()["reversed_by_entry_id"] == reversal.json()["id"]
    assert wallet(api, "2001") == "1000.00"


def test_closing_a_period(api):
    closed = api.post("/periods/2026-10/close", headers={"X-User": "controller"})
    assert (closed.status_code, closed.json()["status"], closed.json()["closed_by"]) == (200, "CLOSED", "controller")

    refused = transfer(api, "1.00")
    assert (refused.status_code, refused.json()["error"]) == (422, "PERIOD_CLOSED")
    assert api.post("/periods/2026-10/close", headers={"X-User": "controller"}).status_code == 409
    assert api.get("/periods").json()[0]["period"] == "2026-10"


def test_not_found(api):
    assert api.get("/entries/999").status_code == 404
    assert api.get("/accounts/9999").status_code == 404
