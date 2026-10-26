# [CORE-005] 实现 Instrument Registry 与交易规则版本化

- **当前状态**：`CLOSED (已验收)`
- **完成时间**：`2026-10-26`

## 背景与目标
实现 `InstrumentRegistry` 与版本化 `TradingRules`，支持按场所隔离同名资产（如 Binance Spot `BTCUSDT` 与 OKX Swap `BTC-USDT-SWAP`），自动处理 `tick_size`、`step_size`、`min_notional` 与 `contract_multiplier` 精确量化。

## 负责人角色
- DRI：Gemini (总工程师兼实施人)
- Reviewer：Gemini (架构与风控总工审查)
- QA：Gemini (自动化单元与合同测试验收)

## 优先级与估算
- Priority：P0
- Estimate：5 SP
- Sprint：Sprint 1 (公共核心)
- Labels：`platform,instruments,p0`

## 依赖
- Blocked by：CORE-003, CORE-004
- Blocks：MD-001, COST-001, VENUE-001, DERIV-001, EQ-001, EQ-002, EQ-003

## 接口定义
- 输入：`Instrument` 元数据、下单方向、原始数量与原始价格。
- 输出：带確定性哈希 `rules_version` 的 `TradingRules` 及精确量化后的 `QuantizedOrderSpec`。
- 错误/拒绝语义：交易规则缺失、状态非 `TRADING`、规则过期（超 `max_rules_age_seconds`）或量化后不满足 `min_qty`/`min_notional` 时，抛出 `InstrumentNotTradableError` 或 `RulesExpiredError`。
- 版本/时间戳/单位：基于规则核心参数计算 SHA-256 前 12 位生成 `rules_<hash>`，绑定到每笔 `OpportunityLeg` 与 `ExecutionPlanLeg`。
- 与其他模块的边界：为 `COST-001` 成本模拟引擎与 `EXEC-001` 订单生成提供唯一合法的精度量化入口（严禁使用浮点数 `round()`）。

## 代码与文档交付物
- [x] 代码路径：`src/arb/instruments/registry.py`, `src/arb/instruments/__init__.py`
- [x] 配置/迁移：无
- [x] 文档/ADR：量化舍入规则说明（数量向零截断 `ROUND_DOWN`，买价 `ROUND_FLOOR`，卖价 `ROUND_CEILING`）
- [x] 指标/日志/告警：规则版本哈希与过期年龄检查

## 测试用例
- [x] 正常路径
- [x] 边界条件
- [x] 异常/超时/断线
- [x] 重复/乱序/幂等（适用时）
- [x] 回放/参考实现（策略任务适用时）
- [x] 安全与权限（外部 API/资金操作适用时）
- **自动化测试证据**：`test_core005_instrument_registry_and_multiplier_quantization` (PASSED)

## 验收条件
- 所有接口契约、类型校验、安全门禁与自动化测试用例 100% 通过。
- 严格遵循 `LIVE_TRADING_ENABLED=false` 默认硬门禁与 Fail-Closed 原则。

## 风险与回滚
- 已知风险与回滚方案：规则过期或停牌时自动阻断新增风险。
- 是否影响 live trading（默认必须为否）：**否 (`LIVE_TRADING_ENABLED=false`)**

## PR 验收
- [x] CI 通过 (`Ran 9 tests in 0.138s - OK`)
- [x] 测试证据附在 PR
- [x] 日志/指标/错误处理完整
- [x] 文档与配置同步更新
- [x] 无绕过风控/账本/实盘门禁的路径
- [x] Reviewer 与 QA 批准 (Signed off by Gemini)
