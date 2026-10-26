# [RISK-001] 组合与策略级风控网关、资金预占与 Kill Switch

## 1. 元信息（Metadata）

- **Issue ID**：`RISK-001`
- **标题**：组合与策略级风控网关、资金预占与 Kill Switch
- **工作流（Workstream）**：`RISK`
- **负责人（DRI）**：Gemini（总工程师）
- **审阅人（Reviewer）**：Gemini（架构与风控负责人）
- **优先级（Priority）**：`P0`
- **风险等级（Risk Level）**：`Critical`
- **所属 Sprint**：`Sprint 5 — 风控、订单状态机、对账、Paper Engine 与故障演练`
- **关联策略 / 场景**：`ALL`（S1 现实现货、S2 衍生品资金费率/基差、组合级资金池）
- **关联文档章节**：
  - 《总体架构与研发路线图》：`3.2 (F) Risk & Portfolio Engine`、`5.4 风控与熔断策略`、`Sprint 5`
  - 《策略专项验收矩阵与发布门禁》：`2.1 CORE-008 ~ CORE-009`、`3.1 S1-011`、`6.1 PORT-001 ~ PORT-004`
- **状态（Status）**：`CLOSED (Verified)`

## 2. 背景与目标（Context & Objective）

- **背景**：在进入自动下单或 Paper 闭环执行前，必须由独立且不可绕过的 `RiskGateway` 执行交易前（Pre-Trade）风控检查、并发安全的资金预占（Fund Reservation）以及持久化 Kill Switch 熔断保护。
- **目标**：实现 `src/arb/risk/gateway.py` 与 `fund_reservations` 账本表，阻断同余额并发双花（`S1-011`、`PORT-001`），确保风控/数据库不可用时严格 Fail-Closed（`CORE-008`），且熔断状态跨进程重启后持久生效（`CORE-009`）。
- **非目标（Out of Scope）**：链上智能合约授权额度吊销（留待 S4 扩展模块）。

## 3. 范围与交付物（Scope & Deliverables）

- **交付物清单**：
  - [x] 代码/模块：`src/arb/risk/gateway.py`（`RiskGateway`、`AccountRiskState`、`FundReservation`、`RiskDecision`、`KillSwitchScope`、`RiskRejectCode`）
  - [x] 数据库/表结构：`src/arb/ledger/migrations/0001_initial_ledger_schema.sql`（新增 `fund_reservations` 表）
  - [x] 自动化测试：`tests/unit/test_sprint5_execution_and_risk.py::TestSprint5ExecutionAndRisk::test_risk_001_pre_trade_gateway_and_concurrency_safe_reservations`

## 4. 强约束与安全不变量（Hard Constraints & Invariants）

- [x] **Fail-Closed**：`account_state is None`、账本数据库异常或行情不健康时，一律拒绝放行（`RISK_SERVICE_UNAVAILABLE` / `MARKET_HEALTH_INVALID`）。
- [x] **并发预占隔离**：基于可重入锁与 SQLite `BEGIN IMMEDIATE` 事务将预占额度写入 `fund_reservations`，任何并发策略试图重复占用同一笔可用余额立即抛出 `INSUFFICIENT_FREE_BALANCE_FOR_RESERVATION`。
- [x] **持久化熔断**：`activate_kill_switch` 写入 `kill_switch_state` 表，重启实例后依然拦截新开仓计划，仅允许人工解除或对账恢复。

## 5. 验收标准（Acceptance Criteria / Gherkin）

- [x] **场景 1（多维限额与行情门禁放行）**：Given 账户可用余额充足且行情 `SYNCHRONIZED`，When 提交 600 USDT 跨所执行计划，Then `RiskGateway.evaluate()` 返回 `approved=True` 且包含 `PASS_ALL_GATES`。
- [x] **场景 2（同资产并发预占防超卖 `S1-011` / `PORT-001`）**：Given `binance_spot` 可用余额为 1,000 USDT，When 周期 1 预占 600 USDT 成功后周期 2 再次请求预占 600 USDT，Then 周期 2 被拒绝；When 周期 1 释放预占后，Then 周期 2 预占成功。
- [x] **场景 3（Kill Switch 跨重启持久化 `CORE-009`）**：Given 触发 `GLOBAL` 级 Kill Switch，When 重新实例化 `RiskGateway` 并评估新计划，Then 依然返回 `approved=False`（`KILL_SWITCH_ACTIVE`）。

## 6. 实测验证结果（Verification Evidence）

- **测试命令**：`PYTHONPATH=src python3 -m unittest discover -s tests/unit -p "test_*.py" -v`
- **实测结论**：`test_risk_001_pre_trade_gateway_and_concurrency_safe_reservations ... ok`
