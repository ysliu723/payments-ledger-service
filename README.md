# Payments Ledger Service

A double-entry ledger service that stays correct under retries and concurrent
transfers, and enforces accounting controls at the moment an entry is written.

**Python · FastAPI · PostgreSQL · SQLAlchemy · Kafka (Redpanda) · Docker · pytest**

[![tests](https://github.com/ysliu723/payments-ledger-service/actions/workflows/tests.yml/badge.svg)](https://github.com/ysliu723/payments-ledger-service/actions/workflows/tests.yml)

## Why

Every payments product has a ledger underneath it, and the ledger is where bugs
cost real money. The hard part is not adding numbers; it is staying correct when:

- the network fails and a client **retries** a transfer that already went through;
- two requests **race** for the same balance at the same instant;
- a process **crashes** between saving a transfer and telling other systems about it.

The same bugs show up far from finance. Double-spending a dollar and overselling
the last concert ticket are the same bug.

This service is also the *preventive* half of an audit control set. Its sister
project, the [Journal Entry Risk Platform](https://github.com/ysliu723/journal-entry-risk-platform),
*detects* risky entries after the fact. Here, the violations that are always wrong
are made impossible at write time:

| The risk platform detects | This ledger prevents |
|---|---|
| Unbalanced entries | Rejected by the service, and by a database trigger at commit |
| Duplicate postings | Idempotency keys: a retried request is posted once |
| Entries keyed in after the books closed | Closed periods accept no entries |
| Self-approval / missing approval | Large manual entries wait for a *different* person's approval |
| (Overdrafts) | Row locks: a wallet never goes below zero, even under concurrency |
| (Tampering) | Append-only: posted entries never change; corrections are reversals |

What cannot be prevented, because it is only *sometimes* wrong (a late-night entry,
a round amount), is left to detection. Prevention handles what is always wrong;
detection handles what is only sometimes wrong. A third project, the
[Audit Evidence Agent](https://github.com/ysliu723/audit-evidence-agent), *verifies*
flagged entries against invoices and contracts.

## Results

Measured on a laptop (Apple M1 Pro, PostgreSQL in Docker). Details:
[docs/load-test.md](docs/load-test.md).

| | |
|---|---|
| 50 simultaneous withdrawals of 80.00 from a 1,000.00 wallet | exactly 12 succeed, balance 40.00 (without row locks: all 50 succeeded) |
| 20 identical requests with one idempotency key, at the same instant | posted once; 19 got the original response back |
| Random operations: 300 in sequence, and 8 threads at once | ledger reconciles and the trial balance sums to zero every time |
| Load test, transfers between 100 wallets | 224 requests/s on one API process, **550 on four** (p95 333 → 184 ms) |
| Load test, every transfer from one hot wallet | about 200 requests/s whatever the server: one row lock serializes them |
| About 2% deliberate retries during load tests | all replayed, none posted twice; reconciliation passed after every run |
| Export loaded into the risk platform | trial-balance rollforward passed; a pending entry showed as a sequence gap |

## Quick start

### Everything in Docker

```bash
docker compose up --build
```

This starts PostgreSQL, Redpanda (Kafka-compatible), the API on
http://localhost:8001 (interactive docs at `/docs`), the outbox relay, and the
statement consumer.

```bash
H='Content-Type: application/json'
curl -X POST localhost:8001/accounts -H "$H" \
  -d '{"number": "1000", "name": "Bank clearing", "account_type": "ASSET", "allow_negative": true}'
curl -X POST localhost:8001/accounts -H "$H" \
  -d '{"number": "2001", "name": "Alice wallet", "account_type": "LIABILITY"}'
curl -X POST localhost:8001/entries -H "$H" -H 'X-User: api' -H 'Idempotency-Key: deposit-1' \
  -d '{"posting_date": "2026-10-06", "description": "Deposit",
       "lines": [{"account_number": "1000", "debit": "100.00"},
                 {"account_number": "2001", "credit": "100.00"}]}'
curl localhost:8001/accounts/2001             # normal_balance: "100.00"
curl localhost:8001/accounts/2001/statement   # built from the event a moment later
curl localhost:8001/reconciliation            # every check passes
```

Send the same `POST /entries` again with the same `Idempotency-Key`: you get the
same response with `Idempotent-Replayed: true`, and nothing is posted twice.

### Local development

Requires Python 3.12+ and Docker.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt

docker compose up -d db redpanda
pytest                                  # 90 tests (database and Kafka tests are skipped if those are not running)
uvicorn app.main:app --reload --port 8001
python -m scripts.load_test             # with the API running
```

## How it works

```text
client ──POST /entries + Idempotency-Key──► API (FastAPI)
                                             │  one transaction:
                                             │    idempotency key
                                             │    lock period (share), lock accounts (by id)
                                             │    entry + lines, running balances
                                             │    outbox event
                                             ▼
                                         PostgreSQL ── triggers: balanced at commit, append-only
                                             │
                               relay ◄───────┘  (outbox rows, SKIP LOCKED)
                                 │
                                 ▼
                         Kafka topic ledger.entries
                                 │
                                 ▼
                consumer ── statements (duplicates skipped by event id)
```

| Folder | Responsibility |
|---|---|
| `app/db` | Tables, the database's own guards (triggers, constraints), connections |
| `app/ledger` | Validation, posting with locks, idempotency, periods, approvals and reversals, reports |
| `app/events` | Outbox, relay to Kafka, statement consumer |
| `app/api` | FastAPI routes and JSON shapes |
| `scripts` | Load test |

### Design decisions

- **Locks, always in the same order.** Posting locks the period (shared), then the
  accounts by id (`SELECT ... FOR UPDATE`). The second of two racing withdrawals waits
  and then sees the new balance, and opposite transfers cannot deadlock.
- **Idempotency in the same transaction as the write.** The key, the request's hash,
  and the response are saved with the entry. PostgreSQL's unique index makes a
  simultaneous duplicate wait for the first and then replay its response. Keys are
  scoped per user; a key reused for a different request is refused with 409.
- **The database enforces the core rules too.** A deferred constraint trigger rejects
  any unbalanced entry at commit; triggers make lines and posted entries append-only.
  They hold even if the application is bypassed.
- **Transactional outbox.** Writing to PostgreSQL and to Kafka separately risks a
  transfer without its event or an event without its transfer. The event is written
  to an outbox table in the same transaction, and a relay publishes it after the
  broker acknowledges. Delivery is at-least-once; the consumer records each event id
  with its work, so duplicates have no effect.
- **Segregation of duties.** Manual entries above 10,000 (configurable) are created
  pending and move no money until approved by someone other than the preparer.
  Funds and the period are checked again at approval.
- **Corrections are reversals.** A posted entry never changes; a new entry with
  debits and credits swapped undoes it, once, dated in an open period.
- **Money is exact.** `Decimal` in Python, `NUMERIC(18,2)` in PostgreSQL, strings in JSON.
- **Time zones matter for audits.** Exports use the company's time zone, so the risk
  platform's late-night rule reads them correctly.

## API

Writes need an `X-User` header; `POST /entries` and `POST /entries/{id}/reverse` also
need an `Idempotency-Key`.

| Method | Path | |
|---|---|---|
| POST | `/accounts` | Open an account |
| GET | `/accounts`, `/accounts/{number}` | Balances (`balance` debit-positive; `normal_balance` as people read it) |
| GET | `/accounts/{number}/statement` | Activity built from events (eventually consistent) |
| POST | `/entries` | Post an entry, or create it pending if it needs approval |
| GET | `/entries/{id}` | Lines and audit trail: preparer, approver, reversal links |
| POST | `/entries/{id}/approve`, `/reject`, `/reverse` | Review and correct |
| POST | `/periods/{yyyy-mm}/close`; GET `/periods` | Close a month |
| GET | `/trial-balance?as_of=` | Every account's balance on a date |
| GET | `/reconciliation` | Debits = credits, entries balance, running balances = journal, no overdrafts |
| GET | `/exports/gl-detail.csv`, `/exports/trial-balance.csv` | Input for the risk platform |

Refused requests return `{"error": "...", "detail": "..."}`: 404 not found, 403
segregation of duties, 409 conflict (idempotency key reused, entry not pending,
already reversed, period already closed), 422 invalid entry, insufficient funds, or
period closed.

## Limitations

- **No authentication.** `X-User` is trusted; a real system would take the user from a token.
- **Hot accounts serialize.** Every posting to one account waits for its row lock;
  measured at about 200 requests/s. Fixing it means splitting the account or batching.
- **Idempotency keys never expire**, and only successful responses are saved.
- **Statements are eventually consistent**, and the outbox is never cleaned up.
- **One currency per entry**; no foreign exchange.
- Tables are created at startup instead of with migrations.

## Roadmap

- Optimistic locking (a version column) compared with row locks under contention.
- Sub-accounts or batching for hot accounts.
- Expiring idempotency keys; outbox cleanup.
- Authentication, and multi-currency entries.
