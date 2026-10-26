"""[DERIV-001] Derivative Market Data, Mark/Index Price, Funding Rate & Position Adapters (`binance_usdm` & `okx_swap`)."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from arb.domain.models import (
    BalanceSnapshot,
    Fill,
    Instrument,
    InstrumentType,
    MarketSnapshot,
    OrderBook,
    OrderEvent,
    OrderSide,
    OrderType,
    PositionSnapshot,
    PriceLevel,
    TradingStatus,
    ensure_utc,
)
from arb.venues.base import (
    AccountAdapter,
    ErrorCategory,
    ExchangeError,
    InstrumentMetadataAdapter,
    MarketDataAdapter,
    TradingAdapter,
)
from arb.venues.binance_spot.rest import HttpTransport


class PositionMode(str, Enum):
    ONE_WAY = "one_way"          # Net position mode (`BOTH` / `net`)
    HEDGE = "hedge"              # Dual-side mode (`LONG` / `SHORT`)


class DerivativeFundingSnapshot(BaseModel):
    venue_id: str
    instrument_id: str
    mark_price: Decimal
    index_price: Decimal
    funding_rate: Decimal
    predicted_next_funding_rate: Optional[Decimal] = None
    next_funding_time_utc: datetime
    funding_interval_hours: int = 8
    event_time_utc: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    receive_time_utc: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def funding_rate_bps(self) -> Decimal:
        return self.funding_rate * Decimal("10000")

    @property
    def standardized_8h_funding_bps(self) -> Decimal:
        """Normalize 1h/4h/8h funding rate into a standardized 8-hour equivalent in bps (`S2-006`)."""
        if self.funding_interval_hours <= 0:
            return self.funding_rate_bps
        scale = Decimal("8") / Decimal(str(self.funding_interval_hours))
        return self.funding_rate_bps * scale

    @property
    def annualized_funding_bps(self) -> Decimal:
        """Compute annualized funding yield in bps based on `funding_interval_hours` (365 days * 24h)."""
        periods_per_year = Decimal(str((365 * 24) / max(1, self.funding_interval_hours)))
        return self.funding_rate_bps * periods_per_year


class BinanceUsdmDerivativesAdapter(
    InstrumentMetadataAdapter,
    MarketDataAdapter,
    AccountAdapter,
):
    """
    [DERIV-001] Binance USDⓈ-M Perpetual & Delivery Futures Read-Only Adapter (`venue_id = "binance_usdm"`).
    - Separates `mark_price` and `index_price` from executable L2 order book `bids`/`asks` (`S2-001`).
    - Normalizes funding rate (`lastFundingRate`), `nextFundingTime`, and `funding_interval_hours`.
    - Parses `/fapi/v2/positionRisk` into `PositionSnapshot` with liquidation price and margin usage.
    """

    VENUE_ID = "binance_usdm"

    def __init__(
        self,
        transport: HttpTransport,
        base_url: str = "https://fapi.binance.com",
        position_mode: PositionMode = PositionMode.ONE_WAY,
        timeout_ms: int = 3000,
    ) -> None:
        self._transport = transport
        self._base_url = base_url.rstrip("/")
        self._position_mode = position_mode
        self._timeout_ms = timeout_ms

    @property
    def venue_id(self) -> str:
        return self.VENUE_ID

    @property
    def position_mode(self) -> PositionMode:
        return self._position_mode

    async def get_server_time(self) -> datetime:
        resp = await self._transport.request("GET", f"{self._base_url}/fapi/v1/time", timeout_ms=self._timeout_ms)
        if resp.status_code != 200 or not isinstance(resp.json_body, dict):
            raise ExchangeError(self.VENUE_ID, ErrorCategory.INVALID_RESPONSE, "Invalid /fapi/v1/time response", resp.status_code)
        return datetime.fromtimestamp(int(resp.json_body["serverTime"]) / 1000.0, tz=timezone.utc)

    async def fetch_instruments(self) -> List[Instrument]:
        resp = await self._transport.request("GET", f"{self._base_url}/fapi/v1/exchangeInfo", timeout_ms=self._timeout_ms)
        if resp.status_code != 200 or not isinstance(resp.json_body, dict):
            raise ExchangeError(self.VENUE_ID, ErrorCategory.INVALID_RESPONSE, "Invalid /fapi/v1/exchangeInfo", resp.status_code)

        now = datetime.now(timezone.utc)
        results: List[Instrument] = []
        for sym in resp.json_body.get("symbols", []):
            ctype = str(sym.get("contractType", "PERPETUAL")).upper()
            if ctype not in ("PERPETUAL", "CURRENT_QUARTER", "NEXT_QUARTER"):
                continue
            base = str(sym["baseAsset"]).upper()
            quote = str(sym["quoteAsset"]).upper()
            margin_asset = str(sym.get("marginAsset", quote)).upper()
            venue_symbol = str(sym["symbol"]).upper()

            if ctype == "PERPETUAL":
                inst_type = InstrumentType.PERPETUAL
                canonical_id = f"{base}-{quote}-PERP"
                expiry_dt = None
            else:
                inst_type = InstrumentType.DATED_FUTURE
                delivery_ms = int(sym.get("deliveryDate", 0))
                expiry_dt = datetime.fromtimestamp(delivery_ms / 1000.0, tz=timezone.utc) if delivery_ms > 0 else None
                suffix = venue_symbol.split("_")[1] if "_" in venue_symbol else "FUT"
                canonical_id = f"{base}-{quote}-{suffix}"

            tick_size = Decimal("0.01")
            step_size = Decimal("0.001")
            min_qty = Decimal("0.001")
            min_notional = Decimal("5.0")
            for f in sym.get("filters", []):
                ft = f.get("filterType")
                if ft == "PRICE_FILTER":
                    tick_size = Decimal(str(f["tickSize"]))
                elif ft == "LOT_SIZE":
                    step_size = Decimal(str(f["stepSize"]))
                    min_qty = Decimal(str(f.get("minQty", step_size)))
                elif ft == "MIN_NOTIONAL":
                    min_notional = Decimal(str(f.get("notional", "5.0")))

            status_str = str(sym.get("status", "TRADING")).upper()
            t_status = TradingStatus.TRADING if status_str == "TRADING" else TradingStatus.HALTED

            results.append(
                Instrument(
                    venue_id=self.VENUE_ID,
                    instrument_id=canonical_id,
                    venue_symbol=venue_symbol,
                    instrument_type=inst_type,
                    base_asset=base,
                    quote_asset=quote,
                    settlement_asset=margin_asset,
                    contract_multiplier=Decimal("1"),
                    tick_size=tick_size,
                    step_size=step_size,
                    min_notional=min_notional,
                    min_qty=min_qty,
                    trading_status=t_status,
                    expiry=expiry_dt,
                    funding_interval_hours=int(sym.get("fundingIntervalHours", 8)) if inst_type == InstrumentType.PERPETUAL else None,
                    event_time_utc=now,
                    receive_time_utc=now,
                )
            )
        return results

    async def fetch_order_book(self, instrument_id: str, depth: int = 20) -> OrderBook:
        venue_sym = instrument_id.replace("-PERP", "").replace("-", "")
        resp = await self._transport.request(
            "GET",
            f"{self._base_url}/fapi/v1/depth",
            params={"symbol": venue_sym, "limit": depth},
            timeout_ms=self._timeout_ms,
        )
        if resp.status_code != 200 or not isinstance(resp.json_body, dict):
            raise ExchangeError(self.VENUE_ID, ErrorCategory.INVALID_RESPONSE, "Invalid /fapi/v1/depth", resp.status_code)
        body = resp.json_body
        now = datetime.now(timezone.utc)
        ev_ms = int(body.get("T", int(now.timestamp() * 1000)))
        ev_utc = datetime.fromtimestamp(ev_ms / 1000.0, tz=timezone.utc)
        bids = [PriceLevel(price=Decimal(str(x[0])), quantity=Decimal(str(x[1]))) for x in body.get("bids", [])]
        asks = [PriceLevel(price=Decimal(str(x[0])), quantity=Decimal(str(x[1]))) for x in body.get("asks", [])]
        bids.sort(key=lambda x: x.price, reverse=True)
        asks.sort(key=lambda x: x.price)
        return OrderBook(
            venue_id=self.VENUE_ID,
            instrument_id=instrument_id,
            bids=bids,
            asks=asks,
            last_update_id=int(body["lastUpdateId"]),
            is_valid=True,
            event_time_utc=ev_utc,
            receive_time_utc=now,
        )

    async def fetch_funding_and_mark(self, instrument_id: str, funding_interval_hours: int = 8) -> DerivativeFundingSnapshot:
        venue_sym = instrument_id.replace("-PERP", "").replace("-", "")
        resp = await self._transport.request(
            "GET",
            f"{self._base_url}/fapi/v1/premiumIndex",
            params={"symbol": venue_sym},
            timeout_ms=self._timeout_ms,
        )
        if resp.status_code != 200 or not isinstance(resp.json_body, dict):
            raise ExchangeError(self.VENUE_ID, ErrorCategory.INVALID_RESPONSE, "Invalid /fapi/v1/premiumIndex", resp.status_code)
        body = resp.json_body
        now = datetime.now(timezone.utc)
        ev_ms = int(body.get("time", int(now.timestamp() * 1000)))
        next_ms = int(body["nextFundingTime"])
        return DerivativeFundingSnapshot(
            venue_id=self.VENUE_ID,
            instrument_id=instrument_id,
            mark_price=Decimal(str(body["markPrice"])),
            index_price=Decimal(str(body["indexPrice"])),
            funding_rate=Decimal(str(body["lastFundingRate"])),
            next_funding_time_utc=datetime.fromtimestamp(next_ms / 1000.0, tz=timezone.utc),
            funding_interval_hours=funding_interval_hours,
            event_time_utc=datetime.fromtimestamp(ev_ms / 1000.0, tz=timezone.utc),
            receive_time_utc=now,
        )

    async def fetch_market_snapshot(self, instrument_id: str) -> MarketSnapshot:
        book = await self.fetch_order_book(instrument_id, depth=20)
        fm = await self.fetch_funding_and_mark(instrument_id)
        return MarketSnapshot(
            venue_id=self.VENUE_ID,
            instrument_id=instrument_id,
            quote_asset="USDT",
            bid_levels=book.bids,
            ask_levels=book.asks,
            mark_price=fm.mark_price,
            index_price=fm.index_price,
            funding_rate=fm.funding_rate,
            next_funding_time=fm.next_funding_time_utc,
            sequence_id=book.last_update_id,
            event_time_utc=book.event_time_utc,
            receive_time_utc=book.receive_time_utc,
        )

    async def fetch_balances(self) -> List[BalanceSnapshot]:
        resp = await self._transport.request("GET", f"{self._base_url}/fapi/v2/balance", timeout_ms=self._timeout_ms)
        if resp.status_code != 200 or not isinstance(resp.json_body, list):
            raise ExchangeError(self.VENUE_ID, ErrorCategory.INVALID_RESPONSE, "Invalid /fapi/v2/balance", resp.status_code)
        now = datetime.now(timezone.utc)
        snaps: List[BalanceSnapshot] = []
        for b in resp.json_body:
            total = Decimal(str(b.get("balance", "0")))
            avail = Decimal(str(b.get("availableBalance", "0")))
            if total > Decimal("0"):
                snaps.append(
                    BalanceSnapshot(
                        venue_id=self.VENUE_ID,
                        account_id="binance_usdm_main",
                        asset=str(b["asset"]).upper(),
                        total_balance=total,
                        free_balance=avail,
                        locked_balance=max(Decimal("0"), total - avail),
                        event_time_utc=now,
                        receive_time_utc=now,
                    )
                )
        return snaps

    async def fetch_positions(self) -> List[PositionSnapshot]:
        resp = await self._transport.request("GET", f"{self._base_url}/fapi/v2/positionRisk", timeout_ms=self._timeout_ms)
        if resp.status_code != 200 or not isinstance(resp.json_body, list):
            raise ExchangeError(self.VENUE_ID, ErrorCategory.INVALID_RESPONSE, "Invalid /fapi/v2/positionRisk", resp.status_code)
        now = datetime.now(timezone.utc)
        positions: List[PositionSnapshot] = []
        for p in resp.json_body:
            qty = Decimal(str(p.get("positionAmt", "0")))
            if qty == Decimal("0"):
                continue
            sym = str(p["symbol"]).upper()
            inst_id = f"{sym[:-4]}-USDT-PERP" if sym.endswith("USDT") else sym
            liq_raw = Decimal(str(p.get("liquidationPrice", "0")))
            positions.append(
                PositionSnapshot(
                    venue_id=self.VENUE_ID,
                    account_id="binance_usdm_main",
                    instrument_id=inst_id,
                    position_side=str(p.get("positionSide", "NET")).upper(),
                    quantity=qty,
                    contract_multiplier=Decimal("1"),
                    entry_price=Decimal(str(p["entryPrice"])),
                    mark_price=Decimal(str(p["markPrice"])),
                    unrealized_pnl=Decimal(str(p.get("unRealizedProfit", "0"))),
                    margin_used=Decimal(str(p.get("isolatedMargin", p.get("initialMargin", "0")))),
                    liquidation_price=liq_raw if liq_raw > Decimal("0") else None,
                    event_time_utc=now,
                    receive_time_utc=now,
                )
            )
        return positions


class OkxSwapDerivativesAdapter(
    InstrumentMetadataAdapter,
    MarketDataAdapter,
    AccountAdapter,
):
    """
    [DERIV-001] OKX V5 USDT-M Swap & Futures Adapter (`venue_id = "okx_swap"`).
    - Explicitly parses `ctVal` (`contract_multiplier`, e.g., `0.01 BTC` per contract on `BTC-USDT-SWAP`)
      so downstream strategies never confuse contract count with base asset quantity (`S2-003`).
    - Separates `mark_price` / `index_price` from order book `bids` / `asks` (`S2-001`).
    """

    VENUE_ID = "okx_swap"

    def __init__(
        self,
        transport: HttpTransport,
        base_url: str = "https://www.okx.com",
        position_mode: PositionMode = PositionMode.ONE_WAY,
        timeout_ms: int = 3000,
    ) -> None:
        self._transport = transport
        self._base_url = base_url.rstrip("/")
        self._position_mode = position_mode
        self._timeout_ms = timeout_ms

    @property
    def venue_id(self) -> str:
        return self.VENUE_ID

    async def get_server_time(self) -> datetime:
        resp = await self._transport.request("GET", f"{self._base_url}/api/v5/public/time", timeout_ms=self._timeout_ms)
        if resp.status_code != 200 or not isinstance(resp.json_body, dict):
            raise ExchangeError(self.VENUE_ID, ErrorCategory.INVALID_RESPONSE, "Invalid OKX time", resp.status_code)
        ts_ms = int(resp.json_body["data"][0]["ts"])
        return datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)

    async def fetch_instruments(self) -> List[Instrument]:
        resp = await self._transport.request(
            "GET",
            f"{self._base_url}/api/v5/public/instruments",
            params={"instType": "SWAP"},
            timeout_ms=self._timeout_ms,
        )
        if resp.status_code != 200 or not isinstance(resp.json_body, dict):
            raise ExchangeError(self.VENUE_ID, ErrorCategory.INVALID_RESPONSE, "Invalid OKX SWAP instruments", resp.status_code)

        now = datetime.now(timezone.utc)
        results: List[Instrument] = []
        for item in resp.json_body.get("data", []):
            inst_id_native = str(item["instId"]).upper()  # e.g., BTC-USDT-SWAP
            parts = inst_id_native.split("-")
            base = parts[0]
            quote = parts[1] if len(parts) > 1 else "USDT"
            settle = str(item.get("settleCcy", quote)).upper()
            ct_val = Decimal(str(item.get("ctVal", "1")))
            tick_sz = Decimal(str(item["tickSz"]))
            lot_sz = Decimal(str(item["lotSz"]))
            min_sz = Decimal(str(item["minSz"]))
            state_str = str(item.get("state", "live")).lower()
            t_status = TradingStatus.TRADING if state_str == "live" else TradingStatus.HALTED
            canonical_id = inst_id_native.replace("-SWAP", "-PERP")

            results.append(
                Instrument(
                    venue_id=self.VENUE_ID,
                    instrument_id=canonical_id,
                    venue_symbol=inst_id_native,
                    instrument_type=InstrumentType.PERPETUAL,
                    base_asset=base,
                    quote_asset=quote,
                    settlement_asset=settle,
                    contract_multiplier=ct_val,
                    tick_size=tick_sz,
                    step_size=lot_sz,
                    min_notional=Decimal("10.0"),
                    min_qty=min_sz,
                    trading_status=t_status,
                    funding_interval_hours=8,
                    event_time_utc=now,
                    receive_time_utc=now,
                )
            )
        return results

    async def fetch_order_book(self, instrument_id: str, depth: int = 20) -> OrderBook:
        okx_sym = instrument_id.replace("-PERP", "-SWAP")
        resp = await self._transport.request(
            "GET",
            f"{self._base_url}/api/v5/market/books",
            params={"instId": okx_sym, "sz": str(depth)},
            timeout_ms=self._timeout_ms,
        )
        if resp.status_code != 200 or not isinstance(resp.json_body, dict):
            raise ExchangeError(self.VENUE_ID, ErrorCategory.INVALID_RESPONSE, "Invalid OKX swap book", resp.status_code)
        book_obj = resp.json_body["data"][0]
        now = datetime.now(timezone.utc)
        ts_ms = int(book_obj.get("ts", int(now.timestamp() * 1000)))
        bids = [PriceLevel(price=Decimal(str(x[0])), quantity=Decimal(str(x[1]))) for x in book_obj.get("bids", [])]
        asks = [PriceLevel(price=Decimal(str(x[0])), quantity=Decimal(str(x[1]))) for x in book_obj.get("asks", [])]
        bids.sort(key=lambda x: x.price, reverse=True)
        asks.sort(key=lambda x: x.price)
        return OrderBook(
            venue_id=self.VENUE_ID,
            instrument_id=instrument_id,
            bids=bids,
            asks=asks,
            last_update_id=int(book_obj.get("seqId", ts_ms)),
            is_valid=True,
            event_time_utc=datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc),
            receive_time_utc=now,
        )

    async def fetch_funding_and_mark(self, instrument_id: str) -> DerivativeFundingSnapshot:
        okx_sym = instrument_id.replace("-PERP", "-SWAP")
        resp = await self._transport.request(
            "GET",
            f"{self._base_url}/api/v5/public/funding-rate",
            params={"instId": okx_sym},
            timeout_ms=self._timeout_ms,
        )
        if resp.status_code != 200 or not isinstance(resp.json_body, dict):
            raise ExchangeError(self.VENUE_ID, ErrorCategory.INVALID_RESPONSE, "Invalid OKX funding-rate", resp.status_code)
        item = resp.json_body["data"][0]
        now = datetime.now(timezone.utc)
        f_time_ms = int(item["fundingTime"])
        next_time_ms = int(item.get("nextFundingTime", f_time_ms + 8 * 3600 * 1000))
        interval_hours = max(1, int(round((next_time_ms - f_time_ms) / (3600 * 1000)))) if next_time_ms > f_time_ms else 8
        next_rate_str = str(item.get("nextFundingRate", "")).strip()

        return DerivativeFundingSnapshot(
            venue_id=self.VENUE_ID,
            instrument_id=instrument_id,
            mark_price=Decimal(str(item.get("markPx", "68000"))),
            index_price=Decimal(str(item.get("idxPx", "68000"))),
            funding_rate=Decimal(str(item["fundingRate"])),
            predicted_next_funding_rate=Decimal(next_rate_str) if next_rate_str else None,
            next_funding_time_utc=datetime.fromtimestamp(f_time_ms / 1000.0, tz=timezone.utc),
            funding_interval_hours=interval_hours,
            event_time_utc=now,
            receive_time_utc=now,
        )

    async def fetch_market_snapshot(self, instrument_id: str) -> MarketSnapshot:
        book = await self.fetch_order_book(instrument_id, depth=20)
        fm = await self.fetch_funding_and_mark(instrument_id)
        return MarketSnapshot(
            venue_id=self.VENUE_ID,
            instrument_id=instrument_id,
            quote_asset="USDT",
            bid_levels=book.bids,
            ask_levels=book.asks,
            mark_price=fm.mark_price,
            index_price=fm.index_price,
            funding_rate=fm.funding_rate,
            next_funding_time=fm.next_funding_time_utc,
            sequence_id=book.last_update_id,
            event_time_utc=book.event_time_utc,
            receive_time_utc=book.receive_time_utc,
        )

    async def fetch_balances(self) -> List[BalanceSnapshot]:
        return []

    async def fetch_positions(self) -> List[PositionSnapshot]:
        resp = await self._transport.request("GET", f"{self._base_url}/api/v5/account/positions", timeout_ms=self._timeout_ms)
        if resp.status_code != 200 or not isinstance(resp.json_body, dict):
            raise ExchangeError(self.VENUE_ID, ErrorCategory.INVALID_RESPONSE, "Invalid OKX positions", resp.status_code)
        now = datetime.now(timezone.utc)
        positions: List[PositionSnapshot] = []
        for p in resp.json_body.get("data", []):
            qty = Decimal(str(p.get("pos", "0")))
            if qty == Decimal("0"):
                continue
            inst_native = str(p["instId"]).upper()
            inst_id = inst_native.replace("-SWAP", "-PERP")
            liq_str = str(p.get("liqPx", "")).strip()
            positions.append(
                PositionSnapshot(
                    venue_id=self.VENUE_ID,
                    account_id="okx_swap_main",
                    instrument_id=inst_id,
                    position_side=str(p.get("posSide", "net")).upper(),
                    quantity=qty,
                    contract_multiplier=Decimal(str(p.get("ctVal", "0.01"))),
                    entry_price=Decimal(str(p["avgPx"])),
                    mark_price=Decimal(str(p["markPx"])),
                    unrealized_pnl=Decimal(str(p.get("upl", "0"))),
                    margin_used=Decimal(str(p.get("imr", p.get("margin", "0")))),
                    liquidation_price=Decimal(liq_str) if liq_str and Decimal(liq_str) > Decimal("0") else None,
                    event_time_utc=now,
                    receive_time_utc=now,
                )
            )
        return positions
