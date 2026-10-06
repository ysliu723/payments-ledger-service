"""Requests the ledger refuses. Each error carries the HTTP status the API returns for it."""


class LedgerError(Exception):
    status = 422
    code = "LEDGER_ERROR"


class InvalidEntry(LedgerError):
    code = "INVALID_ENTRY"


class AccountNotFound(LedgerError):
    status = 404
    code = "ACCOUNT_NOT_FOUND"


class AccountExists(LedgerError):
    status = 409
    code = "ACCOUNT_EXISTS"


class InsufficientFunds(LedgerError):
    code = "INSUFFICIENT_FUNDS"


class IdempotencyKeyReused(LedgerError):
    status = 409
    code = "IDEMPOTENCY_KEY_REUSED"


class PeriodClosed(LedgerError):
    code = "PERIOD_CLOSED"


class InvalidState(LedgerError):
    status = 409
    code = "INVALID_STATE"
