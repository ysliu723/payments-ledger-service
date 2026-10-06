"""Run a write at most once per idempotency key.

Clients send an Idempotency-Key with every write. If the network drops the
response after the entry was posted, the client retries with the same key
and gets the original response back, instead of a second posting.

Two identical requests at the same instant: the key is saved in the same
transaction as the write. PostgreSQL's unique index makes the second
transaction wait until the first one finishes. If the first committed, the
second gets a duplicate-key error, rolls back, and replays the saved response.

Only successful responses are saved. A refused request (say, insufficient
funds) leaves no trace, so the client may retry it with the same key.
"""

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import IdempotencyRecord
from app.ledger.errors import IdempotencyKeyReused


@dataclass(frozen=True)
class Response:
    status: int
    body: dict
    replayed: bool = False


def fingerprint(method: str, path: str, body: dict) -> str:
    """A hash of the request, to tell a genuine retry from a different request reusing a key."""
    canonical = json.dumps({"method": method, "path": path, "body": body}, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def run_once(session: Session, key: str, request_hash: str, operation: Callable[[], tuple[int, dict]]) -> Response:
    """Run `operation` and commit, unless this key was already used: then replay the saved response.

    `operation` does the write and returns (status, body). If it raises, the
    caller must roll back; nothing about the key is saved.
    """
    record = IdempotencyRecord(key=key, request_hash=request_hash)
    session.add(record)
    try:
        session.flush()  # waits here if another transaction is saving the same key right now
    except IntegrityError:
        session.rollback()
        return _replay(session, key, request_hash)

    status, body = operation()
    record.response_status = status
    record.response_body = body
    session.commit()
    return Response(status, body)


def _replay(session: Session, key: str, request_hash: str) -> Response:
    record = session.get(IdempotencyRecord, key)
    if record.request_hash != request_hash:
        raise IdempotencyKeyReused(f"idempotency key {key!r} was already used for a different request")
    return Response(record.response_status, record.response_body, replayed=True)
