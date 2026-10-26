#!/usr/bin/env python3
"""Cross-platform Live Real-Time Market Data CLI Runner for Windows, macOS, and Linux.

Connects to live Binance (`https://data-api.binance.vision` / `https://api.binance.com`)
and OKX (`https://www.okx.com`) public L2 order books & funding rates and runs the
S1-A, S1-B, S2-A, and S2-B arbitrage scanners in real time.

Usage on Windows (cmd.exe or PowerShell):
    python run_live_feed.py
    python run_live_feed.py --proxy http://127.0.0.1:7890 --loops 5 --interval 3
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
SRC_DIR = str(REPO_ROOT / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from arb.marketdata.live_feed import LiveMarketDataFeed  # noqa: E402


async def _run_loop(proxy: str | None, loops: int, interval_sec: float, timeout_ms: int) -> None:
    feed = LiveMarketDataFeed(proxy_url=proxy, timeout_ms=timeout_ms)
    for i in range(1, loops + 1):
        print(f"\n[Tick #{i}/{loops}] Pulling live L2 order books from Binance & OKX...")
        snap = await feed.fetch_and_scan_once()
        print(
            f"Timestamp: {snap['timestamp_utc']} | Connected Books: {snap['connected_books_count']} "
            f"(Binance={snap['binance_ok_count']}, OKX={snap['okx_ok_count']})"
        )
        if snap["live_quotes"]:
            print("--- Live Top-of-Book Quotes ---")
            for k, q in snap["live_quotes"].items():
                extra = f" | funding_8h={q['funding_rate_8h']}" if "funding_rate_8h" in q else ""
                print(f"  {k:<26} Bid: {q['best_bid']:<12} Ask: {q['best_ask']:<12}{extra}")
        else:
            print("WARNING: Could not reach Binance/OKX direct endpoints from your network.")
            print("If you are in a region requiring a proxy (e.g. Clash/v2ray), pass --proxy http://127.0.0.1:7890")
            for k, st in snap["venue_status"].items():
                if st.get("status") == "ERROR":
                    print(f"  [{k}] {st.get('error')}")

        if snap["strategies_evaluated"]:
            print("--- Real-Time Arbitrage Scanner Evaluations ---")
            for s in snap["strategies_evaluated"]:
                print(
                    f"  [{s['decision']:<8}] {s['strategy']:<42} | Pair: {s['pair']:<24} "
                    f"| Net Edge: {s['net_edge_bps']:>7} bps | Est. Net PnL: ${s['expected_net_pnl_usdt']}"
                )
        if i < loops:
            time.sleep(interval_sec)


def main() -> None:
    parser = argparse.ArgumentParser(description="ARB-PLATFORM Real-Time Market Data Feed & Arbitrage Scanner")
    parser.add_argument(
        "--proxy",
        default=None,
        help="Optional HTTP/HTTPS proxy URL (e.g. http://127.0.0.1:7890) if required by your network",
    )
    parser.add_argument("--loops", type=int, default=1, help="Number of polling cycles to run (default: 1)")
    parser.add_argument("--interval", type=float, default=3.0, help="Seconds between cycles (default: 3.0)")
    parser.add_argument("--timeout-ms", type=int, default=5000, help="HTTP request timeout in ms (default: 5000)")
    parser.add_argument("--json", action="store_true", help="Print full JSON report")
    args = parser.parse_args()

    if args.json:
        feed = LiveMarketDataFeed(proxy_url=args.proxy, timeout_ms=args.timeout_ms)
        res = asyncio.run(feed.fetch_and_scan_once())
        print(json.dumps(res, indent=2, ensure_ascii=False))
    else:
        asyncio.run(_run_loop(args.proxy, args.loops, args.interval, args.timeout_ms))


if __name__ == "__main__":
    main()
