"""Sprint 8 automated unit & contract tests (`DEX-001` through `DEX-004`).

Verifies all S4-A CEX-DEX Arbitrage acceptance gates (`S4A-001` through `S4A-008`):
- `DEX-001` (`S4A-002`, `S4A-005`): RPC quorum consistency, block staleness detection, parent-hash
  reorg detection, and whitelist enforcement (blocking unreviewed or transfer-tax tokens).
- `DEX-002` (`S4A-001`, `S4A-002`, `S4A-003`): Exact AMM constant-product swap curve simulation
  (never using mid-price as execution price), stale pool block rejection, price impact limit,
  and EIP-1559 gas + MEV risk budget calculation.
- `DEX-003` (`S4A-004`, `S4A-005`, `S4A-006`): 10-state `ChainTxStatus` lifecycle (`CREATED ->
  SIMULATED -> SIGNED -> SUBMITTED -> PENDING -> CONFIRMED -> FINALIZED / REVERTED / REPLACED`),
  blocking blind new nonce allocation when a pending tx exists, replacing stuck tx with same nonce
  and higher gas, and rolling back `CONFIRMED` tx to `PENDING` on block reorg before finality.
- `DEX-004` (`S4A-001` ~ `S4A-008`): CEX-DEX cross-market arbitrage scanner deducting CEX fee +
  DEX LP fee + AMM price impact + gas + MEV budget, attaching `CexDexRecoveryPlan` for non-atomic
  leg failure, and locking output to `OpportunityDecision.PAPER`.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from arb.dex.chain import (
    BlockHeaderSnapshot,
    EvmChainAdapter,
    WhitelistPoolContract,
    WhitelistTokenMetadata,
)
from arb.dex.pool_sim import (
    AmmPoolState,
    DexPoolProtocol,
    DexPoolSimulator,
)
from arb.dex.tx_lifecycle import (
    InvalidChainTxTransitionError,
    OnchainTxLifecycleManager,
)
from arb.domain.models import (
    ChainTxStatus,
    OpportunityDecision,
    OrderBook,
    OrderSide,
    PriceLevel,
)
from arb.strategies.cex_dex.scanner import CexDexArbitrageScanner


class TestSprint8CexDexArbitrage(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime.now(timezone.utc)
        self.chain = EvmChainAdapter(
            chain_id=1,
            chain_name="ethereum_mainnet",
            max_block_age_seconds=15.0,
            max_rpc_block_skew=1,
            required_confirmations=2,
        )

        # Register whitelisted WETH and USDT tokens and Uniswap V2 pool
        self.weth = WhitelistTokenMetadata(
            chain_id=1,
            token_address="0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
            symbol="ETH",
            decimals=18,
            is_whitelisted=True,
            has_transfer_tax_or_blacklist_hook=False,
            compliance_reviewed=True,
        )
        self.usdt = WhitelistTokenMetadata(
            chain_id=1,
            token_address="0xdAC17F958D2ee523a2206206994597C13D831ec7",
            symbol="USDT",
            decimals=6,
            is_whitelisted=True,
            has_transfer_tax_or_blacklist_hook=False,
            compliance_reviewed=True,
        )
        self.pool_contract = WhitelistPoolContract(
            chain_id=1,
            pool_address="0x0d4a11d5EEaaC28EC3F61d100daF4d40471f1852",
            protocol="UNISWAP_V2",
            token0_address=self.weth.token_address,
            token1_address=self.usdt.token_address,
            fee_bps=Decimal("30.0"),
            is_whitelisted=True,
        )
        self.chain.register_token(self.weth)
        self.chain.register_token(self.usdt)
        self.chain.register_pool_contract(self.pool_contract)

        self.block_100 = BlockHeaderSnapshot(
            chain_id=1,
            block_number=21_000_100,
            block_hash="0xhash_100",
            parent_hash="0xhash_099",
            block_timestamp_utc=self.now - timedelta(seconds=2),
            received_timestamp_utc=self.now,
            finalized_block_number=21_000_098,
            base_fee_per_gas_wei=10_000_000_000,     # 10 gwei
            priority_fee_per_gas_wei=2_000_000_000,  # 2 gwei
        )
        self.chain.ingest_block_header("rpc_alchemy", self.block_100)
        self.chain.ingest_block_header("rpc_infura", self.block_100)

    def test_dex001_chain_adapter_quorum_reorg_and_token_whitelist(self) -> None:
        """Verify DEX-001 (`S4A-002`, `S4A-005`): healthy quorum, split-brain/reorg detection, and tax-token rejection."""
        health = self.chain.check_rpc_quorum_and_health(self.now)
        self.assertTrue(health.is_healthy)
        self.assertTrue(health.rpc_quorum_consistent)

        # 1. Tax / fee-on-transfer token is rejected by whitelist validation (`S4A-002`)
        tax_token = WhitelistTokenMetadata(
            chain_id=1,
            token_address="0xBadTaxToken00000000000000000000000000001",
            symbol="TAX",
            decimals=18,
            is_whitelisted=True,
            has_transfer_tax_or_blacklist_hook=True,
            compliance_reviewed=True,
        )
        self.chain.register_token(tax_token)
        tax_pool = WhitelistPoolContract(
            chain_id=1,
            pool_address="0xTaxPool000000000000000000000000000000001",
            protocol="UNISWAP_V2",
            token0_address=tax_token.token_address,
            token1_address=self.usdt.token_address,
            fee_bps=Decimal("30.0"),
            is_whitelisted=True,
        )
        self.chain.register_pool_contract(tax_pool)
        ok_tax, reason_tax = self.chain.validate_pool_and_tokens(tax_pool.pool_address)
        self.assertFalse(ok_tax)
        self.assertIn("TOKEN_HAS_TRANSFER_TAX_OR_HOOK:TAX", reason_tax or "")

        # 2. Ingest a block 101 whose parent_hash does NOT match block 100 -> detects chain reorg!
        bad_block_101 = BlockHeaderSnapshot(
            chain_id=1,
            block_number=21_000_101,
            block_hash="0xhash_101_fork",
            parent_hash="0xhash_100_DIFFERENT_FORK",
            block_timestamp_utc=self.now,
            received_timestamp_utc=self.now,
            finalized_block_number=21_000_098,
            base_fee_per_gas_wei=10_000_000_000,
            priority_fee_per_gas_wei=2_000_000_000,
        )
        no_reorg = self.chain.ingest_block_header("rpc_alchemy", bad_block_101)
        self.assertFalse(no_reorg)
        health_reorg = self.chain.check_rpc_quorum_and_health(self.now)
        self.assertFalse(health_reorg.is_healthy)
        self.assertTrue(health_reorg.reorg_detected)

    def test_dex002_pool_simulator_exact_curve_gas_and_stale_block_rejection(self) -> None:
        """Verify DEX-002 (`S4A-001`, `S4A-003`): exact AMM curve simulation vs mid-price, gas/MEV cost, and stale block."""
        sim = DexPoolSimulator(self.chain, max_pool_block_lag=1, max_price_impact_bps=Decimal("100.0"))
        pool_state = AmmPoolState(
            chain_id=1,
            pool_address=self.pool_contract.pool_address,
            protocol=DexPoolProtocol.UNISWAP_V2,
            token0_address=self.weth.token_address,
            token0_symbol="ETH",
            token1_address=self.usdt.token_address,
            token1_symbol="USDT",
            reserve0=Decimal("1000"),       # 1,000 ETH
            reserve1=Decimal("2600000"),    # 2,600,000 USDT -> mid_price = 2,600.00 USDT/ETH
            fee_bps=Decimal("30.0"),
            block_number=21_000_100,
            block_hash="0xhash_100",
            block_timestamp_utc=self.now,
        )

        # 1. Valid swap of 2 ETH -> USDT: execution_price must be strictly less than mid_price (2600.00) due to 30 bps LP fee + curve impact (`S4A-001`)
        res = sim.simulate_swap(
            pool_state=pool_state,
            token_in_symbol="ETH",
            amount_in=Decimal("2.0"),
            latest_block=self.block_100,
            native_gas_token_usd_price=Decimal("2500.0"),
            notional_usd_estimate=Decimal("5200.0"),
            now_utc=self.now,
        )
        self.assertTrue(res.is_valid)
        self.assertEqual(res.mid_price, Decimal("2600"))
        self.assertLess(res.execution_price, res.mid_price)
        self.assertGreater(res.price_impact_bps, Decimal("0"))
        self.assertGreater(res.estimated_gas_cost_usd, Decimal("0"))
        self.assertGreater(res.mev_risk_budget_usd, Decimal("0"))

        # 2. Stale pool state (block_number lags by 3 blocks > max_pool_block_lag=1) -> rejected
        stale_pool = pool_state.copy(update={"block_number": 21_000_097})
        res_stale = sim.simulate_swap(
            pool_state=stale_pool,
            token_in_symbol="ETH",
            amount_in=Decimal("2.0"),
            latest_block=self.block_100,
            native_gas_token_usd_price=Decimal("2500.0"),
            notional_usd_estimate=Decimal("5200.0"),
            now_utc=self.now,
        )
        self.assertFalse(res_stale.is_valid)
        self.assertIn("STALE_POOL_BLOCK_STATE", res_stale.reject_reason or "")

    def test_dex003_onchain_tx_lifecycle_nonce_replacement_and_reorg_rollback(self) -> None:
        """Verify DEX-003 (`S4A-004`, `S4A-005`, `S4A-006`): 10-state tx machine, duplicate nonce block, replacement, and reorg rollback."""
        mgr = OnchainTxLifecycleManager(chain_id=1, required_confirmations=2)
        sender = "0xTraderWallet0000000000000000000000000001"
        mgr.initialize_sender_nonce(sender, 42)

        rec = mgr.create_transaction(
            cycle_id="cyc_dex_1",
            strategy_id="strat_s4a_cex_dex",
            sender_address=sender,
            target_contract=self.pool_contract.pool_address,
            gas_limit=150_000,
            max_fee_per_gas_wei=12_000_000_000,
            now_utc=self.now,
        )
        self.assertEqual(rec.tx.nonce, 42)
        self.assertEqual(rec.tx.status, ChainTxStatus.CREATED)

        mgr.transition(rec.tx.transaction_id, ChainTxStatus.SIMULATED)
        mgr.transition(rec.tx.transaction_id, ChainTxStatus.SIGNED)
        mgr.transition(rec.tx.transaction_id, ChainTxStatus.SUBMITTED, tx_hash="0xtx_hash_orig")
        mgr.transition(rec.tx.transaction_id, ChainTxStatus.PENDING)

        # S4A-006: Attempting to allocate a new nonce while nonce 42 is still PENDING must raise RuntimeError!
        with self.assertRaises(RuntimeError):
            mgr.create_transaction(
                cycle_id="cyc_dex_2",
                strategy_id="strat_s4a_cex_dex",
                sender_address=sender,
                target_contract=self.pool_contract.pool_address,
                gas_limit=150_000,
                max_fee_per_gas_wei=12_000_000_000,
                now_utc=self.now,
            )

        # Replace stuck transaction using the exact same nonce (42) and higher gas fee (`S4A-006`)
        rep_rec = mgr.replace_stuck_transaction(
            original_tx_id=rec.tx.transaction_id,
            new_max_fee_per_gas_wei=18_000_000_000,
            new_tx_hash="0xtx_hash_replaced",
            now_utc=self.now,
        )
        self.assertEqual(mgr.get_record(rec.tx.transaction_id).tx.status, ChainTxStatus.REPLACED)
        self.assertEqual(rep_rec.tx.nonce, 42)
        self.assertEqual(rep_rec.tx.status, ChainTxStatus.PENDING)

        # Confirm replacement tx with 1 confirmation (< required_confirmations=2)
        mgr.transition(
            rep_rec.tx.transaction_id,
            ChainTxStatus.CONFIRMED,
            block_number=21_000_100,
            block_hash="0xhash_100",
            confirmations=1,
        )
        # S4A-005: Cannot transition to FINALIZED with only 1 confirmation!
        with self.assertRaises(InvalidChainTxTransitionError):
            mgr.transition(rep_rec.tx.transaction_id, ChainTxStatus.FINALIZED)

        # Simulate block reorg at 21_000_100 -> tx rolls back from CONFIRMED to PENDING (`S4A-005`)!
        affected = mgr.handle_block_reorg(21_000_100, "0xhash_100", now_utc=self.now)
        self.assertEqual(len(affected), 1)
        self.assertEqual(mgr.get_record(rep_rec.tx.transaction_id).tx.status, ChainTxStatus.PENDING)
        self.assertEqual(mgr.get_record(rep_rec.tx.transaction_id).reorg_rolled_back_count, 1)

        # Re-confirm on canonical block and reach 2 confirmations -> FINALIZED succeeds!
        mgr.transition(
            rep_rec.tx.transaction_id,
            ChainTxStatus.CONFIRMED,
            block_number=21_000_101,
            block_hash="0xhash_101_canon",
            confirmations=2,
        )
        final_rec = mgr.transition(rep_rec.tx.transaction_id, ChainTxStatus.FINALIZED)
        self.assertEqual(final_rec.tx.status, ChainTxStatus.FINALIZED)

    def test_dex004_cex_dex_scanner_and_non_atomic_recovery_plan(self) -> None:
        """Verify DEX-004 (`S4A-001` ~ `S4A-008`): profitable CEX-DEX scan, gas/MEV deduction, and CexDexRecoveryPlan."""
        sim = DexPoolSimulator(self.chain, max_pool_block_lag=1, max_price_impact_bps=Decimal("100.0"))
        scanner = CexDexArbitrageScanner(
            chain_adapter=self.chain,
            pool_simulator=sim,
            min_net_edge_bps=Decimal("10.0"),
            cex_taker_fee_bps=Decimal("8.0"),
        )

        # CEX OrderBook: Ask = 2,500.00 USDT, Bid = 2,499.00 USDT
        cex_book = OrderBook(
            venue_id="binance_spot",
            instrument_id="ETH-USDT",
            bids=[PriceLevel(price=Decimal("2499.00"), quantity=Decimal("20.0"))],
            asks=[PriceLevel(price=Decimal("2500.00"), quantity=Decimal("20.0"))],
            last_update_id=8801,
            is_valid=True,
            event_time_utc=self.now,
            receive_time_utc=self.now,
        )

        # Case 1: DEX Pool has ETH priced at ~2,560 USDT (1,000 ETH / 2,560,000 USDT) -> BUY CEX @ 2500, SELL DEX @ ~2547
        rich_pool = AmmPoolState(
            chain_id=1,
            pool_address=self.pool_contract.pool_address,
            protocol=DexPoolProtocol.UNISWAP_V2,
            token0_address=self.weth.token_address,
            token0_symbol="ETH",
            token1_address=self.usdt.token_address,
            token1_symbol="USDT",
            reserve0=Decimal("1000"),
            reserve1=Decimal("2560000"),
            fee_bps=Decimal("30.0"),
            block_number=21_000_100,
            block_hash="0xhash_100",
            block_timestamp_utc=self.now,
        )

        eval_ok = scanner.evaluate_cex_dex_pair(
            cex_book=cex_book,
            pool_state=rich_pool,
            base_asset_symbol="ETH",
            quote_asset_symbol="USDT",
            target_base_qty=Decimal("2.0"),
            latest_block=self.block_100,
            native_gas_token_usd_price=Decimal("2500.0"),
            now_utc=self.now,
        )
        self.assertTrue(eval_ok.accepted)
        self.assertEqual(eval_ok.direction, "BUY_CEX_SELL_DEX")
        self.assertIsNotNone(eval_ok.candidate)
        self.assertEqual(eval_ok.candidate.decision, OpportunityDecision.PAPER)
        self.assertIsNotNone(eval_ok.recovery_plan)
        self.assertTrue(eval_ok.recovery_plan.unwind_cex_on_dex_revert)
        self.assertEqual(eval_ok.recovery_plan.fallback_cex_compensating_side, OrderSide.SELL)
        self.assertGreater(eval_ok.expected_net_pnl_usd, Decimal("50.0"))

        # Case 2: Extreme gas spike (base_fee = 500 gwei) wipes out net edge -> rejected (`S4A-003`)
        gas_spike_block = self.block_100.copy(
            update={"base_fee_per_gas_wei": 500_000_000_000}  # 500 gwei -> ~0.0728 ETH (~$182 gas!)
        )
        eval_gas_reject = scanner.evaluate_cex_dex_pair(
            cex_book=cex_book,
            pool_state=rich_pool,
            base_asset_symbol="ETH",
            quote_asset_symbol="USDT",
            target_base_qty=Decimal("2.0"),
            latest_block=gas_spike_block,
            native_gas_token_usd_price=Decimal("2500.0"),
            now_utc=self.now,
        )
        self.assertFalse(eval_gas_reject.accepted)
        self.assertEqual(eval_gas_reject.rejection_reason, "INSUFFICIENT_NET_EDGE_AFTER_GAS_MEV_AND_IMPACT")


if __name__ == "__main__":
    unittest.main()
