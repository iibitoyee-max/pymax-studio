#!/usr/bin/env python3
"""
PryMax Studio — load test
===========================
A real load-testing tool, not a stub — uses `requests` and a thread pool
to generate genuine concurrent HTTP traffic against a running instance of
app.py and reports actual latency percentiles and throughput. No mocking:
every number this prints comes from a real request that actually hit the
server.

Two modes:

  --mode capacity   Hammers one cheap read endpoint (/api/health) with
                     increasing concurrency to find where this specific
                     process/hardware combination starts degrading —
                     the raw ceiling, with rate limiting bypassed by
                     hitting a route that isn't rate-limited at all.

  --mode realistic  Simulates N "clients" behaving like real room.js
                     clients: join once, then repeatedly poll chat/polls/
                     Q&A/reactions like the frontend actually does,
                     respecting the app's real rate limits (this mode
                     will show 429s once N is high enough — that's
                     correct behavior, not a bug in the tool).

Usage:
    python3 loadtest.py --mode capacity --max-concurrency 200
    python3 loadtest.py --mode realistic --clients 20 --duration 15

Requires the server already running (python3 app.py in another process)
— this tool doesn't start or manage the server itself, matching how a
real load test against a real deployment would work.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import statistics
import time
import uuid

import requests

BASE_URL = "http://localhost:5000"


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    idx = min(int(len(values) * pct), len(values) - 1)
    return values[idx]


def report(label: str, latencies_ms: list[float], errors: int, elapsed_s: float) -> None:
    total = len(latencies_ms) + errors
    print(f"\n--- {label} ---")
    print(f"requests: {total}  errors: {errors}  elapsed: {elapsed_s:.2f}s")
    if latencies_ms:
        print(f"throughput: {total / elapsed_s:.1f} req/s")
        print(
            f"latency ms — min: {min(latencies_ms):.1f}  "
            f"p50: {percentile(latencies_ms, 0.50):.1f}  "
            f"p95: {percentile(latencies_ms, 0.95):.1f}  "
            f"p99: {percentile(latencies_ms, 0.99):.1f}  "
            f"max: {max(latencies_ms):.1f}"
        )


def _timed_get(path: str) -> tuple[float | None, int | None]:
    start = time.monotonic()
    try:
        res = requests.get(f"{BASE_URL}{path}", timeout=10)
        elapsed_ms = (time.monotonic() - start) * 1000
        return elapsed_ms, res.status_code
    except requests.RequestException:
        return None, None


def run_capacity_test(max_concurrency: int, requests_per_level: int) -> None:
    """Ramps concurrency on a cheap, unthrottled endpoint until latency or
    error rate clearly degrades, to find this specific process's ceiling."""
    print(f"=== Capacity test against {BASE_URL}/api/health ===")
    levels = [1, 5, 10, 25, 50, 100, max_concurrency]
    levels = sorted(set(l for l in levels if l <= max_concurrency))

    for level in levels:
        latencies = []
        errors = 0
        start = time.monotonic()
        with concurrent.futures.ThreadPoolExecutor(max_workers=level) as executor:
            futures = [executor.submit(_timed_get, "/api/health") for _ in range(requests_per_level)]
            for f in concurrent.futures.as_completed(futures):
                ms, status = f.result()
                if ms is None or status != 200:
                    errors += 1
                else:
                    latencies.append(ms)
        elapsed = time.monotonic() - start
        report(f"concurrency={level}", latencies, errors, elapsed)


def _client_session(client_id: int, duration_s: float, stats: dict, stats_lock) -> None:
    """One simulated real client: join, then poll several endpoints
    repeatedly like room.js actually does, for `duration_s` seconds."""
    room = f"loadtest-{uuid.uuid4().hex[:8]}"
    session = requests.Session()
    end_time = time.monotonic() + duration_s

    try:
        join = session.post(f"{BASE_URL}/api/room/{room}/join", json={"name": f"user{client_id}"}, timeout=10)
        if join.status_code != 200:
            with stats_lock:
                stats["join_failures"] += 1
            return
        join_data = join.json()
        peer_id, peer_secret = join_data["peer_id"], join_data["peer_secret"]
    except Exception:
        with stats_lock:
            stats["join_failures"] += 1
        return

    endpoints = [
        ("GET", f"/api/room/{room}/chat?since=0", None),
        ("GET", f"/api/room/{room}/poll", None),
        ("GET", f"/api/room/{room}/qa", None),
        ("GET", f"/api/room/{room}/reaction?since=0", None),
        ("POST", f"/api/room/{room}/chat", {"name": f"user{client_id}", "message": "load test message"}),
    ]

    while time.monotonic() < end_time:
        for method, path, body in endpoints:
            start = time.monotonic()
            try:
                if method == "GET":
                    res = session.get(f"{BASE_URL}{path}", timeout=10)
                else:
                    res = session.post(f"{BASE_URL}{path}", json=body, timeout=10)
                elapsed_ms = (time.monotonic() - start) * 1000
                with stats_lock:
                    if res.status_code == 429:
                        stats["rate_limited"] += 1
                    elif res.status_code >= 400:
                        stats["errors"] += 1
                    else:
                        stats["latencies"].append(elapsed_ms)
            except requests.RequestException:
                with stats_lock:
                    stats["errors"] += 1
        time.sleep(1.0)  # roughly matches the frontend's real poll cadence


def run_realistic_test(num_clients: int, duration_s: float) -> None:
    import threading

    print(f"=== Realistic simulation: {num_clients} concurrent clients for {duration_s:.0f}s ===")
    stats = {"latencies": [], "errors": 0, "rate_limited": 0, "join_failures": 0}
    stats_lock = threading.Lock()

    start = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=num_clients) as executor:
        futures = [
            executor.submit(_client_session, i, duration_s, stats, stats_lock)
            for i in range(num_clients)
        ]
        concurrent.futures.wait(futures)
    elapsed = time.monotonic() - start

    print(f"\n--- {num_clients} clients over {elapsed:.1f}s ---")
    print(f"join failures (often /join's own rate limit — 10/60s per source IP): {stats['join_failures']}")
    print(f"successful requests: {len(stats['latencies'])}")
    print(f"rate-limited (429): {stats['rate_limited']}  (expected once traffic exceeds real limits)")
    print(f"other errors: {stats['errors']}")
    if stats["latencies"]:
        lat = stats["latencies"]
        print(
            f"latency ms — p50: {percentile(lat, 0.50):.1f}  "
            f"p95: {percentile(lat, 0.95):.1f}  "
            f"p99: {percentile(lat, 0.99):.1f}  max: {max(lat):.1f}"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=["capacity", "realistic"], default="capacity")
    parser.add_argument("--max-concurrency", type=int, default=100)
    parser.add_argument("--requests-per-level", type=int, default=100)
    parser.add_argument("--clients", type=int, default=20)
    parser.add_argument("--duration", type=float, default=15.0)
    args = parser.parse_args()

    try:
        requests.get(f"{BASE_URL}/api/health", timeout=3)
    except requests.RequestException:
        print(f"ERROR: no server responding at {BASE_URL} — start it first with `python3 app.py`")
        raise SystemExit(1)

    if args.mode == "capacity":
        run_capacity_test(args.max_concurrency, args.requests_per_level)
    else:
        run_realistic_test(args.clients, args.duration)
