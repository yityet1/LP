# [COST-001] 实现逐档成交模拟、手续费、取整与容量成本引擎

- **当前状态**：`CLOSED (已验收)`
- **完成时间**：`2026-10-26`

## 背景与目标
实现全平台公用的逐档成交模拟与成本估算引擎 (`CostEngine`) 及独立参考计算器 (`ReferenceCostCalculator`)，严禁使用 `last_trade_price` 代替可执行盘口深度，严格使用 `TradingRules` Decimal 步长/价格截断（禁止 Python `round()`），并支持多等级费率与第三资产（如 BNB）手续费抵扣。

## 负责人角色
- DRI：Gemini (总工程师兼实施人)
- Reviewer：Gemini (架构与量化风控总工审查)
- QA：Gemini (自动化单元与合同测试验收)

## 优先级与估算
- Priority：P0
- Estimate：8 SP
- Sprint：Sprint 3 (策略公共组件)
- Labels：`quant,cost-model,p0`

## 依赖
- Blocked by：CORE-005, MD-003, CORE-003
- Blocks：STRAT-001, STRAT-002, STRAT-003, EXEC-003, DERIV-003, EQ-004, DEX-002, OPT-002, XCH-002

## 接口定义
- 输入：L2 `OrderBook`、`TradingRules`、交易方向 (`OrderSide`)、目标数量或报价预算 (`quote_budget`)、场所费率表 (`FeeSchedule`)。
- 输出：`SimulatedLegFill`（含 VWAP 成交均价、消耗档位数、滑点 bps、未成交残余、是否完全可执行及拒绝原因）、`FeeEstimate`（含原币种费用、结算币种折算费用、有效费率）与 `NetPnlEstimate`。
- 错误/拒绝语义：盘口无效 (`BOOK_INVALID`)、深度不足 (`INSUFFICIENT_DEPTH`)、步长截断后低于最小数量 (`STEP_OR_MIN_QTY_REJECT`) 或名义金额低于 `min_notional` (`MIN_NOTIONAL_NOT_MET`) 时，`is_fully_executable` 均返回 `False` 并标记具体拒绝原因。
- 版本/时间戳/单位：强制绑定 `rules.rules_version`；买单遍历 `book.asks`（从低到高），卖单遍历 `book.bids`（从高到低）；成交价格类型标记为 `PriceType.ESTIMATED_FILL_PRICE`。
- 与其他模块的边界：为所有现货、衍生品及跨所套利 Scanner 与 Paper 撮合器提供统一的滑点、容量和净收益核算。

## 代码与文档交付物
- [x] 代码路径：`src/arb/costs/engine.py`, `src/arb/costs/__init__.py`
- [x] 配置/迁移：`FeeSchedule`（支持按 venue/VIP 等级与 BNB 折扣率配置）
- [x] 文档/ADR：`ReferenceCostCalculator` 独立参考算例
- [x] 指标/日志/告警：`slippage_bps`, `levels_consumed`, `total_fees_in_settlement`, `net_edge_bps`

## 测试用例
- [x] 正常路径
- [x] 边界条件
- [x] 异常/超时/断线
- [x] 重复/乱序/幂等（适用时）
- [x] 回放/参考实现（策略任务适用时）
- [x] 安全与权限（外部 API/资金操作适用时）
- **自动化测试证据**：`test_cost001_multilevel_slippage_depth_and_third_asset_fees` (PASSED)

## 验收条件
- 所有接口契约、类型校验、安全门禁与自动化测试用例 100% 通过。
- 严格遵循 `LIVE_TRADING_ENABLED=false` 默认硬门禁与 Fail-Closed 原则。

## 风险与回滚
- 已知风险与回滚方案：未转换的步长残余（dust）不计入已实现结算现金，确保净收益估算严格保守。
- 是否影响 live trading（默认必须为否）：**否 (`LIVE_TRADING_ENABLED=false`)**

## PR 验收
- [x] CI 通过 (`Ran 19 tests - OK`)
- [x] 测试证据附在 PR
- [x] 日志/指标/错误处理完整
- [x] 文档与配置同步更新
- [x] 无绕过风控/账本/实盘门禁的路径
- [x] Reviewer 与 QA 批准 (Signed off by Gemini)
