# [DERIV-002] 开发 S2-A：现期基差与永续基差套利引擎

## 1. 元信息（Metadata）

- **Issue ID**：`DERIV-002`
- **标题**：开发 S2-A：现期基差与永续基差套利引擎
- **工作流（Workstream）**：`DERIV / STRAT`
- **负责人（DRI）**：Gemini（总工程师）
- **审阅人（Reviewer）**：Gemini（架构与风控负责人）
- **优先级（Priority）**：`P1`
- **风险等级（Risk Level）**：`High`
- **所属 Sprint**：`Sprint 6 — S2 衍生品基差与资金费率套利`
- **关联策略 / 场景**：`S2-A`（期现 Cash-and-Carry 与永续基差）
- **关联文档章节**：
  - 《总体架构与研发路线图》：`Sprint 6`
  - 《策略专项验收矩阵与发布门禁》：`4.1 S2-001 ~ S2-005`
- **状态（Status）**：`CLOSED (Verified)`

## 2. 背景与目标（Context & Objective）

- **背景**：期现正套（买现货 Ask + 空期货/永续 Bid）要求现货币数与合约张数×合约乘数精确对齐（Delta 中性），并扣除建仓+平仓共 4 笔手续费与滑点后计算净年化基差。
- **目标**：实现 `BasisArbitrageScanner`（`src/arb/strategies/basis/scanner.py`），支持现货与交割/永续合约基差扫描、合约乘数最小公倍数对齐、年化净基差门槛过滤以及临近到期/基差收敛平仓信号。

## 3. 范围与交付物（Scope & Deliverables）

- **交付物清单**：
  - [x] 代码/模块：`src/arb/strategies/basis/scanner.py`（`BasisArbitrageScanner`、`BasisPairSpec`、`BasisScanReport`）
  - [x] 自动化测试：`tests/unit/test_sprint6_derivatives.py::TestSprint6DerivativesAndS2Matrix::test_deriv_002_s2a_spot_futures_basis_engine_and_multiplier_alignment`

## 4. 强约束与安全不变量（Hard Constraints & Invariants）

- [x] **严禁使用 Mark Price 充当成交价（`S2-001`）**：即使传入虚高的 `mark_price_reference=70000`，扫描器依然严格按现货 L2 `Ask`（68,000）与期货 L2 `Bid`（68,680）计算可执行基差。
- [x] **零 Delta 残差对齐（`S2-002` / `S2-003`）**：自动按 `max(spot_step, deriv_step * contract_multiplier)` 向下取整，确保 `net_delta_base_mismatch == 0`。
- [x] **4 腿全往返成本扣除与到期拦截（`S2-004` / `S2-005`）**：扣除开平仓双边 4 笔 Taker 费用，且当距离交割小于 `unwind_window_hours_before_expiry`（24h）时自动阻断新开仓并触发平仓标志。

## 5. 实测验证结果（Verification Evidence）

- **测试命令**：`PYTHONPATH=src python3 -m unittest discover -s tests/unit -p "test_*.py" -v`
- **实测结论**：`test_deriv_002_s2a_spot_futures_basis_engine_and_multiplier_alignment ... ok`
