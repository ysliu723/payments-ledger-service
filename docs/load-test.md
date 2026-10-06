# Load Test

Three runs of `python -m scripts.load_test` against the API, each with different
server settings. Same laptop, PostgreSQL in Docker on the same machine, one run
each: treat the numbers as rough and compare the rows with each other.

- Date: 2026-10-05
- Machine: Apple M1 Pro, macOS-26.6.2-arm64-arm-64bit, Python 3.12.15
- Each request: `POST /entries`, a transfer between two of 100 funded wallets, with an
  idempotency key. About 2% are retries of an earlier request with the same key.
  50 requests in flight at a time.
- **spread**: random pairs of wallets, so requests rarely wait for the same lock.
- **hot**: every transfer debits one wallet, so every request waits for that row's lock in turn.

| Server | Scenario | Requests | Requests/s | p50 ms | p95 ms | p99 ms | Replayed retries | HTTP statuses |
|---|---|---|---|---|---|---|---|---|
| 1 process, pool of 15 connections (defaults) | spread | 3,058 | 224 | 215 | 333 | 429 | 58 | 201 × 3,058 |
| | hot | 3,057 | 191 | 200 | 307 | 2,895 | 57 | 201 × 3,057 |
| 1 process, pool of 50 connections | spread | 3,058 | 220 | 206 | 369 | 539 | 58 | 201 × 3,058 |
| | hot | 3,057 | 170 | 78 | 210 | 8,691 | 57 | 201 × 3,057 |
| 4 processes, pool of 15 each | spread | 3,058 | **550** | 81 | 184 | 264 | 58 | 201 × 3,058 |
| | hot | 3,057 | 203 | 24 | 348 | 7,030 | 57 | 201 × 3,057 |

After every run, `GET /reconciliation` passed and the wallets held exactly the money
they were funded with. Every deliberate retry was replayed, none posted twice.

## What the runs show

1. **The connection pool was not the bottleneck.** Raising it from 15 to 50
   connections left throughput unchanged (224 → 220 requests/s).
2. **A single Python process was.** Four processes handled 2.5 times as many
   spread transfers (550 requests/s) with lower latency.
3. **A hot row does not scale with more processes.** Every transfer from the one
   wallet must wait for that row's lock, so throughput stayed near 200 requests/s
   whatever the server settings. More connections only let more requests queue at
   the lock, which made the slowest ones slower (p99 up to 7–9 s).

Fixing a hot account means changing the design, not adding servers: for example,
splitting one busy account into several sub-accounts, or batching its postings.

## Run it yourself

```bash
docker compose up -d db
uvicorn app.main:app --port 8001                                     # row 1
DB_POOL_SIZE=40 DB_MAX_OVERFLOW=10 uvicorn app.main:app --port 8001  # row 2
uvicorn app.main:app --port 8001 --workers 4                         # row 3
python -m scripts.load_test --out /tmp/load-test.md                  # in another terminal
```
