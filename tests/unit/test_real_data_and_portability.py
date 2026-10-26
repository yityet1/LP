"""Automated Unit & Integration Tests for Cross-Platform Portability & Real Market Data (2026-10-26)."""
from __future__ import annotations
import sys as _sys, pathlib as _pathlib
_src_dir = str(_pathlib.Path(__file__).resolve().parents[2] / "src")
if _src_dir not in _sys.path:
    _sys.path.insert(0, _src_dir)

from decimal import Decimal
from pathlib import Path
import unittest

from arb.portability import (
    model_to_json,
    normalize_partition_key,
    normalize_sqlite_db_path,
    run_portability_check,
)
from arb.testing.real_data_runner import RealDataBenchmarkRunner

REPO_ROOT = Path(__file__).resolve().parents[2]


class TestRealMarketDataAndPortability(unittest.TestCase):
    def test_portability_audit_all_checks_pass(self) -> None:
        """Verify Python/Pydantic/SQLite/OS path & Decimal portability checks."""
        rep = run_portability_check()
        self.assertTrue(rep.overall_passed)
        self.assertEqual(len(rep.checks), 5)
        for chk in rep.checks:
            self.assertTrue(chk.passed, f"Portability check {chk.check_id} failed")

        # Verify Windows & POSIX SQLite URL normalization
        self.assertEqual(normalize_sqlite_db_path("sqlite:///:memory:"), ":memory:")
        self.assertIn("C:", normalize_sqlite_db_path("sqlite:///C:/arb/data.db"))
        self.assertEqual(
            normalize_partition_key("binance_spot", "2026-10-26", "BTC-USDT"),
            "venue=binance_spot/date=2026-10-26/symbol=BTC-USDT",
        )

    def test_real_market_data_20261026_end_to_end_benchmark(self) -> None:
        """Run all 8 scanners across 6 strategy families on real 2026-10-26 market data."""
        report = RealDataBenchmarkRunner.run_full_real_data_suite(REPO_ROOT)
        self.assertTrue(report["portability"]["overall_passed"])
        self.assertEqual(report["deterministic_replay"]["events_processed"], 15)

        strats = report["strategies"]
        # S1 Cross-CEX Spot: OKX Ask ($80,225.0) -> Binance Bid ($80,430.0)
        self.assertEqual(strats["S1_cross_cex_spot"]["buy_venue"], "okx_spot")
        self.assertEqual(strats["S1_cross_cex_spot"]["sell_venue"], "binance_spot")
        self.assertGreater(Decimal(strats["S1_cross_cex_spot"]["net_edge_bps"]), Decimal("5.0"))

        # S2-A Basis & S2-B Funding Carry
        self.assertGreater(Decimal(strats["S2A_spot_futures_basis"]["net_basis_bps"]), Decimal("100.0"))
        self.assertEqual(Decimal(strats["S2A_spot_futures_basis"]["net_delta_mismatch"]), Decimal("0"))
        self.assertGreater(Decimal(strats["S2B_funding_rate_carry"]["expected_net_carry_usdt"]), Decimal("0"))

        # S3 NVDA Equity Arbitrage (NASDAQ $213.45 Ask -> Tokenized $214.65 Bid)
        self.assertTrue(strats["S3_nvda_equity_arb"]["accepted"])
        self.assertGreater(Decimal(strats["S3_nvda_equity_arb"]["net_edge_bps"]), Decimal("10.0"))

        # S4-A CEX-DEX, S4-B BTC Options Parity, S4-C Cross-Chain Bridge
        self.assertTrue(strats["S4A_cex_dex_uniswap"]["accepted"])
        self.assertTrue(strats["S4B_btc_options_parity"]["accepted"])
        self.assertTrue(strats["S4C_cross_chain_bridge"]["accepted"])
        self.assertTrue(report["portfolio_allocation"]["approved"])


if __name__ == "__main__":
    unittest.main()


class TestLiveMarketDataConnector(unittest.TestCase):
    def test_urllib_http_transport_and_live_feed_over_local_socket(self) -> None:
        """Verify UrllibHttpTransport and LiveMarketDataFeed over a real local TCP HTTP server."""
        import asyncio
        import json
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        from arb.marketdata.live_feed import LiveMarketDataFeed

        class _MockExchangeServer(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                pass

            def do_GET(self) -> None:  # noqa: N802
                if "/api/v3/depth" in self.path:
                    if "BTCUSDT" in self.path:
                        payload = {"lastUpdateId": 1001, "bids": [["80430.0", "1.5"]], "asks": [["80432.0", "1.5"]]}
                    elif "ETHUSDT" in self.path:
                        payload = {"lastUpdateId": 1002, "bids": [["2328.0", "10.0"]], "asks": [["2329.0", "10.0"]]}
                    elif "ETHBTC" in self.path:
                        payload = {"lastUpdateId": 1003, "bids": [["0.02890", "10.0"]], "asks": [["0.02892", "10.0"]]}
                    else:
                        payload = {"lastUpdateId": 1004, "bids": [["735.0", "10.0"]], "asks": [["736.0", "10.0"]]}
                elif "/api/v5/market/books" in self.path:
                    if "BTC-USDT-SWAP" in self.path:
                        payload = {"code": "0", "msg": "", "data": [{"bids": [["81560.0", "200", "0", "5"]], "asks": [["81570.0", "200", "0", "5"]], "seqId": 2001}]}
                    elif "BTC-USDT" in self.path:
                        payload = {"code": "0", "msg": "", "data": [{"bids": [["80580.0", "1.5", "0", "2"]], "asks": [["80590.0", "1.5", "0", "2"]], "seqId": 2002}]}
                    else:
                        payload = {"code": "0", "msg": "", "data": [{"bids": [["2332.0", "10.0", "0", "2"]], "asks": [["2333.0", "10.0", "0", "2"]], "seqId": 2003}]}
                elif "/api/v5/public/funding-rate" in self.path:
                    payload = {"code": "0", "msg": "", "data": [{"instId": "BTC-USDT-SWAP", "fundingRate": "0.0005", "fundingTime": "1793001600000", "nextFundingTime": "1793030400000", "markPx": "81565.0", "idxPx": "80500.0"}]}
                else:
                    payload = {"code": "0", "data": []}
                raw = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        server = HTTPServer(("127.0.0.1", 0), _MockExchangeServer)
        port = server.server_port
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        try:
            base_url = f"http://127.0.0.1:{port}"
            feed = LiveMarketDataFeed(
                binance_base_url=base_url,
                okx_base_url=base_url,
                timeout_ms=3000,
            )
            report = asyncio.run(feed.fetch_and_scan_once())
            self.assertEqual(report["binance_ok_count"], 4)
            self.assertEqual(report["okx_ok_count"], 3)
            self.assertGreaterEqual(len(report["strategies_evaluated"]), 4)
        finally:
            server.shutdown()
            server.server_close()

    def test_ui_http_server_status_and_index_endpoints(self) -> None:
        """Verify run_ui.py / create_ui_http_server serves index.html and /api/status cleanly."""
        import json
        import threading
        import urllib.request
        from arb.ui import create_ui_http_server

        server = create_ui_http_server("127.0.0.1", 0)
        port = server.server_port
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                body = resp.read().decode("utf-8")
                self.assertIn("arb-platform", body.lower())

            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                data = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(data["status"], "OK")
                self.assertIn("checks", data["portability"])
        finally:
            server.shutdown()
            server.server_close()
