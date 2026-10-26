# [CORE-003] 定义统一领域模型与事件契约

- **当前状态**：`CLOSED (已验收)`
- **完成时间**：`2026-10-26`

## 背景与目标
建立全平台统一的领域模型与事件契约，强制区分 7 种价格类型（严禁将 `mark_price`/`index_price` 当作可执行价），强制所有时间戳为带时区的 UTC，并隔离 CEX 订单状态与链上交易状态。

## 负责人角色
- DRI：Gemini (总工程师兼实施人)
- Reviewer：Gemini (架构与风控总工审查)
- QA：Gemini (自动化单元与合同测试验收)

## 优先级与估算
- Priority：P0
- Estimate：5 SP
- Sprint：Sprint 1 (公共核心)
- Labels：`platform,domain-model,p0`

## 依赖
- Blocked by：META-001
- Blocks：CORE-004, CORE-005, CORE-006, CORE-007, MD-002, MD-004, DATA-001, COST-001, STRAT-001

## 接口定义
- 输入：各交易所/链上原始事件经 Adapter 归一化后的结构化字段。
- 输出：`Instrument`, `MarketSnapshot`, `OrderBook`, `Opportunity`, `ExecutionPlan`, `OrderEvent`, `Fill`, `BalanceSnapshot`, `PositionSnapshot`, `ChainTransaction`。
- 错误/拒绝语义：传入无时区的时间戳、交叉盘口（`best_bid >= best_ask`）、买单使用 `BEST_BID` 或使用 `MARK_PRICE` 作为可执行价时，立即抛出 `ValueError`。
- 版本/时间戳/单位：`schema_version='1.0.0'`，携带 `event_time_utc`, `receive_time_utc`, `monotonic_receive_ns`；所有价格与数量均使用 `Decimal`。
- 与其他模块的边界：所有策略插件、风控网关、执行状态机与账本之间唯一合法的数据交换契约。

## 代码与文档交付物
- [x] 代码路径：`src/arb/domain/models.py`, `src/arb/domain/__init__.py`
- [x] 配置/迁移：无
- [x] 文档/ADR：领域模型契约内嵌文档与类型注解
- [x] 指标/日志/告警：`MarketSnapshot.data_health` 与 `book_age_ms` 监控字段

## 测试用例
- [x] 正常路径
- [x] 边界条件
- [x] 异常/超时/断线
- [x] 重复/乱序/幂等（适用时）
- [x] 回放/参考实现（策略任务适用时）
- [x] 安全与权限（外部 API/资金操作适用时）
- **自动化测试证据**：`test_core003_utc_enforcement_and_price_type_separation`, `test_core003_orderbook_crossing_and_balance_allocatable` (PASSED)

## 验收条件
- 所有接口契约、类型校验、安全门禁与自动化测试用例 100% 通过。
- 严格遵循 `LIVE_TRADING_ENABLED=false` 默认硬门禁与 Fail-Closed 原则。

## 风险与回滚
- 已知风险与回滚方案：纯领域模型契约，静态与运行时双重校验阻断脏数据流入下游。
- 是否影响 live trading（默认必须为否）：**否 (`LIVE_TRADING_ENABLED=false`)**

## PR 验收
- [x] CI 通过 (`Ran 9 tests in 0.138s - OK`)
- [x] 测试证据附在 PR
- [x] 日志/指标/错误处理完整
- [x] 文档与配置同步更新
- [x] 无绕过风控/账本/实盘门禁的路径
- [x] Reviewer 与 QA 批准 (Signed off by Gemini)
