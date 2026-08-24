"""`make load` -- p50/p99 ingestion latency for POST /webhooks/razorpay, measured against a
real running instance of the app (a real subprocess, a real localhost socket, a real ASGI
event loop), not a synthetic estimate. CLAUDE.md: "measure it, don't assert it" -- the same
instinct the benchmark and the compliance-invariants test already apply, here for the one
externally-facing endpoint that has a documented latency claim (webhooks.py's module
docstring: "ack within 200ms, enqueue").

Spins up its own uvicorn subprocess on a scratch port with a scratch SQLite file and a fixed
webhook secret, fires CONCURRENCY concurrent payment.failed events (each individually signed,
each with a unique x-razorpay-event-id so none collapse into the dedupe path), tears the
subprocess down, and prints the numbers. Nothing here is committed as a claimed number --
run it yourself; this script exists so that number is reproducible, not so it can be quoted
once and left stale.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import httpx

HOST = "127.0.0.1"
PORT = 8931
BASE_URL = f"http://{HOST}:{PORT}"
WEBHOOK_SECRET = "load-test-secret-not-for-real-use"
CONCURRENCY = 50
TOTAL_REQUESTS = 500
STARTUP_TIMEOUT_SECONDS = 30.0

REPO_ROOT = Path(__file__).resolve().parent.parent
VENV_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"


def _signed_payload(event_id: str) -> tuple[bytes, str]:
    body = {
        "event": "payment.failed",
        "payload": {
            "payment": {
                "entity": {
                    "id": f"pay_load_{event_id}",
                    "customer_id": "cust_load_test",
                    "amount": 50000,
                    "error_code": "BAD_REQUEST_ERROR",
                    "error_description": "insufficient funds",
                    "error_source": "customer",
                    "error_step": "payment_authorization",
                    "error_reason": "insufficient_funds",
                }
            }
        },
    }
    raw = json.dumps(body).encode("utf-8")
    signature = hmac.new(WEBHOOK_SECRET.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    return raw, signature


async def _fire_one(client: httpx.AsyncClient) -> tuple[float, int]:
    event_id = str(uuid.uuid4())
    raw, signature = _signed_payload(event_id)
    started = time.perf_counter()
    response = await client.post(
        "/webhooks/razorpay",
        content=raw,
        headers={
            "content-type": "application/json",
            "X-Razorpay-Signature": signature,
            "x-razorpay-event-id": event_id,
        },
    )
    elapsed_ms = (time.perf_counter() - started) * 1000
    return elapsed_ms, response.status_code


async def _run_load(concurrency: int, total: int) -> list[tuple[float, int]]:
    results: list[tuple[float, int]] = []
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10.0) as client:
        semaphore = asyncio.Semaphore(concurrency)

        async def bounded() -> None:
            async with semaphore:
                results.append(await _fire_one(client))

        await asyncio.gather(*(bounded() for _ in range(total)))
    return results


def _wait_for_server(proc: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"server process exited early with code {proc.returncode}")
        try:
            response = httpx.get(f"{BASE_URL}/", timeout=1.0)
            if response.status_code == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(0.2)
    raise TimeoutError(f"server did not become ready within {STARTUP_TIMEOUT_SECONDS}s")


def _percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    index = min(int(len(ordered) * p), len(ordered) - 1)
    return ordered[index]


def main() -> None:
    if not VENV_PYTHON.exists():
        print("no .venv found -- run `make install` first", file=sys.stderr)
        raise SystemExit(1)

    with tempfile.TemporaryDirectory(prefix="vasool_load_") as tmp_dir:
        db_path = str(Path(tmp_dir) / "load_test.db")
        env = {
            **os.environ,
            "RAZORPAY_WEBHOOK_SECRET": WEBHOOK_SECRET,
            "DATABASE_URL": db_path,
            "VASOOL_LLM": "stub",  # the latency claim is about ingestion, not a live LLM call
        }
        proc = subprocess.Popen(
            [str(VENV_PYTHON), "-m", "uvicorn", "vasool.api.main:app", "--host", HOST, "--port", str(PORT)],
            cwd=REPO_ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            print(f"waiting for {BASE_URL} ...")
            _wait_for_server(proc)
            print(f"firing {TOTAL_REQUESTS} requests, concurrency {CONCURRENCY} ...")
            results = asyncio.run(_run_load(CONCURRENCY, TOTAL_REQUESTS))
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                proc.kill()

    latencies = [ms for ms, _ in results]
    statuses = [code for _, code in results]
    non_200 = [code for code in statuses if code != 200]

    print()
    print(f"requests:        {len(results)}")
    print(f"non-200:         {len(non_200)}" + (f" (e.g. {non_200[0]})" if non_200 else ""))
    print(f"mean:            {statistics.mean(latencies):.2f} ms")
    print(f"p50:             {_percentile(latencies, 0.50):.2f} ms")
    print(f"p95:             {_percentile(latencies, 0.95):.2f} ms")
    print(f"p99:             {_percentile(latencies, 0.99):.2f} ms")
    print(f"max:             {max(latencies):.2f} ms")


if __name__ == "__main__":
    main()
