from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.db.models import JournalEntry
from app.ledger.accounts import get_account, open_account
from app.ledger.entries import EntryRequest, LineRequest, validate
from app.ledger.errors import AccountExists, AccountNotFound, InsufficientFunds, InvalidEntry
from app.ledger.posting import post_entry
from tests.factories import DAY, deposit, entry, open_bank, open_wallet, transfer, withdraw


@pytest.fixture
def ledger(db_session):
    """A bank account and two customer wallets."""
    open_bank(db_session)
    open_wallet(db_session, "2001", "Alice")
    open_wallet(db_session, "2002", "Bob")
    db_session.commit()
    return db_session


def balance(session, number):
    return get_account(session, number).normal_balance


class TestValidation:
    def test_a_good_entry_passes(self):
        validate(transfer("2001", "2002", "10.00"))

    @pytest.mark.parametrize(
        "request_, message",
        [
            (entry(("2001", "10.00", "0"), ("2002", "0", "9.00")), "debits 10.00 do not equal credits 9.00"),
            (entry(("2001", "10.00", "0")), "at least two lines"),
            (entry(("2001", "-10.00", "0"), ("2002", "0", "-10.00")), "cannot be negative"),
            (entry(("2001", "10.00", "10.00"), ("2002", "0", "0")), "exactly one of debit and credit"),
            (entry(("2001", "10.001", "0"), ("2002", "0", "10.001")), "more than two decimal places"),
            (entry(("2001", "10.00", "0"), ("2002", "0", "10.00"), description="  "), "description is blank"),
        ],
    )
    def test_problems_are_reported(self, request_, message):
        with pytest.raises(InvalidEntry, match=message):
            validate(request_)

    def test_every_problem_is_listed_at_once(self):
        bad = entry(("2001", "-1", "0"), ("2002", "0", "1.001"), description="")
        with pytest.raises(InvalidEntry) as error:
            validate(bad)
        assert str(error.value).count(";") == 2  # three problems

    def test_floats_are_refused(self):
        with pytest.raises(InvalidEntry, match="must be a Decimal"):
            validate(EntryRequest(DAY, "float", (LineRequest("2001", 0.1), LineRequest("2002", credit=0.1))))


class TestPosting:
    def test_a_deposit_moves_both_balances(self, ledger):
        posted = post_entry(ledger, deposit("2001", "100.00"), user="api")
        ledger.commit()

        assert posted.status == "POSTED"
        assert posted.amount == Decimal("100.00")
        assert [(line.debit, line.credit) for line in posted.lines] == [
            (Decimal("100.00"), Decimal("0")),
            (Decimal("0"), Decimal("100.00")),
        ]
        assert balance(ledger, "2001") == Decimal("100.00")  # the wallet holds 100
        assert get_account(ledger, "1000").balance == Decimal("100.00")  # the bank received 100

    def test_a_transfer_moves_money_between_wallets(self, ledger):
        post_entry(ledger, deposit("2001", "100.00"), user="api")
        post_entry(ledger, transfer("2001", "2002", "30.00"), user="api")
        ledger.commit()
        assert (balance(ledger, "2001"), balance(ledger, "2002")) == (Decimal("70.00"), Decimal("30.00"))

    def test_a_wallet_cannot_go_below_zero(self, ledger):
        post_entry(ledger, deposit("2001", "50.00"), user="api")
        ledger.commit()

        with pytest.raises(InsufficientFunds, match="2001 would go to -30.00"):
            post_entry(ledger, withdraw("2001", "80.00"), user="api")
        ledger.rollback()

        assert balance(ledger, "2001") == Decimal("50.00")
        entry_count = ledger.scalar(select(func.count()).select_from(JournalEntry))
        assert entry_count == 1  # the refused withdrawal left nothing behind

    def test_an_account_allowed_to_go_negative_may(self, ledger):
        # A sign-up bonus funds the wallet without any money reaching the bank,
        # so paying it out takes the bank clearing account below zero, which it is allowed to do.
        open_account(ledger, "6000", "Promotions", "EXPENSE", "USD")
        post_entry(ledger, entry(("6000", "25.00", "0"), ("2001", "0", "25.00"), description="Sign-up bonus"), user="api")
        post_entry(ledger, withdraw("2001", "25.00"), user="api")
        ledger.commit()
        assert get_account(ledger, "1000").balance == Decimal("-25.00")

    def test_unknown_accounts_are_refused(self, ledger):
        with pytest.raises(AccountNotFound, match="9999"):
            post_entry(ledger, transfer("2001", "9999", "1.00"), user="api")

    def test_one_entry_cannot_mix_currencies(self, ledger):
        open_wallet(ledger, "2003", "Euro wallet", currency="EUR")
        with pytest.raises(InvalidEntry, match="one currency"):
            post_entry(ledger, transfer("2001", "2003", "1.00"), user="api")


class TestAccounts:
    def test_account_numbers_are_unique(self, ledger):
        with pytest.raises(AccountExists):
            open_wallet(ledger, "2001")

    def test_account_type_and_currency_are_checked(self, db_session):
        with pytest.raises(InvalidEntry, match="account_type"):
            open_account(db_session, "3000", "Bad", "SAVINGS", "USD")
        with pytest.raises(InvalidEntry, match="currency"):
            open_account(db_session, "3000", "Bad", "ASSET", "DOLLARS")
