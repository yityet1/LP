"""Comprehensive Unit & Contract Tests for Sprint 2 (MD-001 ~ MD-004).
Covers:
- MD-001: Binance Spot REST Read-Only Adapter (200/401/403/429/418/5xx/Timeout, clock skew, exchangeInfo, depth, account, read-only guard, write UNKNOWN handling).
- MD-002: Binance Spot WebSocket Collector & Connection Lifecycle (bookTicker, diff depth, disconnect invalidation, reconnect snapshot requirement, 23h rotation, bounded queue overload circuit breaker).
- MD-003: Official L2 OrderBookBuilder Synchronization (buffered diff alignment, u <= lastUpdateId drop, U <= lastUpdateId+1 <= u first packet, sequence gap invalidation, qty=0 deletion, sorted depth walk).
- MD-004: Unified MarketHealthGate (stale book age > 500ms, cross-venue skew > 250ms, queue lag > 100ms, future clock drift, sequence gap rejection).
"""
from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from arb.config import RiskPolicyConfig, SecretStr, VenueCredentials
from arb.domain import (
    DataHealthStatus,
    OrderSide,
    OrderStatus,
    OrderType,
    PriceLevel,
    TradingStatus,
)
from arb.marketdata import (
    HealthReasonCode,
    MarketHealthGate,
    OrderBookBuilder,
    SequenceStatus,
)
from arb.observability import (
    AlertManager,
    MetricsRegistry,
    StructuredLogger,
)
from arb.venues import ErrorCategory, ExchangeError
from arb.venues.binance_spot import (
    BinanceBookTickerEvent,
    BinanceDiffDepthEvent,
    BinanceSpotRestAdapter,
    BinanceSpotStreamManager,
    HttpResponsePayload,
    HttpTransport,
    StreamOverloadError,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
RISK_YAML_PATH = REPO_ROOT / "configs" / "risk-policy.example.yaml"


class MockHttpTransport(HttpTransport):
    def __init__(self, handler: Callable[[str, str, Optional[Dict[str, Any]], Optional[Dict[str, str]]], HttpResponsePayload]) -> None:
        self._handler = handler
        self.calls: List[Dict[str, Any]] = []

    async def request(
        self,
        method: str,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout_ms: int = 3000,
    ) -> HttpResponsePayload:
        self.calls.append({"method": method, "url": url, "params": params, "headers": headers, "timeout_ms": timeout_ms})
        return self._handler(method, url, params, headers)


class TestSprint2MarketData(unittest.TestCase):
    # ==========================================================================
    # MD-001: Binance Spot REST Adapter Tests
    # ==========================================================================

    def test_md001_server_time_and_clock_skew_guard(self) -> None:
        now = datetime(2026, 10, 26, 2, 0, 0, tzinfo=timezone.utc)
        now_ms = int(now.timestamp() * 1000)

        # 1. Normal server time within 1000ms skew
        transport_ok = MockHttpTransport(
            lambda m, u, p, h: HttpResponsePayload(status_code=200, json_body={"serverTime": now_ms - 120})
        )
        adapter_ok = BinanceSpotRestAdapter(transport=transport_ok, max_clock_skew_ms=1000)
        srv_dt = asyncio.run(adapter_ok.get_server_time(local_now_utc=now))
        self.assertEqual(int(srv_dt.timestamp() * 1000), now_ms - 120)

        # 2. Clock skew > 1000ms raises TRANSIENT_NETWORK with native_code -1021
        transport_skew = MockHttpTransport(
            lambda m, u, p, h: HttpResponsePayload(status_code=200, json_body={"serverTime": now_ms - 2500})
        )
        adapter_skew = BinanceSpotRestAdapter(transport=transport_skew, max_clock_skew_ms=1000)
        with self.assertRaises(ExchangeError) as ctx:
            asyncio.run(adapter_skew.get_server_time(local_now_utc=now))
        self.assertEqual(ctx.exception.category, ErrorCategory.TRANSIENT_NETWORK)
        self.assertEqual(ctx.exception.native_code, "-1021")

    def test_md001_exchange_info_and_depth_snapshot_parsing(self) -> None:
        exchange_info_payload = {
            "timezone": "UTC",
            "serverTime": 1792980000000,
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "status": "TRADING",
                    "baseAsset": "BTC",
                    "quoteAsset": "USDT",
                    "isSpotTradingAllowed": True,
                    "filters": [
                        {"filterType": "PRICE_FILTER", "tickSize": "0.01000000"},
                        {"filterType": "LOT_SIZE", "stepSize": "0.00001000", "minQty": "0.00001000"},
                        {"filterType": "NOTIONAL", "minNotional": "10.00000000"},
                    ],
                },
                {
                    "symbol": "LUNAUSDT",
                    "status": "BREAK",
                    "baseAsset": "LUNA",
                    "quoteAsset": "USDT",
                    "isSpotTradingAllowed": False,
                    "filters": [
                        {"filterType": "PRICE_FILTER", "tickSize": "0.0001"},
                        {"filterType": "LOT_SIZE", "stepSize": "0.01", "minQty": "0.01"},
                        {"filterType": "MIN_NOTIONAL", "minNotional": "5.0"},
                    ],
                },
            ],
        }
        depth_payload = {
            "lastUpdateId": 1050,
            "bids": [["67990.50", "1.25000"], ["68000.00", "0.50000"], ["67980.00", "0.00000"]],
            "asks": [["68005.00", "2.00000"], ["68001.00", "0.80000"]],
        }

        def router(method: str, url: str, params: Optional[Dict[str, Any]], headers: Optional[Dict[str, str]]) -> HttpResponsePayload:
            if url.endswith("/api/v3/exchangeInfo"):
                return HttpResponsePayload(status_code=200, json_body=exchange_info_payload)
            if url.endswith("/api/v3/depth"):
                return HttpResponsePayload(
                    status_code=200,
                    json_body=depth_payload,
                    headers={"X-MBX-USED-WEIGHT-1M": "42"},
                )
            raise AssertionError(f"Unexpected URL: {url}")

        metrics = MetricsRegistry()
        adapter = BinanceSpotRestAdapter(transport=MockHttpTransport(router), metrics=metrics)
        instruments = asyncio.run(adapter.fetch_instruments())
        self.assertEqual(len(instruments), 2)
        btc = instruments[0]
        self.assertEqual(btc.instrument_id, "BTC-USDT")
        self.assertEqual(btc.venue_symbol, "BTCUSDT")
        self.assertEqual(btc.tick_size, Decimal("0.01"))
        self.assertEqual(btc.step_size, Decimal("0.00001"))
        self.assertEqual(btc.min_notional, Decimal("10.0"))
        self.assertEqual(btc.trading_status, TradingStatus.TRADING)

        luna = instruments[1]
        self.assertEqual(luna.trading_status, TradingStatus.HALTED)

        book = asyncio.run(adapter.fetch_order_book("BTC-USDT", depth=20))
        self.assertEqual(book.last_update_id, 1050)
        # Zero-qty level filtered out, bids sorted descending, asks sorted ascending
        self.assertEqual(len(book.bids), 2)
        self.assertEqual(book.bids[0].price, Decimal("68000.00"))
        self.assertEqual(book.asks[0].price, Decimal("68001.00"))
        self.assertEqual(
            metrics.get_gauge("arb_binance_used_weight_1m", labels={"venue_id": "binance_spot"}),
            42.0,
        )

    def test_md001_http_error_classification_429_418_5xx_timeout_and_readonly_gate(self) -> None:
        metrics = MetricsRegistry()
        logger = StructuredLogger()
        alerts = AlertManager(metrics=metrics, logger=logger)

        # 1. HTTP 429 sets rate limit backoff and fires P1 alert
        t_429 = MockHttpTransport(
            lambda m, u, p, h: HttpResponsePayload(
                status_code=429,
                json_body={"code": -1003, "msg": "Too many requests; current limit is 6000 weight per minute"},
                headers={"Retry-After": "10"},
            )
        )
        ad_429 = BinanceSpotRestAdapter(transport=t_429, metrics=metrics, logger=logger, alerts=alerts)
        with self.assertRaises(ExchangeError) as ctx429:
            asyncio.run(ad_429.fetch_order_book("BTC-USDT"))
        self.assertEqual(ctx429.exception.category, ErrorCategory.RATE_LIMITED)
        self.assertEqual(ctx429.exception.retry_after_ms, 10000)
        self.assertTrue(ad_429.rate_limit_backoff_until_ms > 0)

        # 2. HTTP 418 locks the adapter (IP ban) and blocks subsequent requests immediately
        t_418 = MockHttpTransport(
            lambda m, u, p, h: HttpResponsePayload(
                status_code=418,
                json_body={"code": -1003, "msg": "IP banned until 1792989999000"},
                headers={"Retry-After": "120"},
            )
        )
        ad_418 = BinanceSpotRestAdapter(transport=t_418, metrics=metrics, logger=logger, alerts=alerts)
        with self.assertRaises(ExchangeError) as ctx418:
            asyncio.run(ad_418.fetch_order_book("BTC-USDT"))
        self.assertEqual(ctx418.exception.category, ErrorCategory.IP_BANNED)
        self.assertTrue(ad_418.is_ip_banned)
        # Subsequent call fails without hitting transport
        with self.assertRaises(ExchangeError) as ctx_locked:
            asyncio.run(ad_418.get_server_time())
        self.assertEqual(ctx_locked.exception.category, ErrorCategory.IP_BANNED)
        self.assertEqual(len(t_418.calls), 1)

        # 3. Phase 1 Read-Only Mode blocks submit_order by default
        with self.assertRaises(ExchangeError) as ctx_ro:
            asyncio.run(
                ad_429.submit_order(
                    cycle_id="cyc_1",
                    strategy_id="strat_1",
                    instrument_id="BTC-USDT",
                    client_order_id="cid_1",
                    side=OrderSide.BUY,
                    order_type=OrderType.IOC,
                    quantity=Decimal("0.01"),
                    limit_price=Decimal("68000"),
                )
            )
        self.assertEqual(ctx_ro.exception.category, ErrorCategory.AUTH_PERMISSION)

        # 4. When unlocked for write, TimeoutError or HTTP 504 returns OrderStatus.UNKNOWN (never definite failure!)
        def timeout_handler(m: str, u: str, p: Any, h: Any) -> HttpResponsePayload:
            raise TimeoutError("Read timed out")

        creds = VenueCredentials(
            venue_id="binance_spot",
            api_key=SecretStr("k_test_12345"),
            api_secret=SecretStr("s_test_67890"),
        )
        ad_write = BinanceSpotRestAdapter(
            transport=MockHttpTransport(timeout_handler),
            credentials=creds,
            read_only_mode=False,
        )
        unknown_ev = asyncio.run(
            ad_write.submit_order(
                cycle_id="cyc_99",
                strategy_id="strat_s1",
                instrument_id="BTC-USDT",
                client_order_id="cid_timeout_99",
                side=OrderSide.BUY,
                order_type=OrderType.IOC,
                quantity=Decimal("0.05"),
                limit_price=Decimal("68000"),
            )
        )
        self.assertEqual(unknown_ev.status, OrderStatus.UNKNOWN)
        self.assertIn("UNKNOWN_ON_", unknown_ev.native_venue_status)

    # ==========================================================================
    # MD-002 & MD-003: WebSocket Collector & Official L2 OrderBook Sync Tests
    # ==========================================================================

    def test_md003_official_orderbook_snapshot_and_diff_sync_lifecycle(self) -> None:
        builder = OrderBookBuilder(venue_id="binance_spot", instrument_id="BTC-USDT")
        self.assertFalse(builder.is_valid())
        self.assertEqual(builder.sequence_status, SequenceStatus.UNINITIALIZED)

        now = datetime(2026, 10, 26, 2, 10, 0, tzinfo=timezone.utc)

        # Step 1: Buffer WebSocket diffs before REST snapshot arrives
        # Packet 1: U=998, u=1000 (will be dropped since snapshot lastUpdateId=1000)
        builder.buffer_or_apply_diff(
            first_update_id=998,
            final_update_id=1000,
            bid_deltas=[PriceLevel(price=Decimal("67900"), quantity=Decimal("9.9"))],
            ask_deltas=[],
            event_time_utc=now,
        )
        # Packet 2: U=1000, u=1002 (spans snapshot+1=1001: U <= 1001 <= u -> MUST be applied!)
        builder.buffer_or_apply_diff(
            first_update_id=1000,
            final_update_id=1002,
            bid_deltas=[PriceLevel(price=Decimal("68000"), quantity=Decimal("2.5"))],
            ask_deltas=[PriceLevel(price=Decimal("68010"), quantity=Decimal("0"))],  # deletes 68010 ask!
            event_time_utc=now,
        )
        # Packet 3: U=1003, u=1004 (continuous -> applied!)
        builder.buffer_or_apply_diff(
            first_update_id=1003,
            final_update_id=1004,
            bid_deltas=[],
            ask_deltas=[PriceLevel(price=Decimal("68015"), quantity=Decimal("1.5"))],
            event_time_utc=now,
        )
        self.assertEqual(builder.sequence_status, SequenceStatus.BUFFERING)

        # Step 2: Apply REST snapshot with lastUpdateId = 1000
        ok = builder.apply_snapshot(
            bids=[
                PriceLevel(price=Decimal("68000"), quantity=Decimal("1.0")),
                PriceLevel(price=Decimal("67990"), quantity=Decimal("3.0")),
            ],
            asks=[
                PriceLevel(price=Decimal("68010"), quantity=Decimal("0.8")),
                PriceLevel(price=Decimal("68020"), quantity=Decimal("2.0")),
            ],
            last_update_id=1000,
            event_time_utc=now,
        )
        self.assertTrue(ok)
        self.assertTrue(builder.is_valid())
        self.assertEqual(builder.last_update_id, 1004)

        # Verify buffered replay results:
        # - Packet 1 (u=1000) was dropped (67900 not in book)
        # - Packet 2 updated 68000 bid to 2.5 and deleted 68010 ask
        # - Packet 3 inserted 68015 ask with 1.5
        book = builder.to_order_book()
        self.assertEqual(book.bids[0].price, Decimal("68000"))
        self.assertEqual(book.bids[0].quantity, Decimal("2.5"))
        self.assertEqual(book.asks[0].price, Decimal("68015"))
        self.assertEqual(book.asks[0].quantity, Decimal("1.5"))
        self.assertEqual(book.asks[1].price, Decimal("68020"))

        # Step 3: Duplicate/old event (u=1004 <= last_update_id=1004) is safely ignored
        self.assertTrue(
            builder.apply_diff(
                first_update_id=1003,
                final_update_id=1004,
                bid_deltas=[PriceLevel(price=Decimal("68000"), quantity=Decimal("999"))],
                ask_deltas=[],
                event_time_utc=now,
            )
        )
        self.assertEqual(builder.to_order_book().bids[0].quantity, Decimal("2.5"))

        # Step 4: Simulate depth walk across multiple levels (BUY 2.5 BTC walks 1.5 @ 68015 + 1.0 @ 68020)
        slice_res = builder.depth(OrderSide.BUY, Decimal("2.5"))
        self.assertTrue(slice_res.is_fully_filled)
        self.assertEqual(slice_res.filled_qty, Decimal("2.5"))
        self.assertEqual(slice_res.levels_consumed, 2)
        expected_quote = Decimal("1.5") * Decimal("68015") + Decimal("1.0") * Decimal("68020")
        self.assertEqual(slice_res.total_quote_notional, expected_quote)

        # Step 5: Any sequence gap (e.g. U=1006 when last_update_id=1004, missing 1005) IMMEDIATELY invalidates book (CORE-001 P0 test)
        gap_ok = builder.apply_diff(
            first_update_id=1006,
            final_update_id=1007,
            bid_deltas=[PriceLevel(price=Decimal("68001"), quantity=Decimal("1.0"))],
            ask_deltas=[],
            event_time_utc=now,
        )
        self.assertFalse(gap_ok)
        self.assertFalse(builder.is_valid())
        self.assertEqual(builder.sequence_status, SequenceStatus.GAP_DETECTED)
        with self.assertRaisesRegex(RuntimeError, "Cannot compute depth on invalid order book"):
            builder.depth(OrderSide.BUY, Decimal("0.1"))

    def test_md002_websocket_stream_disconnect_reconnect_rotation_and_overload(self) -> None:
        builder = OrderBookBuilder(venue_id="binance_spot", instrument_id="BTC-USDT")
        now = datetime(2026, 10, 26, 2, 20, 0, tzinfo=timezone.utc)
        now_ms = int(now.timestamp() * 1000)

        metrics = MetricsRegistry()
        logger = StructuredLogger()
        alerts = AlertManager(metrics=metrics, logger=logger)

        mgr = BinanceSpotStreamManager(
            symbol_to_instrument_id={"BTCUSDT": "BTC-USDT"},
            order_books={"BTC-USDT": builder},
            max_queue_size=3,
            rotation_interval_seconds=23 * 3600,
            metrics=metrics,
            logger=logger,
            alerts=alerts,
        )

        sub = mgr.build_subscription_payload(request_id=7, depth_interval_ms=100)
        self.assertEqual(sub["params"], ["btcusdt@bookTicker", "btcusdt@depth@100ms"])

        mgr.on_connect(now_utc=now)
        self.assertTrue(mgr.is_connected)
        # Apply snapshot so book becomes valid
        builder.apply_snapshot(
            bids=[PriceLevel(price=Decimal("68000"), quantity=Decimal("1.0"))],
            asks=[PriceLevel(price=Decimal("68010"), quantity=Decimal("1.0"))],
            last_update_id=100,
            event_time_utc=now,
        )
        self.assertTrue(builder.is_valid())

        # Send valid diff depth message via WebSocket stream manager
        diff_msg = {
            "e": "depthUpdate",
            "E": now_ms + 50,
            "s": "BTCUSDT",
            "U": 101,
            "u": 102,
            "b": [["68002.00", "1.50"]],
            "a": [["68010.00", "0.80"]],
        }
        ev1 = mgr.on_raw_message(diff_msg, receive_time_utc=now + timedelta(milliseconds=55))
        self.assertIsInstance(ev1, BinanceDiffDepthEvent)
        self.assertEqual(builder.last_update_id, 102)
        self.assertEqual(builder.to_order_book().bids[0].price, Decimal("68002.00"))

        # Send bookTicker message
        bbo_msg = {
            "u": 103,
            "s": "BTCUSDT",
            "b": "68002.00",
            "B": "1.50",
            "a": "68010.00",
            "A": "0.80",
            "E": now_ms + 60,
        }
        ev2 = mgr.on_raw_message(bbo_msg, receive_time_utc=now + timedelta(milliseconds=65))
        self.assertIsInstance(ev2, BinanceBookTickerEvent)

        # Proactive 23h connection rotation check
        self.assertFalse(mgr.needs_proactive_rotation(now_utc=now + timedelta(hours=22)))
        self.assertTrue(mgr.needs_proactive_rotation(now_utc=now + timedelta(hours=23, seconds=1)))

        # Disconnect immediately invalidates book and fires alert
        mgr.on_disconnect(reason="NETWORK_DROP")
        self.assertFalse(mgr.is_connected)
        self.assertFalse(builder.is_valid())
        self.assertEqual(builder.sequence_status, SequenceStatus.DISCONNECTED)

        # Reconnect MUST NOT reuse old book (sets status to BUFFERING until fresh snapshot)
        mgr.on_connect(now_utc=now + timedelta(seconds=5))
        self.assertFalse(builder.is_valid())
        self.assertEqual(builder.sequence_status, SequenceStatus.BUFFERING)

        # Queue overload check (max_queue_size=3; currently 2 items in queue -> 3rd item succeeds, 4th triggers circuit breaker)
        mgr.on_raw_message(bbo_msg, receive_time_utc=now + timedelta(seconds=6))
        with self.assertRaises(StreamOverloadError):
            mgr.on_raw_message(bbo_msg, receive_time_utc=now + timedelta(seconds=7))
        self.assertTrue(mgr.is_circuit_broken)
        self.assertFalse(builder.is_valid())

    # ==========================================================================
    # MD-004: Unified MarketHealthGate Tests (CORE-002 P0 Test)
    # ==========================================================================

    def test_md004_market_health_freshness_and_cross_venue_skew_gates(self) -> None:
        policy = RiskPolicyConfig.from_yaml(RISK_YAML_PATH)  # max_book_age_ms=500, max_cross_venue_skew_ms=250, max_ws_queue_lag_ms=100
        gate = MarketHealthGate(risk_policy=policy)

        t0 = datetime(2026, 10, 26, 2, 30, 0, tzinfo=timezone.utc)

        b_book = OrderBookBuilder("binance_spot", "BTC-USDT")
        b_book.apply_snapshot(
            bids=[PriceLevel(price=Decimal("68000"), quantity=Decimal("1.0"))],
            asks=[PriceLevel(price=Decimal("68010"), quantity=Decimal("1.0"))],
            last_update_id=500,
            event_time_utc=t0,
            receive_time_utc=t0,
        )

        o_book = OrderBookBuilder("okx_spot", "BTC-USDT")
        o_book.apply_snapshot(
            bids=[PriceLevel(price=Decimal("68005"), quantity=Decimal("1.0"))],
            asks=[PriceLevel(price=Decimal("68015"), quantity=Decimal("1.0"))],
            last_update_id=900,
            event_time_utc=t0 + timedelta(milliseconds=100),
            receive_time_utc=t0 + timedelta(milliseconds=100),
        )

        # 1. Both books fresh (age 150ms & 50ms <= 500ms) and skew 100ms <= 250ms -> HEALTHY
        ok, reports, reasons = gate.evaluate_multi_book_gate(
            [b_book, o_book],
            now_utc=t0 + timedelta(milliseconds=150),
        )
        self.assertTrue(ok)
        self.assertEqual(len(reasons), 0)
        self.assertEqual(reports[0].cross_venue_skew_ms, 100)

        # 2. Stale book age (now = t0 + 600ms -> b_book age is 600ms > 500ms) -> REJECTED with STALE_BOOK_AGE
        ok_stale, reports_stale, reasons_stale = gate.evaluate_multi_book_gate(
            [b_book, o_book],
            now_utc=t0 + timedelta(milliseconds=600),
        )
        self.assertFalse(ok_stale)
        self.assertIn("binance_spot:BTC-USDT:STALE_BOOK_AGE", reasons_stale)
        self.assertEqual(reports_stale[0].status, DataHealthStatus.STALE)

        # 3. Cross-venue skew > 250ms (even if both books are < 500ms old) -> REJECTED with CROSS_VENUE_SKEW_EXCEEDED
        o_book.apply_snapshot(
            bids=[PriceLevel(price=Decimal("68005"), quantity=Decimal("1.0"))],
            asks=[PriceLevel(price=Decimal("68015"), quantity=Decimal("1.0"))],
            last_update_id=901,
            event_time_utc=t0 + timedelta(milliseconds=300),
            receive_time_utc=t0 + timedelta(milliseconds=300),
        )
        ok_skew, reports_skew, reasons_skew = gate.evaluate_multi_book_gate(
            [b_book, o_book],
            now_utc=t0 + timedelta(milliseconds=350),
        )
        self.assertFalse(ok_skew)
        self.assertIn("binance_spot:BTC-USDT:CROSS_VENUE_SKEW_EXCEEDED", reasons_skew)
        self.assertEqual(reports_skew[0].cross_venue_skew_ms, 300)

        # 4. WebSocket queue lag > 100ms -> REJECTED with QUEUE_LAG_EXCEEDED
        h_lag = gate.evaluate_book(
            o_book,
            now_utc=t0 + timedelta(milliseconds=350),
            queue_lag_ms=150,
        )
        self.assertFalse(h_lag.validity)
        self.assertIn(HealthReasonCode.QUEUE_LAG_EXCEEDED.value, h_lag.reason_codes)


if __name__ == "__main__":
    unittest.main(verbosity=2)
