# [DERIV-003] 开发 S2-B：永续资金费率套利与保证金/强平缓冲监控

## 1. 元信息（Metadata）

- **Issue ID**：`DERIV-003`
- **标题**：开发 S2-B：永续资金费率套利与保证金/强平缓冲监控
- **工作流（Workstream）**：`DERIV / RISK`
- **负责人（DRI）**：Gemini（总工程师）
- **审阅人（Reviewer）**：Gemini（架构与风控负责人）
- **优先级（Priority）**：`P0`
- **风险等级（Risk Level）**：`Critical`
- **所属 Sprint**：`Sprint 6 — S2 衍生品基差与资金费率套利`
- **关联策略 / 场景**：`S2-B`（永续资金费率套利与强平保护）
- **关联文档章节**：
  - 《总体架构与研发路线图》：`Sprint 6`
  - 《策略专项验收矩阵与发布门禁》：`4.1 S2-006 ~ S2-012`
- **状态（Status）**：`CLOSED (Verified)`

## 2. 背景与目标（Context & Objective）

- **背景**：永续资金费率套利在极端单边行情中面临空头腿保证金不足或逼近强平价的风险；若仅单边减仓合约腿而不同步卖出对冲现货腿，会立即产生裸方向性敞口。
- **目标**：实现 `FundingRateArbitrageScanner` 与 `MarginAndLiquidationMonitor`（`src/arb/strategies/funding/scanner.py`），覆盖跨周期费率归一化、结算时间对齐校验、盈亏平衡期数核算，以及两级保证金/强平缓冲监控与成对（Paired 2-Leg）紧急减仓计划生成。

## 3. 范围与交付物（Scope & Deliverables）

- **交付物清单**：
  - [x] 代码/模块：`src/arb/strategies/funding/scanner.py`（`FundingRateArbitrageScanner`、`MarginAndLiquidationMonitor`、`MarginHealthReport`、`MarginSafetyAction`、`FundingCarryPairSpec`、`FundingCarryScanReport`）
  - [x] 自动化测试：`tests/unit/test_sprint6_derivatives.py::TestSprint6DerivativesAndS2Matrix::test_deriv_003_and_004_funding_rate_carry_margin_monitor_and_paper_closed_loop`

## 4. 强约束与安全不变量（Hard Constraints & Invariants）

- [x] **结算时钟错位拦截（`S2-007`）**：跨所永续对冲若两边 `next_funding_time_utc` 偏差超过 60s，立即拒绝（`FUNDING_SETTLEMENT_TIME_MISMATCH`）。
- [x] **两级强平缓冲门禁（`S2-009`）**：距离强平价 `< 1500 bps`（15%）或保证金率 `>= 65%` 触发 `WARN_BLOCK_NEW_OPEN` 禁止新开仓；距离强平价 `< 800 bps`（8%）或保证金率 `>= 80%` 触发 `CRITICAL_AUTO_DELEVERAGE`。
- [x] **成对双腿同步减仓（`S2-010`）**：紧急减仓计划 `emergency_unwind_plan` 强制同时生成衍生品平仓腿与现货对冲平仓腿，严禁只平单腿留下裸敞口。

## 5. 实测验证结果（Verification Evidence）

- **测试命令**：`PYTHONPATH=src python3 -m unittest discover -s tests/unit -p "test_*.py" -v`
- **实测结论**：`test_deriv_003_and_004_funding_rate_carry_margin_monitor_and_paper_closed_loop ... ok`
