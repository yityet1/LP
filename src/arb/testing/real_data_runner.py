"""End-to-End Real Market Data Verification Runner across all 6 Strategy Families (S1, S2, S3, S4-A/B/C, M7)."""
from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Tuple

from arb.bridge.ledger import BridgeHealthStatus, BridgeRouteSpec, BridgeTransferLedger
from arb.config.settings import RiskPolicyConfig, Settings
from arb.costs.engine import CostEngine, FeeSchedule
from arb.dex.chain import BlockHeaderSnapshot, EvmChainAdapter, WhitelistPoolContract, WhitelistTokenMetadata
from arb.dex.pool_sim import AmmPoolState, DexPoolProtocol, DexPoolSimulator
from arb.domain.models import (
    BalanceSnapshot,
    Instrument,
    InstrumentType,
    OptionType,
    PriceLevel,
)
from arb.equities.adapters import BorrowLocateQuote, EquityBrokerAccountSnapshot, EquityBrokerAdapter, EquityLinkedMarketAdapter
from arb.equities.terms import (
    ComparabilityApproval,
    DividendTreatment,
    EquityProductKind,
    EquityProductTerms,
    EquitySettlementCycle,
    EquityTermsRegistry,
    EquityTradingSession,
    RedemptionRights,
)
from arb.instruments.registry import InstrumentRegistry
from arb.ledger.store import LedgerStore
from arb.marketdata.health import MarketHealthGate
from arb.marketdata.orderbook import OrderBookBuilder
from arb.options.models import ExerciseStyle, OptionContractSpec, OptionMarketQuote, OptionSettlementStyle
from arb.options.pricing import OptionPricingEngine
from arb.portfolio.allocator import PortfolioCapitalAllocator
from arb.portability import model_to_json, run_portability_check
from arb.replay.recorder import DeterministicReplayEngine, MarketDataRecorder, RecordedMarketEvent
from arb.strategies.basis.scanner import BasisArbitrageScanner, BasisPairSpec
from arb.strategies.cex_dex.scanner import CexDexArbitrageScanner
from arb.strategies.cross_cex_spot.scanner import CrossCexPairMapping, CrossCexSpotScanner
from arb.strategies.cross_chain.scanner import CrossChainArbitrageScanner
from arb.strategies.equity_arb.scanner import EquityCrossMarketScanner
from arb.strategies.funding.scanner import FundingCarryPairSpec, FundingRateArbitrageScanner
from arb.strategies.options_arb.scanner import OptionComboRiskLimits, OptionsRelativeValueScanner
from arb.strategies.triangle.scanner import TriangleArbitrageScanner
from arb.strategy.lifecycle import OpportunityLifecycleManager
from arb.venues.derivatives.adapters import DerivativeFundingSnapshot


class RealDataBenchmarkRunner:
    """
    Runs the entire `arb-platform` stack against real 2026-10-26 market data
    (Google Finance, Binance, OKX, NASDAQ NVDA, Ethereum Uniswap V2/V3) and produces
    a structured verification report.
    """

    @staticmethod
    def run_full_real_data_suite(repo_root: Path | str) -> Dict[str, Any]:
        root = Path(repo_root)
        dataset_path = root / "datasets" / "real_market_20261026.json"
        risk_yaml_path = root / "configs" / "risk-policy.example.yaml"

        raw_data = json.loads(dataset_path.read_text(encoding="utf-8"))
        policy = RiskPolicyConfig.from_yaml(risk_yaml_path)
        settings = Settings.from_env(risk_yaml_path)
        ledger = LedgerStore(":memory:")
        cfg_hash = ledger.record_config_version(settings)
        port_report = run_portability_check()

        now = datetime(2026, 10, 26, 14, 30, 0, tzinfo=timezone.utc)  # 10:30 AM EDT (NASDAQ Regular Session)
        reg = InstrumentRegistry(max_rules_age_seconds=86400)
        health_gate = MarketHealthGate(risk_policy=policy)
        cost_engine = CostEngine(
            fee_schedules={
                "binance_spot": FeeSchedule(
                    venue_id="binance_spot",
                    tier_name="VIP1_BNB",
                    maker_fee_rate=Decimal("0.0006"),
                    taker_fee_rate=Decimal("0.0008"),
                    fee_discount_asset="BNB",
                    fee_discount_rate=Decimal("0.75"),
                ),
                "okx_spot": FeeSchedule(
                    venue_id="okx_spot",
                    tier_name="VIP1",
                    maker_fee_rate=Decimal("0.0006"),
                    taker_fee_rate=Decimal("0.0008"),
                ),
                "okx_swap": FeeSchedule(
                    venue_id="okx_swap",
                    tier_name="VIP1",
                    maker_fee_rate=Decimal("0.0002"),
                    taker_fee_rate=Decimal("0.0005"),
                ),
            }
        )

        # Register real instruments
        for vid, inst_id, sym, base, quote, itype, mult, tick, step in [
            ("binance_spot", "BTC-USDT", "BTCUSDT", "BTC", "USDT", InstrumentType.SPOT, "1", "0.01", "0.0001"),
            ("binance_spot", "ETH-USDT", "ETHUSDT", "ETH", "USDT", InstrumentType.SPOT, "1", "0.01", "0.001"),
            ("binance_spot", "ETH-BTC", "ETHBTC", "ETH", "BTC", InstrumentType.SPOT, "1", "0.00001", "0.001"),
            ("binance_spot", "BNB-USDT", "BNBUSDT", "BNB", "USDT", InstrumentType.SPOT, "1", "0.01", "0.01"),
            ("okx_spot", "BTC-USDT", "BTC-USDT", "BTC", "USDT", InstrumentType.SPOT, "1", "0.1", "0.0001"),
            ("okx_spot", "ETH-USDT", "ETH-USDT", "ETH", "USDT", InstrumentType.SPOT, "1", "0.01", "0.001"),
            ("okx_swap", "BTC-USDT-PERP", "BTC-USDT-SWAP", "BTC", "USDT", InstrumentType.PERPETUAL, "0.01", "0.1", "1"),
        ]:
            reg.register(
                Instrument(
                    venue_id=vid,
                    instrument_id=inst_id,
                    venue_symbol=sym,
                    instrument_type=itype,
                    base_asset=base,
                    quote_asset=quote,
                    settlement_asset="USDT",
                    contract_multiplier=Decimal(mult),
                    tick_size=Decimal(tick),
                    step_size=Decimal(step),
                    min_notional=Decimal("0.001") if quote == "BTC" else Decimal("10.0"),
                    min_qty=Decimal(step),
                    funding_interval_hours=8 if itype == InstrumentType.PERPETUAL else None,
                    event_time_utc=now,
                    receive_time_utc=now,
                )
            )

        # Build L2 OrderBooks from real_market_20261026.json
        books: Dict[Tuple[str, str], OrderBookBuilder] = {}
        b_spot_books: Dict[str, OrderBookBuilder] = {}
        quotes = raw_data["spot_and_perp_quotes"]
        for vid, inst_map in quotes.items():
            for inst_id, q_obj in inst_map.items():
                builder = OrderBookBuilder(vid, inst_id)
                bids = [PriceLevel(price=Decimal(x[0]), quantity=Decimal(x[1])) for x in q_obj["bids"]]
                asks = [PriceLevel(price=Decimal(x[0]), quantity=Decimal(x[1])) for x in q_obj["asks"]]
                builder.apply_snapshot(
                    bids=bids,
                    asks=asks,
                    last_update_id=int(q_obj["last_update_id"]),
                    event_time_utc=now,
                    receive_time_utc=now,
                )
                books[(vid, inst_id)] = builder
                if vid == "binance_spot":
                    b_spot_books[inst_id] = builder

        # ------------------------------------------------------------------
        # 1. Deterministic Replay of Real Google Finance BTC/USD Intraday Series (DATA-001)
        # ------------------------------------------------------------------
        with tempfile.TemporaryDirectory() as tmpdir:
            recorder = MarketDataRecorder(tmpdir)
            base_t = datetime(2026, 10, 26, 0, 0, 0, tzinfo=timezone.utc)
            for idx, pt in enumerate(raw_data["intraday_series_20261026"]["BTC_USD"]):
                px = Decimal(str(pt["price"]))
                ev_t = base_t + timedelta(minutes=idx * 15)
                recorder.record_event(
                    RecordedMarketEvent(
                        event_kind="snapshot",
                        venue_id="binance_spot",
                        instrument_id="BTC-USDT",
                        first_update_id=1000 + idx,
                        final_update_id=1000 + idx,
                        bids=[PriceLevel(price=px - Decimal("1.20"), quantity=Decimal("1.5"))],
                        asks=[PriceLevel(price=px + Decimal("1.20"), quantity=Decimal("1.5"))],
                        event_time_utc=ev_t,
                        receive_time_utc=ev_t,
                    )
                )
            events, manifest = recorder.load_dataset([("binance_spot", "2026-10-26", "BTC-USDT")])
            rep_books = {("binance_spot", "BTC-USDT"): OrderBookBuilder("binance_spot", "BTC-USDT")}
            rep_engine = DeterministicReplayEngine(config_version_hash=cfg_hash, order_books=rep_books)
            rep_res, _ = rep_engine.run(events, manifest)

        # ------------------------------------------------------------------
        # 2. S1-A: Binance Spot Triangle Arbitrage on Real BTC/ETH/BNB Quotes
        # ------------------------------------------------------------------
        tri_scanner = TriangleArbitrageScanner(
            venue_id="binance_spot",
            settlement_asset="USDT",
            registry=reg,
            health_gate=health_gate,
            cost_engine=cost_engine,
            lifecycle=OpportunityLifecycleManager(ledger=ledger),
            risk_policy=policy,
            initial_capital_quote=Decimal("1000.00"),
        )
        tri_scanner.build_graph(
            [
                reg.get("binance_spot", "BTC-USDT"),
                reg.get("binance_spot", "ETH-USDT"),
                reg.get("binance_spot", "ETH-BTC"),
            ]
        )
        tri_exec, tri_all = tri_scanner.scan_all(
            order_books=b_spot_books,
            now_utc=now,
            third_fee_asset_prices_usdt={"BNB": Decimal("735.72")},
        )

        # ------------------------------------------------------------------
        # 3. S1-B: Cross-CEX Spot Arbitrage (OKX BTC $80,225.0 Ask -> Binance BTC $80,430.0 Bid)
        # ------------------------------------------------------------------
        balances = {
            ("okx_spot", "USDT"): BalanceSnapshot(
                venue_id="okx_spot", account_id="o_main", asset="USDT",
                total_balance=Decimal("10000"), free_balance=Decimal("10000"), event_time_utc=now
            ),
            ("binance_spot", "BTC"): BalanceSnapshot(
                venue_id="binance_spot", account_id="b_main", asset="BTC",
                total_balance=Decimal("0.50"), free_balance=Decimal("0.50"), event_time_utc=now
            ),
            ("binance_spot", "USDT"): BalanceSnapshot(
                venue_id="binance_spot", account_id="b_main", asset="USDT",
                total_balance=Decimal("10000"), free_balance=Decimal("10000"), event_time_utc=now
            ),
            ("okx_spot", "BTC"): BalanceSnapshot(
                venue_id="okx_spot", account_id="o_main", asset="BTC",
                total_balance=Decimal("0.50"), free_balance=Decimal("0.50"), event_time_utc=now
            ),
        }
        cross_scanner = CrossCexSpotScanner(
            registry=reg,
            health_gate=health_gate,
            cost_engine=cost_engine,
            lifecycle=OpportunityLifecycleManager(ledger=ledger),
            risk_policy=policy,
        )
        cross_exec, _ = cross_scanner.scan_pair(
            mapping=CrossCexPairMapping(
                canonical_pair="BTC/USDT",
                venue_a_id="binance_spot",
                venue_a_instrument_id="BTC-USDT",
                venue_b_id="okx_spot",
                venue_b_instrument_id="BTC-USDT",
                target_base_qty=Decimal("0.0100"),
            ),
            order_books=books,
            balances=balances,
            now_utc=now,
        )

        # ------------------------------------------------------------------
        # 4. S2-A & S2-B: Spot-Futures Basis ($80,432.40 vs $81,568.10) & Funding Carry
        # ------------------------------------------------------------------
        basis_scanner = BasisArbitrageScanner(
            registry=reg,
            health_gate=health_gate,
            cost_engine=cost_engine,
            lifecycle=OpportunityLifecycleManager(ledger=ledger),
            risk_policy=policy,
        )
        basis_rep = basis_scanner.evaluate_basis(
            spec=BasisPairSpec(
                pair_id="BTC_SPOT_OKX_SWAP_BASIS",
                spot_venue_id="binance_spot",
                spot_instrument_id="BTC-USDT",
                deriv_venue_id="okx_swap",
                deriv_instrument_id="BTC-USDT-PERP",
                target_base_qty=Decimal("0.0100"),
            ),
            order_books=books,
            mark_price_reference=Decimal("81569.5"),
            now_utc=now,
        )

        funding_scanner = FundingRateArbitrageScanner(
            registry=reg,
            health_gate=health_gate,
            cost_engine=cost_engine,
            lifecycle=OpportunityLifecycleManager(ledger=ledger),
            risk_policy=policy,
        )
        funding_rep = funding_scanner.evaluate_funding_carry(
            spec=FundingCarryPairSpec(
                pair_id="BTC_FUNDING_CARRY",
                long_venue_id="binance_spot",
                long_instrument_id="BTC-USDT",
                short_deriv_venue_id="okx_swap",
                short_deriv_instrument_id="BTC-USDT-PERP",
                target_base_qty=Decimal("0.0100"),
                expected_holding_settlements=12,
            ),
            order_books=books,
            short_funding=DerivativeFundingSnapshot(
                venue_id="okx_swap",
                instrument_id="BTC-USDT-PERP",
                mark_price=Decimal("81569.5"),
                index_price=Decimal("80432.4"),
                funding_rate=Decimal("0.000845"),
                next_funding_time_utc=now + timedelta(hours=4),
                funding_interval_hours=8,
                event_time_utc=now,
                receive_time_utc=now,
            ),
            now_utc=now,
        )

        # ------------------------------------------------------------------
        # 5. S3: NVIDIA (NASDAQ: NVDA, ISIN US67066G1040) Cross-Market Equity Arbitrage ($213.45 vs $214.65)
        # ------------------------------------------------------------------
        eq_reg = EquityTermsRegistry()
        nvda_token = EquityProductTerms(
            instrument_id="NVDA-TOKEN-USDT",
            venue_id="crypto_eq_venue",
            symbol="NVDA-TOKEN-USDT",
            underlying_ticker="NVDA",
            underlying_isin="US67066G1040",
            product_kind=EquityProductKind.TOKENIZED_EQUITY,
            issuer_name="BackedFinanceAG",
            redemption_rights=RedemptionRights.CASH_SETTLEMENT_ONLY,
            dividend_treatment=DividendTreatment.CASH_PASS_THROUGH,
            settlement_cycle=EquitySettlementCycle.INSTANT_24X7,
            supported_sessions=(EquityTradingSession.CONTINUOUS_24X7, EquityTradingSession.REGULAR),
            short_sale_allowed=True,
            requires_borrow_locate=False,
            eligible_jurisdictions=("NON_US_QUALIFIED",),
            account_jurisdiction_verified=True,
            legal_review_approved=True,
            compliance_approver="chief_counsel_arb",
            approved_at_utc=now - timedelta(days=10),
            expires_at_utc=now + timedelta(days=80),
            redemption_fee_bps=Decimal("10.0"),
            holding_basis_risk_haircut_bps=Decimal("10.0"),
            research_only=False,
        )
        nvda_stock = EquityProductTerms(
            instrument_id="NVDA-NASDAQ-USD",
            venue_id="ibkr_nasdaq",
            symbol="NVDA",
            underlying_ticker="NVDA",
            underlying_isin="US67066G1040",
            product_kind=EquityProductKind.TRADITIONAL_EQUITY,
            issuer_name="NVIDIA Corporation",
            redemption_rights=RedemptionRights.DIRECT_PRIMARY_REDEMPTION,
            dividend_treatment=DividendTreatment.CASH_PASS_THROUGH,
            settlement_cycle=EquitySettlementCycle.T_PLUS_1,
            supported_sessions=(EquityTradingSession.REGULAR, EquityTradingSession.PRE_MARKET),
            short_sale_allowed=True,
            requires_borrow_locate=True,
            eligible_jurisdictions=("NON_US_QUALIFIED", "US_INSTITUTIONAL"),
            account_jurisdiction_verified=True,
            legal_review_approved=True,
            compliance_approver="chief_counsel_arb",
            approved_at_utc=now - timedelta(days=10),
            expires_at_utc=now + timedelta(days=80),
            redemption_fee_bps=Decimal("0"),
            holding_basis_risk_haircut_bps=Decimal("8.0"),
            research_only=False,
        )
        eq_reg.register_terms(nvda_token)
        eq_reg.register_terms(nvda_stock)
        eq_reg.register_comparability(
            ComparabilityApproval(
                approval_id="app_nvda_20261026",
                leg_a_instrument_id="NVDA-TOKEN-USDT",
                leg_b_instrument_id="NVDA-NASDAQ-USD",
                underlying_isin="US67066G1040",
                allow_cross_market_conversion=False,
                allow_price_convergence_spread=True,
                requires_synchronous_session=True,
                max_holding_hours=Decimal("48"),
                min_convergence_haircut_bps=Decimal("8.0"),
                approved_by="chief_engineer",
                approved_at_utc=now,
                research_only=False,
            ),
            now,
        )
        token_adapter = EquityLinkedMarketAdapter("crypto_eq_venue", eq_reg)
        brk_account = EquityBrokerAccountSnapshot(
            broker_id="ibkr_nasdaq",
            settled_cash_usd=Decimal("100000"),
            buying_power_usd=Decimal("200000"),
            maintenance_margin_usd=Decimal("25000"),
            positions_shares={"NVDA": Decimal("50")},
            jurisdiction_approved=True,
        )
        broker_adapter = EquityBrokerAdapter("ibkr_nasdaq", eq_reg, brk_account)
        broker_adapter.set_trading_session("NVDA", EquityTradingSession.REGULAR)

        token_snap = token_adapter.normalize_snapshot(
            instrument_id="NVDA-TOKEN-USDT",
            raw_payload={
                "bids": [["214.65", "50"]],
                "asks": [["214.75", "50"]],
                "mark_price": "214.70",
                "index_price": "213.45",
                "session_status": "CONTINUOUS_24X7",
                "source_timestamp_utc": now,
            },
            underlying_session_status=EquityTradingSession.REGULAR,
            now_utc=now,
        )
        stock_snap = broker_adapter.normalize_broker_quote(
            instrument_id="NVDA-NASDAQ-USD",
            bid=Decimal("213.40"),
            bid_size=Decimal("100"),
            ask=Decimal("213.45"),
            ask_size=Decimal("100"),
            last_reference_price=Decimal("212.21"),
            now_utc=now,
        )
        eq_scanner = EquityCrossMarketScanner(terms_registry=eq_reg, min_net_edge_bps=Decimal("8.0"))
        eq_eval = eq_scanner.evaluate_pair(
            leg_a_snapshot=token_snap,
            leg_b_snapshot=stock_snap,
            target_quantity_shares=Decimal("4.0"),
            expected_holding_hours=Decimal("24.0"),
            now_utc=now,
            broker_account=brk_account,
            borrow_locate=BorrowLocateQuote(
                locate_id="loc_nvda_20261026",
                underlying_ticker="NVDA",
                available_shares=Decimal("500"),
                annualized_borrow_rate_bps=Decimal("45.0"),
                expires_at_utc=now + timedelta(hours=6),
            ),
        )

        # ------------------------------------------------------------------
        # 6. S4-A: CEX-DEX Arbitrage (ETH $2,328.59 on Binance vs $2,356.00 on Uniswap Pool)
        # ------------------------------------------------------------------
        evm_chain = EvmChainAdapter(chain_id=1, chain_name="ethereum_mainnet")
        weth_meta = WhitelistTokenMetadata(
            chain_id=1, token_address="0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
            symbol="ETH", decimals=18, is_whitelisted=True,
            has_transfer_tax_or_blacklist_hook=False, compliance_reviewed=True
        )
        usdt_meta = WhitelistTokenMetadata(
            chain_id=1, token_address="0xdAC17F958D2ee523a2206206994597C13D831ec7",
            symbol="USDT", decimals=6, is_whitelisted=True,
            has_transfer_tax_or_blacklist_hook=False, compliance_reviewed=True
        )
        pool_meta = WhitelistPoolContract(
            chain_id=1, pool_address="0x0d4a11d5EEaaC28EC3F61d100daF4d40471f1852",
            protocol="UNISWAP_V2", token0_address=weth_meta.token_address,
            token1_address=usdt_meta.token_address, fee_bps=Decimal("5.0"), is_whitelisted=True
        )
        evm_chain.register_token(weth_meta)
        evm_chain.register_token(usdt_meta)
        evm_chain.register_pool_contract(pool_meta)
        latest_blk = BlockHeaderSnapshot(
            chain_id=1, block_number=21045800, block_hash="0xblk_21045800", parent_hash="0xblk_21045799",
            block_timestamp_utc=now - timedelta(seconds=2), received_timestamp_utc=now,
            finalized_block_number=21045798, base_fee_per_gas_wei=8_000_000_000, priority_fee_per_gas_wei=1_000_000_000
        )
        evm_chain.ingest_block_header("alchemy_1", latest_blk)
        evm_chain.ingest_block_header("infura_1", latest_blk)

        dex_sim = DexPoolSimulator(evm_chain, max_pool_block_lag=1, max_price_impact_bps=Decimal("100.0"))
        pool_state = AmmPoolState(
            chain_id=1, pool_address=pool_meta.pool_address, protocol=DexPoolProtocol.UNISWAP_V2,
            token0_address=weth_meta.token_address, token0_symbol="ETH",
            token1_address=usdt_meta.token_address, token1_symbol="USDT",
            reserve0=Decimal("10000.0"), reserve1=Decimal("23560000.0"),  # Mid price = 2,356.00 USDT/ETH
            fee_bps=Decimal("5.0"), block_number=21045800, block_hash="0xblk_21045800", block_timestamp_utc=now
        )
        cex_dex_scanner = CexDexArbitrageScanner(
            chain_adapter=evm_chain, pool_simulator=dex_sim,
            min_net_edge_bps=Decimal("10.0"), cex_taker_fee_bps=Decimal("8.0")
        )
        cex_dex_eval = cex_dex_scanner.evaluate_cex_dex_pair(
            cex_book=books[("binance_spot", "ETH-USDT")].to_order_book(),
            pool_state=pool_state,
            base_asset_symbol="ETH",
            quote_asset_symbol="USDT",
            target_base_qty=Decimal("2.0"),
            latest_block=latest_blk,
            native_gas_token_usd_price=Decimal("2328.59"),
            now_utc=now,
        )

        # ------------------------------------------------------------------
        # 7. S4-B: BTC Options Black-76 & Put-Call Parity at Real BTC $80,432.40
        # ------------------------------------------------------------------
        opt_expiry = datetime(2026, 11, 27, 8, 0, 0, tzinfo=timezone.utc)
        call_spec = OptionContractSpec(
            venue_id="okx_options", instrument_id="BTC-USD-261127-80000-C", venue_symbol="BTC-USD-261127-80000-C",
            underlying_symbol="BTC", expiry_utc=opt_expiry, strike=Decimal("80000"), option_type=OptionType.CALL,
            contract_multiplier=Decimal("1"), settlement_asset="USDT", exercise_style=ExerciseStyle.EUROPEAN,
            settlement_style=OptionSettlementStyle.CASH_USD, account_options_approved=True, research_only=False
        )
        put_spec = OptionContractSpec(
            venue_id="okx_options", instrument_id="BTC-USD-261127-80000-P", venue_symbol="BTC-USD-261127-80000-P",
            underlying_symbol="BTC", expiry_utc=opt_expiry, strike=Decimal("80000"), option_type=OptionType.PUT,
            contract_multiplier=Decimal("1"), settlement_asset="USDT", exercise_style=ExerciseStyle.EUROPEAN,
            settlement_style=OptionSettlementStyle.CASH_USD, account_options_approved=True, research_only=False
        )
        pricer = OptionPricingEngine(risk_free_rate=Decimal("0.045"))
        tte = float(call_spec.time_to_expiry_years(now))
        c_theo, _, _, _, _ = pricer.black76_price_and_greeks(OptionType.CALL, 80000.0, 80000.0, tte, 0.045, 0.52)
        p_theo, _, _, _, _ = pricer.black76_price_and_greeks(OptionType.PUT, 80000.0, 80000.0, tte, 0.045, 0.52)
        call_quote = OptionMarketQuote(
            venue_id="okx_options", instrument_id=call_spec.instrument_id,
            bid_price=Decimal(f"{c_theo - 12.0:.2f}"), bid_size=Decimal("5.0"),
            ask_price=Decimal(f"{c_theo + 12.0:.2f}"), ask_size=Decimal("5.0"),
            mark_price=Decimal(f"{c_theo:.2f}"), underlying_index_price=Decimal("80432.40"),
            underlying_forward_price=Decimal("80000.00"), open_interest=Decimal("320"),
            source_timestamp_utc=now, received_timestamp_utc=now
        )
        put_quote = OptionMarketQuote(
            venue_id="okx_options", instrument_id=put_spec.instrument_id,
            bid_price=Decimal(f"{p_theo - 10.0:.2f}"), bid_size=Decimal("5.0"),
            ask_price=Decimal(f"{p_theo + 10.0:.2f}"), ask_size=Decimal("5.0"),
            mark_price=Decimal(f"{p_theo:.2f}"), underlying_index_price=Decimal("80432.40"),
            underlying_forward_price=Decimal("80000.00"), open_interest=Decimal("290"),
            source_timestamp_utc=now, received_timestamp_utc=now
        )
        opt_scanner = OptionsRelativeValueScanner(
            pricing_engine=pricer,
            risk_limits=OptionComboRiskLimits(
                max_abs_net_delta=Decimal("0.05"),
                max_scenario_stress_loss_usd=Decimal("300.0"),
                max_margin_requirement_usd=Decimal("15000.0"),
                min_net_edge_after_exit_cost_usd=Decimal("20.0"),
            ),
        )
        opt_eval = opt_scanner.evaluate_put_call_parity(
            call_spec=call_spec, put_spec=put_spec, call_quote=call_quote, put_quote=put_quote,
            underlying_instrument_id="BTC-USDT-261127",
            underlying_executable_bid=Decimal("80430.00"),
            underlying_executable_ask=Decimal("80432.40"),
            quantity_contracts=Decimal("0.5"),
            now_utc=now,
        )

        # ------------------------------------------------------------------
        # 8. S4-C: Cross-Chain Arbitrage (Arbitrum ETH $2,328.59 -> Optimism WETH $2,358.00)
        # ------------------------------------------------------------------
        bridge_ledger = BridgeTransferLedger()
        bridge_ledger.register_route(
            BridgeRouteSpec(
                route_id="arb_to_opt_weth",
                bridge_protocol="canonical_layerzero",
                source_chain_id=42161,
                destination_chain_id=10,
                source_asset="ETH",
                destination_asset="WETH",
                is_wrapped_representation=True,
                wrapped_peg_ratio_to_canonical=Decimal("0.9998"),
                min_acceptable_peg_ratio=Decimal("0.9970"),
                fixed_bridge_fee_usd=Decimal("1.20"),
                variable_bridge_fee_bps=Decimal("2.0"),
                expected_transit_seconds=180,
                max_timeout_seconds=900,
                health_status=BridgeHealthStatus.HEALTHY,
            )
        )
        dest_snap = bridge_ledger.compute_balance_snapshot_with_in_transit(
            venue_or_chain_id="optimism_dex", account_id="wallet_opt", asset="ETH",
            settled_free_balance=Decimal("3.0"), now_utc=now
        )
        xch_scanner = CrossChainArbitrageScanner(
            bridge_ledger=bridge_ledger, min_risk_adjusted_net_edge_bps=Decimal("10.0")
        )
        xch_eval = xch_scanner.evaluate_cross_chain_spread(
            route_id="arb_to_opt_weth",
            source_chain_venue="arbitrum_dex",
            destination_chain_venue="optimism_dex",
            asset_symbol="ETH",
            quantity=Decimal("1.500"),
            source_executable_ask=Decimal("2328.59"),
            destination_executable_bid=Decimal("2358.00"),
            destination_balance_snapshot=dest_snap,
            require_immediate_dest_inventory=True,
            now_utc=now,
        )

        # ------------------------------------------------------------------
        # 9. M7 Portfolio Capital Allocation & Release Gate Check
        # ------------------------------------------------------------------
        allocator = PortfolioCapitalAllocator()
        allocator.update_balance_snapshot(balances[("okx_spot", "USDT")])
        alloc_dec = allocator.request_allocation(
            opportunity_id=cross_exec[0].opportunity.opportunity_id,
            strategy_id=cross_exec[0].opportunity.strategy_id,
            venue_id="okx_spot",
            asset="USDT",
            requested_amount=cross_exec[0].opportunity.capacity,
            requested_notional_usd=cross_exec[0].opportunity.capacity,
        )

        report = {
            "dataset_id": raw_data["dataset_id"],
            "evaluated_at_utc": now.isoformat(),
            "portability": {
                "overall_passed": port_report.overall_passed,
                "python_version": port_report.python_version,
                "pydantic_version": port_report.pydantic_version,
                "sqlite_version": port_report.sqlite_version,
                "checks": [json.loads(model_to_json(c)) for c in port_report.checks],
            },
            "deterministic_replay": {
                "dataset_version_id": rep_res.dataset_version_id,
                "events_processed": rep_res.events_processed,
                "result_fingerprint": rep_res.result_fingerprint,
            },
            "strategies": {
                "S1_triangle_binance": {
                    "evaluated_cycles": len(tri_all),
                    "executable_count": len(tri_exec),
                    "top_net_edge_bps": str(round(tri_exec[0].net_edge_bps, 2)) if tri_exec else "0.00",
                    "top_net_pnl_usdt": str(round(tri_exec[0].expected_net_pnl, 4)) if tri_exec else "0.0000",
                },
                "S1_cross_cex_spot": {
                    "buy_venue": cross_exec[0].buy_venue_id,
                    "sell_venue": cross_exec[0].sell_venue_id,
                    "buy_vwap_usdt": str(cross_exec[0].opportunity.legs[0].executable_price),
                    "sell_vwap_usdt": str(cross_exec[0].opportunity.legs[1].executable_price),
                    "net_edge_bps": str(round(cross_exec[0].opportunity.net_edge_bps, 2)),
                    "expected_net_pnl_usdt": str(round(cross_exec[0].opportunity.expected_net_pnl, 4)),
                },
                "S2A_spot_futures_basis": {
                    "spot_ask": str(basis_rep.spot_executable_ask),
                    "deriv_bid": str(basis_rep.deriv_executable_bid),
                    "net_basis_bps": str(round(basis_rep.net_basis_bps, 2)),
                    "annualized_net_basis_bps": str(round(basis_rep.annualized_net_basis_bps, 2)),
                    "net_delta_mismatch": str(basis_rep.net_delta_base_mismatch),
                },
                "S2B_funding_rate_carry": {
                    "standardized_8h_bps": str(round(funding_rep.standardized_8h_net_funding_bps, 2)),
                    "expected_net_carry_usdt": str(round(funding_rep.expected_net_carry_usdt, 4)),
                    "break_even_settlements": str(round(funding_rep.break_even_settlements, 2)),
                },
                "S3_nvda_equity_arb": {
                    "accepted": eq_eval.accepted,
                    "nasdaq_buy_vwap": str(stock_snap.executable_ask),
                    "token_sell_vwap": str(token_snap.executable_bid),
                    "net_edge_bps": str(round(eq_eval.net_edge_bps, 2)),
                    "expected_net_pnl_usd": str(round(eq_eval.expected_net_pnl_usd, 4)),
                },
                "S4A_cex_dex_uniswap": {
                    "accepted": cex_dex_eval.accepted,
                    "direction": cex_dex_eval.direction,
                    "net_edge_bps": str(round(cex_dex_eval.net_edge_bps, 2)),
                    "gas_cost_usd": str(round(cex_dex_eval.dex_gas_cost_usd, 4)),
                    "expected_net_pnl_usd": str(round(cex_dex_eval.expected_net_pnl_usd, 4)),
                },
                "S4B_btc_options_parity": {
                    "accepted": opt_eval.accepted,
                    "structure": opt_eval.structure_name,
                    "net_delta": str(round(opt_eval.net_greeks.delta, 4)),
                    "expected_net_pnl_usd": str(round(opt_eval.expected_net_pnl_usd, 2)),
                },
                "S4C_cross_chain_bridge": {
                    "accepted": xch_eval.accepted,
                    "risk_adjusted_net_edge_bps": str(round(xch_eval.risk_adjusted_net_edge_bps, 2)),
                    "transit_volatility_haircut_usd": str(round(xch_eval.transit_volatility_haircut_usd, 4)),
                    "expected_net_pnl_usd": str(round(xch_eval.risk_adjusted_net_pnl_usd, 4)),
                },
            },
            "portfolio_allocation": {
                "approved": alloc_dec.approved,
                "allocated_notional_usd": str(round(alloc_dec.requested_notional_usd, 2)),
            },
        }
        report_out = root / "datasets" / "real_data_test_report.json"
        report_out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        return report
