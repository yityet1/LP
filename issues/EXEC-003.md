# [EXEC-003] Paper Matching Engine 与模拟盘端到端执行

## 1. 元信息（Metadata）

- **Issue ID**：`EXEC-003`
- **标题**：Paper Matching Engine 与模拟盘端到端执行
- **工作流（Workstream）**：`EXEC`
- **负责人（DRI）**：Gemini（总工程师）
- **审阅人（Reviewer）**：Gemini（架构与风控负责人）
- **优先级（Priority）**：`P0`
- **风险等级（Risk Level）**：`High`
- **所属 Sprint**：`Sprint 5 — 风控、订单状态机、对账、Paper Engine 与故障演练`
- **关联策略 / 场景**：`S1-A`、`S1-B`、`S2-A`、`S2-B`（所有策略 Paper Trading 阶段）
- **关联文档章节**：
  - 《总体架构与研发路线图》：`6.2 阶段二：模拟盘 / 影子盘（Paper Trading）`、`Sprint 5`
  - 《策略专项验收矩阵与发布门禁》：`1.2 G2 — 模拟盘门禁`、`3.1 S1-004`
- **状态（Status）**：`CLOSED (Verified)`

## 2. 背景与目标（Context & Objective）

- **背景**：模拟盘（Paper Trading）必须复用与实盘完全一致的 `TradingAdapter` / `AccountAdapter` 接口、`ExecutionOrchestrator` 状态机与 `LedgerStore` 账本，且绝不能在单腿失败时伪造“原子回滚”。
- **目标**：实现 `PaperVenueAdapter` 与 `PaperExecutionSimulator`（`src/arb/paper/engine.py`），支持全成、部分成交、拒单、超时 `UNKNOWN` 与滑点超限模拟，并在单腿成交另一腿失败时真实记录未对冲敞口（`unhedged_base_exposure`）并转入 `CycleState.RECOVERY`（`S1-004`）。

## 3. 范围与交付物（Scope & Deliverables）

- **交付物清单**：
  - [x] 代码/模块：`src/arb/paper/engine.py`（`PaperVenueAdapter`、`PaperExecutionSimulator`、`PaperMatchingConfig`、`PaperFillBehavior`、`PaperCycleExecutionResult`）
  - [x] 自动化测试：`tests/unit/test_sprint5_execution_and_risk.py::TestSprint5ExecutionAndRisk::test_exec_003_paper_trading_engine_both_legs_and_partial_leg_exposure`

## 4. 强约束与安全不变量（Hard Constraints & Invariants）

- [x] **严禁伪造原子回滚（`S1-004`）**：若第 1 腿在 `binance_spot` 买入成交，第 2 腿在 `okx_spot` 卖出被拒或过期，系统必须保留第 1 腿真实成交记录，计算真实 `unhedged_base_exposure`，并将周期状态置为 `CycleState.RECOVERY`。
- [x] **模拟盘显式标识**：所有模拟盘订单 ID 与成交 ID 均带 `PAPER_ORD_` / `PAPER_FILL_` / `PAPER_TRD_` 前缀，结果对象显式标记 `is_paper_simulation=True`。

## 5. 验收标准（Acceptance Criteria / Gherkin）

- [x] **场景 1（双腿正常成交闭环）**：双腿均成交时，周期状态收敛为 `CycleState.SETTLED`，`unhedged_base_exposure == 0`。
- [x] **场景 2（单腿成交、单腿拒单敞口暴露 `S1-004`）**：当 `binance_spot` 买入腿成交而 `okx_spot` 卖出腿拒单时，周期状态进入 `CycleState.RECOVERY`，`unhedged_base_exposure` 精确等于第 1 腿买入数量（`0.2000 ETH`）。

## 6. 实测验证结果（Verification Evidence）

- **测试命令**：`PYTHONPATH=src python3 -m unittest discover -s tests/unit -p "test_*.py" -v`
- **实测结论**：`test_exec_003_paper_trading_engine_both_legs_and_partial_leg_exposure ... ok`
