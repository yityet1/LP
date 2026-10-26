"""Automated Unit & Integration Tests for Cross-Platform Portability & Real Market Data (2026-10-26)."""
from __future__ import annotations

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
