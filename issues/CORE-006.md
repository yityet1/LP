# [CORE-006] 建立 PostgreSQL 事件存储、订单/成交账本与迁移

- **当前状态**：`CLOSED (已验收)`
- **完成时间**：`2026-10-26`

## 背景与目标
建立兼容 PostgreSQL 与 SQLite 的事务型账本存储 (`LedgerStore`) 与 SQL 迁移脚本，通过 `cycle_id` 串联机会、风控决策、交易批次、订单、成交与费用，确保成交幂等去重与订单累计成交量单调递增。

## 负责人角色
- DRI：Gemini (总工程师兼实施人)
- Reviewer：Gemini (架构与风控总工审查)
- QA：Gemini (自动化单元与合同测试验收)

## 优先级与估算
- Priority：P0
- Estimate：8 SP
- Sprint：Sprint 1 (公共核心)
- Labels：`platform,database,p0`

## 依赖
- Blocked by：CORE-003
- Blocks：STRAT-001, RISK-001, EXEC-001, EXEC-002, DEX-003, XCH-001

## 接口定义
- 输入：`Settings`, `Opportunity`, `ExecutionPlan`, `RiskDecisionRecord`, `OrderEvent`, `Fill`, `BalanceSnapshot`, `PositionSnapshot`。
- 输出：11 张核心账本表（含 `order_events_audit` 审计表与 `kill_switch_state` 表）及 `CycleAuditTrail` 全链路追溯对象。
- 错误/拒绝语义：重复 `fill_id` 或 `(venue_id, venue_trade_id)` 幂等忽略返回 `False`；若收到使 `cumulative_filled_qty` 倒退的异常订单事件，立即抛出 `LedgerIntegrityError` 并回滚事务。
- 版本/时间戳/单位：迁移版本 `0001_initial_ledger_schema.sql`，所有金额与数量以精确十进制字符串持久化。
- 与其他模块的边界：全平台资金、订单与成交的唯一真相源（Single Source of Truth）。

## 代码与文档交付物
- [x] 代码路径：`src/arb/ledger/store.py`, `src/arb/ledger/migrations/0001_initial_ledger_schema.sql`
- [x] 配置/迁移：`LEDGER_DB_URL`
- [x] 文档/ADR：`src/arb/ledger/migrations/0001_initial_ledger_schema.sql`
- [x] 指标/日志/告警：支持按 `cycle_id` 一键导出完整审计链 `get_cycle_audit_trail()`

## 测试用例
- [x] 正常路径
- [x] 边界条件
- [x] 异常/超时/断线
- [x] 重复/乱序/幂等（适用时）
- [x] 回放/参考实现（策略任务适用时）
- [x] 安全与权限（外部 API/资金操作适用时）
- **自动化测试证据**：`test_core006_ledger_cycle_audit_and_idempotent_fills` (PASSED)

## 验收条件
- 所有接口契约、类型校验、安全门禁与自动化测试用例 100% 通过。
- 严格遵循 `LIVE_TRADING_ENABLED=false` 默认硬门禁与 Fail-Closed 原则。

## 风险与回滚
- 已知风险与回滚方案：ACID 事务保障，写失败自动回滚并可触发全局 Kill Switch。
- 是否影响 live trading（默认必须为否）：**否 (`LIVE_TRADING_ENABLED=false`)**

## PR 验收
- [x] CI 通过 (`Ran 9 tests in 0.138s - OK`)
- [x] 测试证据附在 PR
- [x] 日志/指标/错误处理完整
- [x] 文档与配置同步更新
- [x] 无绕过风控/账本/实盘门禁的路径
- [x] Reviewer 与 QA 批准 (Signed off by Gemini)
