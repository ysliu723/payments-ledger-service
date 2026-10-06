"""Fire concurrent transfers at the running API, then check the ledger still reconciles.

    docker compose up -d db
    uvicorn app.main:app --port 8001
    python -m scripts.load_test

Two scenarios:
- spread: transfers between random pairs of 100 wallets (little lock contention)
- hot:    every transfer takes money from the same wallet (every request waits for one lock)

About 2% of requests are deliberate retries with the same idempotency key, to
exercise idempotency under load. Writes docs/load-test.md.
"""

import argparse
import asyncio
import platform
import random
import statistics
import subprocess
import sys
import time
import uuid
from collections import Counter
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx2 as httpx

DAY = date.today().isoformat()


def lines(*items):
    return [{"account_number": number, "debit": debit, "credit": credit} for number, debit, credit in items]


async def post_entry(client, entry_lines, description, key):
    body = {"posting_date": DAY, "description": description, "lines": entry_lines}
    start = time.perf_counter()
    response = await client.post("/entries", json=body, headers={"X-User": "load-test", "Idempotency-Key": key})
    return response, (time.perf_counter() - start) * 1000


async def setup(client, prefix, wallet_count, funding):
    bank = f"{prefix}-BANK"
    await client.post("/accounts", json={"number": bank, "name": "Bank clearing", "account_type": "ASSET",
                                         "allow_negative": True})
    wallets = [f"{prefix}-{n:03d}" for n in range(wallet_count)]
    for number in wallets:
        await client.post("/accounts", json={"number": number, "name": f"Wallet {number}", "account_type": "LIABILITY"})
        response, _ = await post_entry(client, lines((bank, funding, "0"), (number, "0", funding)), "Funding",
                                       str(uuid.uuid4()))
        response.raise_for_status()
    return wallets


async def run_scenario(base_url, name, requests, concurrency, wallet_count, hot, seed):
    rng = random.Random(seed)
    prefix = f"LT{uuid.uuid4().hex[:6]}"
    funding = "100000.00"
    limits = httpx.Limits(max_connections=concurrency)
    async with httpx.AsyncClient(base_url=base_url, timeout=60, limits=limits) as client:
        wallets = await setup(client, prefix, wallet_count, funding)

        jobs = []
        for _ in range(requests):
            source, target = (wallets[0], rng.choice(wallets[1:])) if hot else rng.sample(wallets, 2)
            amount = f"{rng.randint(1, 50)}.{rng.randint(0, 99):02d}"
            key = str(uuid.uuid4())
            jobs.append((lines((source, amount, "0"), (target, "0", amount)), key))
            if rng.random() < 0.02:
                jobs.append(jobs[-1])  # a client retry with the same key

        semaphore = asyncio.Semaphore(concurrency)
        latencies, statuses, replays = [], Counter(), 0

        async def send(job):
            nonlocal replays
            entry_lines, key = job
            async with semaphore:
                response, ms = await post_entry(client, entry_lines, "Load test transfer", key)
            latencies.append(ms)
            statuses[response.status_code] += 1
            replays += response.headers.get("Idempotent-Replayed") == "true"

        started = time.perf_counter()
        await asyncio.gather(*(send(job) for job in jobs))
        elapsed = time.perf_counter() - started

        reconciliation = (await client.get("/reconciliation")).json()
        balances = [Decimal((await client.get(f"/accounts/{number}")).json()["normal_balance"]) for number in wallets]

    expected_total = Decimal(funding) * wallet_count  # transfers only move money between these wallets
    return {
        "scenario": name,
        "requests": len(jobs),
        "concurrency": concurrency,
        "seconds": elapsed,
        "per_second": len(jobs) / elapsed,
        "p50": statistics.median(latencies),
        "p95": statistics.quantiles(latencies, n=20)[-1],
        "p99": statistics.quantiles(latencies, n=100)[-1],
        "statuses": dict(statuses),
        "replays": replays,
        "reconciled": reconciliation["ok"],
        "money_conserved": sum(balances) == expected_total,
    }


def environment() -> str:
    cpu = platform.processor() or platform.machine()
    if sys.platform == "darwin":
        cpu = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True).stdout.strip()
    return f"{cpu}, {platform.platform()}, Python {platform.python_version()}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Load test the ledger API.")
    parser.add_argument("--url", default="http://localhost:8001")
    parser.add_argument("--requests", type=int, default=3000)
    parser.add_argument("--concurrency", type=int, default=50)
    parser.add_argument("--out", type=Path, default=Path("docs/load-test.md"))
    args = parser.parse_args()

    results = []
    for name, hot in (("spread", False), ("hot", True)):
        print(f"running {name} ...", flush=True)
        results.append(asyncio.run(run_scenario(args.url, name, args.requests, args.concurrency, 100, hot, seed=7)))

    rows = [
        f"| {r['scenario']} | {r['requests']:,} | {r['concurrency']} | {r['per_second']:,.0f} | {r['p50']:.0f} | "
        f"{r['p95']:.0f} | {r['p99']:.0f} | {r['replays']} | {r['statuses']} | "
        f"{'yes' if r['reconciled'] else 'NO'} | {'yes' if r['money_conserved'] else 'NO'} |"
        for r in results
    ]
    report = "\n".join([
        "# Load Test",
        "",
        "Produced by `python -m scripts.load_test` against one API process (`uvicorn app.main:app --port 8001`) "
        "and PostgreSQL in Docker on the same laptop. One run: treat the numbers as rough.",
        "",
        f"- Date: {date.today().isoformat()}",
        f"- Machine: {environment()}",
        "- Each request: POST /entries, a transfer between two of 100 funded wallets, with an idempotency key. "
        "About 2% are retries of an earlier request with the same key.",
        "",
        "| Scenario | Requests | Concurrency | Requests/s | p50 ms | p95 ms | p99 ms | Replayed retries | "
        "HTTP statuses | Reconciles | Money conserved |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
        *rows,
        "",
        "- **spread**: random pairs of wallets, so requests rarely wait for the same lock.",
        "- **hot**: every transfer debits one wallet, so every request waits for that row's lock in turn.",
        "- **Reconciles**: `GET /reconciliation` passed afterwards. **Money conserved**: the wallets still hold "
        "exactly what they were funded with.",
        "",
    ])
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(report, encoding="utf-8")
    print(report)


if __name__ == "__main__":
    main()
