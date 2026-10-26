"""Live Real-Time Market Data Connector & Multi-Strategy Arbitrage Scanner Runner.

Connects to real-time public REST L2 OrderBook & Funding Rate APIs on:
- Binance Spot (`https://data-api.binance.vision` / `https://api.binance.com`)
- OKX V5 Spot (`https://www.okx.com/api/v5/market/books`)
- OKX V5 USDT-M Perpetual Swap (`https://www.okx.com/api/v5/market/books` & `/api/v5/public/funding-rate`)

Feeds live L2 depth snapshots into `OrderBookBuilder` and runs:
1. `S1-A` Binance Spot Triangle Arbitrage (`USDT -> BTC -> ETH -> USDT` & reverse)
2. `S1-B` Cross-CEX Spot Arbitrage (`Binance BTC-USDT <-> OKX BTC-USDT`, `ETH-USDT`)
3. `S2-A` Spot-Perpetual Basis Arbitrage (`OKX BTC-USDT Spot vs BTC-USDT-SWAP`)
4. `S2-B` Perpetual Funding Rate Carry (`OKX BTC-USDT-SWAP`)
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from arb.config.settings import RiskPolicyConfig
from arb.costs.engine import CostEngine, FeeSchedule
from arb.domain.models import (
    BalanceSnapshot,
    Instrument,
    InstrumentType,
    TradingStatus,
)
from arb.instruments.registry import InstrumentRegistry
from arb.marketdata.health import MarketHealthGate
from arb.marketdata.orderbook import OrderBookBuilder
from arb.strategies.basis.scanner import BasisArbitrageScanner, BasisPairSpec
from arb.strategies.cross_cex_spot.scanner import (
    CrossCexPairMapping,
    CrossCexSpotScanner,
)
from arb.strategies.funding.scanner import (
    FundingCarryPairSpec,
    FundingRateArbitrageScanner,
)
from arb.strategies.triangle.scanner import TriangleArbitrageScanner
from arb.strategy.lifecycle import OpportunityLifecycleManager
from arb.venues.binance_spot.rest import BinanceSpotRestAdapter
from arb.venues.derivatives.adapters import (
    DerivativeFundingSnapshot,
    OkxSwapDerivativesAdapter,
)
from arb.venues.live_transport import UrllibHttpTransport
from arb.venues.okx_spot.rest import OkxSpotRestAdapter


REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_RISK_YAML = REPO_ROOT / "configs" / "risk-policy.example.yaml"


class LiveMarketDataFeed:
    """Pulls live L2 order books and funding rates from Binance & OKX and evaluates arbitrage opportunities."""

    def __init__(
        self,
        proxy_url: Optional[str] = None,
        timeout_ms: int = 5000,
        binance_base_url: str = "https://data-api.binance.vision",
        okx_base_url: str = "https://www.okx.com",
    ) -> None:
        self._proxy_url = proxy_url
        self._transport = UrllibHttpTransport(proxy_url=proxy_url)
        self._binance_spot = BinanceSpotRestAdapter(
            transport=self._transport,
            base_url=binance_base_url,
            timeout_ms=timeout_ms,
            read_only_mode=True,
        )
        self._okx_spot = OkxSpotRestAdapter(
            transport=self._transport,
            base_url=okx_base_url,
            timeout_ms=timeout_ms,
            read_only_mode=True,
        )
        self._okx_swap = OkxSwapDerivativesAdapter(
            transport=self._transport,
            base_url=okx_base_url,
            timeout_ms=timeout_ms,
        )

    async def fetch_and_scan_once(self) -> Dict[str, Any]:
        """Fetch live L2 order books from Binance & OKX and run S1-A, S1-B, S2-A, S2-B scanners."""
        now_utc = datetime.now(timezone.utc)
        policy = RiskPolicyConfig.from_yaml(DEFAULT_RISK_YAML)
        # Allow slightly wider cross-venue skew (2500ms) and book age (5000ms) for public REST polling over internet
        live_policy = policy.copy(
            update={
                "max_book_age_ms": 5000,
                "max_cross_venue_skew_ms": 3000,
            }
        )

        registry = InstrumentRegistry()
        self._register_standard_instruments(registry, now_utc)

        order_books: Dict[Tuple[str, str], OrderBookBuilder] = {}
        binance_books: Dict[str, OrderBookBuilder] = {}
        venue_status: Dict[str, Any] = {}
        live_quotes: Dict[str, Any] = {}

        # 1. Fetch Binance Spot L2 OrderBooks concurrently
        binance_pairs = ["BTC-USDT", "ETH-USDT", "ETH-BTC", "BNB-USDT"]
        b_tasks = [self._binance_spot.fetch_order_book(sym, depth=20) for sym in binance_pairs]
        b_results = await asyncio.gather(*b_tasks, return_exceptions=True)

        binance_ok_count = 0
        for sym, res in zip(binance_pairs, b_results):
            if isinstance(res, Exception):
                venue_status[f"binance_spot:{sym}"] = {"status": "ERROR", "error": str(res)}
            else:
                binance_ok_count += 1
                builder = OrderBookBuilder("binance_spot", sym)
                builder.apply_snapshot(
                    bids=res.bids,
                    asks=res.asks,
                    last_update_id=res.last_update_id,
                    event_time_utc=now_utc,
                    receive_time_utc=now_utc,
                )
                order_books[("binance_spot", sym)] = builder
                binance_books[sym] = builder
                best_b = res.bids[0].price if res.bids else Decimal("0")
                best_a = res.asks[0].price if res.asks else Decimal("0")
                live_quotes[f"binance_spot:{sym}"] = {
                    "best_bid": str(best_b),
                    "best_ask": str(best_a),
                    "spread_bps": str(
                        ((best_a - best_b) / best_b * Decimal("10000")).quantize(Decimal("0.01"))
                        if best_b > 0
                        else Decimal("0")
                    ),
                    "last_update_id": res.last_update_id,
                }
                venue_status[f"binance_spot:{sym}"] = {"status": "OK", "levels": len(res.bids)}

        # 2. Fetch OKX Spot & Swap L2 OrderBooks + Funding Rate concurrently
        okx_spot_pairs = ["BTC-USDT", "ETH-USDT"]
        o_spot_tasks = [self._okx_spot.fetch_order_book(sym, depth=20) for sym in okx_spot_pairs]
        o_swap_book_task = self._okx_swap.fetch_order_book("BTC-USDT-PERP", depth=20)
        o_swap_fund_task = self._okx_swap.fetch_funding_and_mark("BTC-USDT-PERP")

        o_results = await asyncio.gather(
            *o_spot_tasks, o_swap_book_task, o_swap_fund_task, return_exceptions=True
        )

        okx_ok_count = 0
        for sym, res in zip(okx_spot_pairs, o_results[:2]):
            if isinstance(res, Exception):
                venue_status[f"okx_spot:{sym}"] = {"status": "ERROR", "error": str(res)}
            else:
                okx_ok_count += 1
                builder = OrderBookBuilder("okx_spot", sym)
                builder.apply_snapshot(
                    bids=res.bids,
                    asks=res.asks,
                    last_update_id=res.last_update_id,
                    event_time_utc=now_utc,
                    receive_time_utc=now_utc,
                )
                order_books[("okx_spot", sym)] = builder
                best_b = res.bids[0].price if res.bids else Decimal("0")
                best_a = res.asks[0].price if res.asks else Decimal("0")
                live_quotes[f"okx_spot:{sym}"] = {
                    "best_bid": str(best_b),
                    "best_ask": str(best_a),
                    "spread_bps": str(
                        ((best_a - best_b) / best_b * Decimal("10000")).quantize(Decimal("0.01"))
                        if best_b > 0
                        else Decimal("0")
                    ),
                    "last_update_id": res.last_update_id,
                }
                venue_status[f"okx_spot:{sym}"] = {"status": "OK", "levels": len(res.bids)}

        swap_book_res = o_results[2]
        swap_fund_res = o_results[3]
        if not isinstance(swap_book_res, Exception):
            okx_ok_count += 1
            s_builder = OrderBookBuilder("okx_swap", "BTC-USDT-PERP")
            s_builder.apply_snapshot(
                bids=swap_book_res.bids,
                asks=swap_book_res.asks,
                last_update_id=swap_book_res.last_update_id,
                event_time_utc=now_utc,
                receive_time_utc=now_utc,
            )
            order_books[("okx_swap", "BTC-USDT-PERP")] = s_builder
            best_b = swap_book_res.bids[0].price if swap_book_res.bids else Decimal("0")
            best_a = swap_book_res.asks[0].price if swap_book_res.asks else Decimal("0")
            live_quotes["okx_swap:BTC-USDT-PERP"] = {
                "best_bid": str(best_b),
                "best_ask": str(best_a),
                "last_update_id": swap_book_res.last_update_id,
            }
            venue_status["okx_swap:BTC-USDT-PERP"] = {"status": "OK", "levels": len(swap_book_res.bids)}
        else:
            venue_status["okx_swap:BTC-USDT-PERP"] = {"status": "ERROR", "error": str(swap_book_res)}

        # 3. Run Arbitrage Scanners on whichever live books succeeded
        health_gate = MarketHealthGate(risk_policy=live_policy)
        cost_engine = CostEngine(
            fee_schedules={
                "binance_spot": FeeSchedule(
                    venue_id="binance_spot",
                    tier_name="VIP0_BNB",
                    maker_fee_rate=Decimal("0.00075"),
                    taker_fee_rate=Decimal("0.00075"),
                ),
                "okx_spot": FeeSchedule(
                    venue_id="okx_spot",
                    tier_name="VIP1",
                    maker_fee_rate=Decimal("0.0006"),
                    taker_fee_rate=Decimal("0.0008"),
                ),
                "okx_swap": FeeSchedule(
                    venue_id="okx_swap",
                    tier_name="VIP1_SWAP",
                    maker_fee_rate=Decimal("0.0002"),
                    taker_fee_rate=Decimal("0.0005"),
                ),
            }
        )
        lifecycle = OpportunityLifecycleManager()
        strategies_output: List[Dict[str, Any]] = []

        # (A) Binance Triangle Arbitrage
        if all(k in binance_books for k in ("BTC-USDT", "ETH-USDT", "ETH-BTC")):
            tri_scanner = TriangleArbitrageScanner(
                venue_id="binance_spot",
                settlement_asset="USDT",
                registry=registry,
                health_gate=health_gate,
                cost_engine=cost_engine,
                lifecycle=lifecycle,
                risk_policy=live_policy,
                initial_capital_quote=Decimal("1000.00"),
            )
            tri_scanner.build_graph(
                [
                    registry.get("binance_spot", "BTC-USDT"),
                    registry.get("binance_spot", "ETH-USDT"),
                    registry.get("binance_spot", "ETH-BTC"),
                ]
            )
            _, all_tri = tri_scanner.scan_all(binance_books, now_utc=now_utc)
            for opp in all_tri:
                strategies_output.append(
                    {
                        "strategy": "S1-A Binance Spot Triangle",
                        "pair": "USDT -> BTC -> ETH -> USDT",
                        "decision": opp.decision.value.upper(),
                        "net_edge_bps": str(opp.net_edge_bps.quantize(Decimal("0.01"))),
                        "expected_net_pnl_usdt": str(opp.expected_net_pnl.quantize(Decimal("0.01"))),
                        "reject_reasons": opp.reject_reason_codes,
                    }
                )

        # (B) Cross-CEX Spot Arbitrage (Binance <-> OKX BTC-USDT & ETH-USDT)
        sim_balances = {
            ("binance_spot", "USDT"): BalanceSnapshot(
                venue_id="binance_spot",
                account_id="live_sim",
                asset="USDT",
                total_balance=Decimal("50000"),
                free_balance=Decimal("50000"),
                event_time_utc=now_utc,
            ),
            ("binance_spot", "BTC"): BalanceSnapshot(
                venue_id="binance_spot",
                account_id="live_sim",
                asset="BTC",
                total_balance=Decimal("1.0"),
                free_balance=Decimal("1.0"),
                event_time_utc=now_utc,
            ),
            ("binance_spot", "ETH"): BalanceSnapshot(
                venue_id="binance_spot",
                account_id="live_sim",
                asset="ETH",
                total_balance=Decimal("15.0"),
                free_balance=Decimal("15.0"),
                event_time_utc=now_utc,
            ),
            ("okx_spot", "USDT"): BalanceSnapshot(
                venue_id="okx_spot",
                account_id="live_sim",
                asset="USDT",
                total_balance=Decimal("50000"),
                free_balance=Decimal("50000"),
                event_time_utc=now_utc,
            ),
            ("okx_spot", "BTC"): BalanceSnapshot(
                venue_id="okx_spot",
                account_id="live_sim",
                asset="BTC",
                total_balance=Decimal("1.0"),
                free_balance=Decimal("1.0"),
                event_time_utc=now_utc,
            ),
            ("okx_spot", "ETH"): BalanceSnapshot(
                venue_id="okx_spot",
                account_id="live_sim",
                asset="ETH",
                total_balance=Decimal("15.0"),
                free_balance=Decimal("15.0"),
                event_time_utc=now_utc,
            ),
        }
        cross_scanner = CrossCexSpotScanner(
            registry=registry,
            health_gate=health_gate,
            cost_engine=cost_engine,
            lifecycle=lifecycle,
            risk_policy=live_policy,
        )
        for pair_sym, qty in [("BTC-USDT", Decimal("0.0100")), ("ETH-USDT", Decimal("0.200"))]:
            if ("binance_spot", pair_sym) in order_books and ("okx_spot", pair_sym) in order_books:
                _, reps = cross_scanner.scan_pair(
                    mapping=CrossCexPairMapping(
                        canonical_pair=pair_sym.replace("-", "/"),
                        venue_a_id="binance_spot",
                        venue_a_instrument_id=pair_sym,
                        venue_b_id="okx_spot",
                        venue_b_instrument_id=pair_sym,
                        target_base_qty=qty,
                    ),
                    order_books=order_books,
                    balances=sim_balances,
                    now_utc=now_utc,
                )
                best_rep = max(reps, key=lambda r: r.opportunity.expected_net_pnl)
                strategies_output.append(
                    {
                        "strategy": f"S1-B Cross-CEX Spot ({best_rep.buy_venue_id} -> {best_rep.sell_venue_id})",
                        "pair": pair_sym,
                        "decision": best_rep.opportunity.decision.value.upper(),
                        "net_edge_bps": str(best_rep.opportunity.net_edge_bps.quantize(Decimal("0.01"))),
                        "expected_net_pnl_usdt": str(best_rep.opportunity.expected_net_pnl.quantize(Decimal("0.01"))),
                        "reject_reasons": best_rep.opportunity.reject_reason_codes,
                    }
                )

        # (C) OKX Spot vs Swap Basis & Funding Carry
        if ("okx_spot", "BTC-USDT") in order_books and ("okx_swap", "BTC-USDT-PERP") in order_books:
            basis_scanner = BasisArbitrageScanner(
                registry=registry,
                health_gate=health_gate,
                cost_engine=cost_engine,
                lifecycle=lifecycle,
                risk_policy=live_policy,
            )
            b_rep = basis_scanner.evaluate_basis(
                spec=BasisPairSpec(
                    pair_id="LIVE_OKX_BTC_BASIS",
                    spot_venue_id="okx_spot",
                    spot_instrument_id="BTC-USDT",
                    deriv_venue_id="okx_swap",
                    deriv_instrument_id="BTC-USDT-PERP",
                    target_base_qty=Decimal("0.01"),
                ),
                order_books=order_books,
                now_utc=now_utc,
            )
            strategies_output.append(
                {
                    "strategy": "S2-A OKX Spot-Perp Basis",
                    "pair": "BTC-USDT vs BTC-USDT-SWAP",
                    "decision": b_rep.opportunity.decision.value.upper(),
                    "net_edge_bps": str(b_rep.net_basis_bps.quantize(Decimal("0.01"))),
                    "expected_net_pnl_usdt": str(b_rep.opportunity.expected_net_pnl.quantize(Decimal("0.01"))),
                    "reject_reasons": b_rep.opportunity.reject_reason_codes,
                }
            )

            if isinstance(swap_fund_res, DerivativeFundingSnapshot):
                live_quotes["okx_swap:BTC-USDT-PERP"]["funding_rate_8h"] = str(swap_fund_res.funding_rate)
                live_quotes["okx_swap:BTC-USDT-PERP"]["mark_price"] = str(swap_fund_res.mark_price)
                fund_scanner = FundingRateArbitrageScanner(
                    registry=registry,
                    health_gate=health_gate,
                    cost_engine=cost_engine,
                    lifecycle=lifecycle,
                    risk_policy=live_policy,
                )
                f_rep = fund_scanner.evaluate_funding_carry(
                    spec=FundingCarryPairSpec(
                        pair_id="LIVE_OKX_BTC_FUNDING",
                        long_venue_id="okx_spot",
                        long_instrument_id="BTC-USDT",
                        short_deriv_venue_id="okx_swap",
                        short_deriv_instrument_id="BTC-USDT-PERP",
                        target_base_qty=Decimal("0.01"),
                        expected_holding_settlements=9,
                    ),
                    order_books=order_books,
                    short_funding=swap_fund_res,
                    now_utc=now_utc,
                )
                strategies_output.append(
                    {
                        "strategy": "S2-B OKX Funding Rate Carry (9x 8h)",
                        "pair": "BTC-USDT-SWAP",
                        "decision": f_rep.opportunity.decision.value.upper(),
                        "net_edge_bps": str(f_rep.net_carry_bps.quantize(Decimal("0.01"))),
                        "expected_net_pnl_usdt": str(f_rep.expected_net_carry_usdt.quantize(Decimal("0.01"))),
                        "reject_reasons": f_rep.opportunity.reject_reason_codes,
                    }
                )

        return {
            "timestamp_utc": now_utc.isoformat(),
            "proxy_used": self._proxy_url,
            "connected_books_count": len(order_books),
            "binance_ok_count": binance_ok_count,
            "okx_ok_count": okx_ok_count,
            "venue_status": venue_status,
            "live_quotes": live_quotes,
            "strategies_evaluated": strategies_output,
        }

    @staticmethod
    def _register_standard_instruments(registry: InstrumentRegistry, now_utc: datetime) -> None:
        specs = [
            ("binance_spot", "BTC-USDT", "BTCUSDT", InstrumentType.SPOT, "BTC", "USDT", Decimal("1"), Decimal("0.01"), Decimal("0.00001")),
            ("binance_spot", "ETH-USDT", "ETHUSDT", InstrumentType.SPOT, "ETH", "USDT", Decimal("1"), Decimal("0.01"), Decimal("0.0001")),
            ("binance_spot", "ETH-BTC", "ETHBTC", InstrumentType.SPOT, "ETH", "BTC", Decimal("1"), Decimal("0.00001"), Decimal("0.0001")),
            ("binance_spot", "BNB-USDT", "BNBUSDT", InstrumentType.SPOT, "BNB", "USDT", Decimal("1"), Decimal("0.01"), Decimal("0.001")),
            ("okx_spot", "BTC-USDT", "BTC-USDT", InstrumentType.SPOT, "BTC", "USDT", Decimal("1"), Decimal("0.1"), Decimal("0.0001")),
            ("okx_spot", "ETH-USDT", "ETH-USDT", InstrumentType.SPOT, "ETH", "USDT", Decimal("1"), Decimal("0.01"), Decimal("0.001")),
            ("okx_swap", "BTC-USDT-PERP", "BTC-USDT-SWAP", InstrumentType.PERPETUAL, "BTC", "USDT", Decimal("0.01"), Decimal("0.1"), Decimal("1")),
        ]
        for vid, iid, vsym, itype, base, quote, mult, tick, step in specs:
            registry.register(
                Instrument(
                    venue_id=vid,
                    instrument_id=iid,
                    venue_symbol=vsym,
                    instrument_type=itype,
                    base_asset=base,
                    quote_asset=quote,
                    settlement_asset="USDT" if quote == "USDT" else quote,
                    contract_multiplier=mult,
                    tick_size=tick,
                    step_size=step,
                    min_notional=Decimal("5.0") if quote == "USDT" else Decimal("0.0001"),
                    min_qty=step,
                    trading_status=TradingStatus.TRADING,
                    event_time_utc=now_utc,
                    receive_time_utc=now_utc,
                )
            )
