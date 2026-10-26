# [EXEC-002] 订单、成交与余额对账及崩溃恢复引擎

## 1. 元信息（Metadata）

- **Issue ID**：`EXEC-002`
- **标题**：订单、成交与余额对账及崩溃恢复引擎
- **工作流（Workstream）**：`EXEC`
- **负责人（DRI）**：Gemini（总工程师）
- **审阅人（Reviewer）**：Gemini（架构与风控负责人）
- **优先级（Priority）**：`P0`
- **风险等级（Risk Level）**：`Critical`
- **所属 Sprint**：`Sprint 5 — 风控、订单状态机、对账、Paper Engine 与故障演练`
- **关联策略 / 场景**：`ALL`
- **关联文档章节**：
  - 《总体架构与研发路线图》：`3.2 (H) Ledger & Reconciliation Service`、`5.2 CEX 订单状态机`、`Sprint 5`
  - 《策略专项验收矩阵与发布门禁》：`2.1 CORE-004 / CORE-006 / CORE-010`
- **状态（Status）**：`CLOSED (Verified)`

## 2. 背景与目标（Context & Objective）

- **背景**：当订单处于 `UNKNOWN`、WebSocket 丢包漏推成交或本地账本余额与远端账户发生漂移时，必须由对账引擎主动查询远端真相源完成收敛或熔断。
- **目标**：实现 `ReconciliationEngine`（`src/arb/reconciliation/engine.py`），支持单次查单未命中容忍重试、漏推 Fill 幂等补录、订单终态收敛解锁以及余额偏差自动熔断（`CORE-010`）。

## 3. 范围与交付物（Scope & Deliverables）

- **交付物清单**：
  - [x] 代码/模块：`src/arb/reconciliation/engine.py`（`ReconciliationEngine`、`ReconciliationReport`、`ReconciliationAction`）
  - [x] 数据库/表结构：`src/arb/ledger/migrations/0001_initial_ledger_schema.sql`（新增 `reconciliation_discrepancies` 表）
  - [x] 自动化测试：`tests/unit/test_sprint5_execution_and_risk.py::TestSprint5ExecutionAndRisk::test_exec_002_reconciliation_engine_and_circuit_breakers`

## 4. 强约束与安全不变量（Hard Constraints & Invariants）

- [x] **单次未命中容忍**：针对交易所刚超时后的最终一致性延迟，首次 `query_order` 404 返回 `RETRY_LATER_NOT_FOUND_ONCE`，连续第二次未命中才判定异常并转入 `HALT_STRATEGY_MANUAL_INTERVENTION`。
- [x] **先补 Fill 后核对累计量**：对账时先拉取远端 `fetch_fills` 幂等写入账本，再校验本地累计成交量与远端订单 `cumulative_filled_qty` 是否严格相等。
- [x] **余额漂移硬熔断（`CORE-010`）**：当本地预期余额与远端余额差值超过容差时，立即写入 `reconciliation_discrepancies` 并同时激活 `VENUE` 与 `STRATEGY` 级 Kill Switch。

## 5. 验收标准（Acceptance Criteria / Gherkin）

- [x] **场景 1（单次未命中容忍与连续未命中熔断）**：首次查询不存在订单返回 `RETRY_LATER_NOT_FOUND_ONCE`，第二次查询触发策略熔断。
- [x] **场景 2（UNKNOWN 订单与后台成交恢复）**：对因超时进入 `UNKNOWN` 但实际已在远端成交的订单执行对账，自动找回 1 笔漏推成交、将周期标记为 `SETTLED` 并解除策略临时冻结。
- [x] **场景 3（余额不一致熔断 `CORE-010`）**：当预期余额与远端实际余额不一致时，`reconcile_balances` 立即触发 `VENUE:binance_spot` 与 `STRATEGY:strat_s1` 熔断。

## 6. 实测验证结果（Verification Evidence）

- **测试命令**：`PYTHONPATH=src python3 -m unittest discover -s tests/unit -p "test_*.py" -v`
- **实测结论**：`test_exec_002_reconciliation_engine_and_circuit_breakers ... ok`
