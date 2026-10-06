"""Builders for tests: a bank account, customer wallets, and the usual movements of money.

Accounting view of a payments company: money customers hold with us is a
liability (we owe it to them), and the cash at the bank is an asset.

    Customer deposits 100:   Dr Bank 100      / Cr Wallet 100
    Transfer A -> B of 30:   Dr Wallet A 30   / Cr Wallet B 30
    Customer withdraws 20:   Dr Wallet 20     / Cr Bank 20
"""

from datetime import date
from decimal import Decimal

from app.db.models import EntrySource
from app.ledger.accounts import open_account
from app.ledger.entries import EntryRequest, LineRequest

DAY = date(2026, 10, 5)


def open_bank(session):
    # The clearing account may go negative: money can leave before the bank settles.
    return open_account(session, "1000", "Bank clearing", "ASSET", "USD", allow_negative=True)


def open_wallet(session, number, name=None, currency="USD"):
    return open_account(session, number, name or f"Customer wallet {number}", "LIABILITY", currency)


def entry(*lines, description="test entry", posting_date=DAY, source=EntrySource.SYSTEM):
    """entry(("1000", "100.00", "0"), ("2001", "0", "100.00"))"""
    return EntryRequest(
        posting_date=posting_date,
        description=description,
        lines=tuple(LineRequest(number, Decimal(debit), Decimal(credit)) for number, debit, credit in lines),
        source=source,
    )


def deposit(wallet, amount, **kwargs):
    return entry(("1000", amount, "0"), (wallet, "0", amount), description=f"Deposit to {wallet}", **kwargs)


def transfer(from_wallet, to_wallet, amount, **kwargs):
    return entry(
        (from_wallet, amount, "0"), (to_wallet, "0", amount), description=f"Transfer {from_wallet} -> {to_wallet}", **kwargs
    )


def withdraw(wallet, amount, **kwargs):
    return entry((wallet, amount, "0"), ("1000", "0", amount), description=f"Withdrawal from {wallet}", **kwargs)
