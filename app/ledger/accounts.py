"""Opening and finding accounts."""

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import Account, AccountType
from app.ledger.errors import AccountExists, AccountNotFound, InvalidEntry


def open_account(
    session: Session, number: str, name: str, account_type: str, currency: str, allow_negative: bool = False
) -> Account:
    try:
        account_type = AccountType(account_type.upper())
    except ValueError:
        raise InvalidEntry(f"account_type must be one of {', '.join(AccountType)}") from None
    if len(currency) != 3 or not currency.isalpha():
        raise InvalidEntry("currency must be a three-letter code such as USD")
    account = Account(
        number=number, name=name, account_type=str(account_type),
        currency=currency.upper(), allow_negative=allow_negative,
    )
    session.add(account)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise AccountExists(f"account {number} already exists") from None
    return account


def get_account(session: Session, number: str) -> Account:
    account = session.scalars(select(Account).where(Account.number == number)).one_or_none()
    if account is None:
        raise AccountNotFound(f"account {number} not found")
    return account
