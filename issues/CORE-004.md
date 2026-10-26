# [CORE-004] 定义 Venue Adapter 抽象接口与合同测试

- **当前状态**：`CLOSED (已验收)`
- **完成时间**：`2026-10-26`

## 背景与目标
将场所适配器解耦为四层抽象接口（`InstrumentMetadataAdapter`, `MarketDataAdapter`, `AccountAdapter`, `TradingAdapter`），建立统一的 `ExchangeError` 错误分类体系，确保写操作超时/5xx 必须进入 `UNKNOWN` 对账而非视为失败。

## 负责人角色
- DRI：Gemini (总工程师兼实施人)
- Reviewer：Gemini (架构与风控总工审查)
- QA：Gemini (自动化单元与合同测试验收)

## 优先级与估算
- Priority：P0
- Estimate：5 SP
- Sprint：Sprint 1 (公共核心)
- Labels：`platform,adapter,p0`

## 依赖
- Blocked by：CORE-003
- Blocks：CORE-005, MD-001, MD-002, VENUE-001, EXEC-001, DERIV-001, EQ-002, EQ-003, OPT-001

## 接口定义
- 输入：统一领域指令（如 `submit_order`, `query_order`, `fetch_order_book`）。
- 输出：标准领域事件（`OrderEvent`, `OrderBook`, `MarketSnapshot`, `Fill`）或带分类语义的 `ExchangeError`。
- 错误/拒绝语义：区分 `DEFINITE_REJECT`, `RATE_LIMITED` (429), `IP_BANNED` (418), `AUTH_PERMISSION` (401/403), `TRANSIENT_NETWORK`, `UNKNOWN_ORDER_STATE`。写操作遇到超时或网络中断时，`is_definite_failure == False` 且 `requires_reconciliation == True`。
- 版本/时间戳/单位：保留 `native_venue_status` 与 `native_code` 原始字段供审计排障。
- 与其他模块的边界：策略层严禁直接调用第三方交易所 SDK，必须通过四层 Adapter 抽象交互。

## 代码与文档交付物
- [x] 代码路径：`src/arb/venues/base.py`, `src/arb/venues/__init__.py`
- [x] 配置/迁移：无
- [x] 文档/ADR：接口契约定义与错误分类表
- [x] 指标/日志/告警：错误分类标签 (`category`, `venue_id`, `http_status`)

## 测试用例
- [x] 正常路径
- [x] 边界条件
- [x] 异常/超时/断线
- [x] 重复/乱序/幂等（适用时）
- [x] 回放/参考实现（策略任务适用时）
- [x] 安全与权限（外部 API/资金操作适用时）
- **自动化测试证据**：`test_core004_adapter_error_taxonomy_and_contract` (PASSED)

## 验收条件
- 所有接口契约、类型校验、安全门禁与自动化测试用例 100% 通过。
- 严格遵循 `LIVE_TRADING_ENABLED=false` 默认硬门禁与 Fail-Closed 原则。

## 风险与回滚
- 已知风险与回滚方案：严格保障超时未知订单不盲目重发。
- 是否影响 live trading（默认必须为否）：**否 (`LIVE_TRADING_ENABLED=false`)**

## PR 验收
- [x] CI 通过 (`Ran 9 tests in 0.138s - OK`)
- [x] 测试证据附在 PR
- [x] 日志/指标/错误处理完整
- [x] 文档与配置同步更新
- [x] 无绕过风控/账本/实盘门禁的路径
- [x] Reviewer 与 QA 批准 (Signed off by Gemini)
