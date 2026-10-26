# [STRAT-003] 开发跨交易所现货套利扫描与库存可执行性检查

- **当前状态**：`CLOSED (已验收)`
- **完成时间**：`2026-10-26`

## 背景与目标
实现跨交易所现货套利扫描器与预置库存可执行性引擎 (`CrossCexSpotScanner`)，支持 `binance_spot` 与 `okx_spot` 双向扫描，严格采用低价所 `Ask` 买入、高价所 `Bid` 卖出，强制要求双边预置可用库存（严禁假设跨所转币即时完成），全面覆盖专项验收矩阵 `S1-001` 至 `S1-009` 及 `S1-011`。

## 负责人角色
- DRI：Gemini (总工程师兼实施人)
- Reviewer：Gemini (架构与量化风控总工审查)
- QA：Gemini (自动化单元与合同测试验收)

## 优先级与估算
- Priority：P1
- Estimate：8 SP
- Sprint：Sprint 4 (S1 现货套利)
- Labels：`strategy,cross-exchange,p1`

## 依赖
- Blocked by：VENUE-001, STRAT-001, COST-001, MD-004
- Blocks：EXEC-003, PORT-001

## 接口定义
- 输入：跨所交易对映射 (`CrossCexPairMapping`)、双边实时/回放 `OrderBookBuilder`、双边账户余额快照 (`BalanceSnapshot`)、可选跨币种汇率报价 (`FxRateQuote`) 与各所充提状态 (`VenueTransferStatus`)。
- 输出：按预期净收益降序排列的 `CrossCexScanReport`（含 `PAPER` 候选 `Opportunity`、买卖场所标识及 `CrossCexInventoryState` 库存偏离/再平衡诊断）。
- 错误/拒绝语义：1) 任一侧盘口过期或跨所时间差 `> 250ms` 拒绝 (`S1-006`)；2) 任一侧无可用预置库存或被其他批次预留导致 `allocatable_balance == 0` 时拒绝 (`INSUFFICIENT_PREPOSITIONED_INVENTORY`, `S1-005`/`S1-011`)；3) 库存不足以覆盖目标数量时自动按双边较粗 `step_size` 向下缩减规模，若缩减后低于 `min_qty` 或 `min_notional` 则拒绝 (`S1-003`/`S1-005`)；4) 两所报价币种不同且缺少可靠 `FxRateQuote` 时拒绝 (`UNRELIABLE_FX_CONVERSION`, `S1-009`)；5) 扣除双边各自费率 (`S1-002`) 与汇率成本后净收益 `<= 0` 或 `< 5.0 bps` 拒绝。
- 版本/时间戳/单位：`strategy_id='strat_s1_cross_cex_spot'`, `strategy_version='1.0.0'`，硬性锁定 `OpportunityStatus.PAPER`。
- 与其他模块的边界：纯候选生成与库存可行性评估器，不直接发起下单或提币请求；充提暂停不影响预置库存交易但自动标记 `rebalance_blocked_by_transfer_pause=True` (`S1-007`)。

## 代码与文档交付物
- [x] 代码路径：`src/arb/strategies/cross_cex_spot/scanner.py`, `src/arb/strategies/cross_cex_spot/__init__.py`
- [x] 配置/迁移：`max_venue_inventory_skew_ratio=0.80`, `min_net_edge_bps=5.0`
- [x] 文档/ADR：`CrossCexInventoryState` 库存偏离告警与充提受阻诊断规范
- [x] 指标/日志/告警：`post_trade_buy_venue_base_share`, `rebalance_alert_triggered`, `rebalance_blocked_by_transfer_pause`

## 测试用例
- [x] 正常路径
- [x] 边界条件
- [x] 异常/超时/断线
- [x] 重复/乱序/幂等（适用时）
- [x] 回放/参考实现（策略任务适用时）
- [x] 安全与权限（外部 API/资金操作适用时）
- **自动化测试证据**：`test_strat003_and_s1_cross_cex_spot_arbitrage_and_inventory_gates` (PASSED — 完整验证 `S1-001`, `S1-002`, `S1-003`, `S1-005`, `S1-006`, `S1-007`, `S1-008`, `S1-009`, `S1-011`)

## 验收条件
- 所有接口契约、类型校验、安全门禁与自动化测试用例 100% 通过。
- 严格遵循 `LIVE_TRADING_ENABLED=false` 默认硬门禁与 Fail-Closed 原则。

## 风险与回滚
- 已知风险与回滚方案：严格基于双边现货预置库存与 `PAPER` 模式运行，零跨所提币在途风险。
- 是否影响 live trading（默认必须为否）：**否 (`LIVE_TRADING_ENABLED=false`)**

## PR 验收
- [x] CI 通过 (`Ran 22 tests - OK`)
- [x] 测试证据附在 PR
- [x] 日志/指标/错误处理完整
- [x] 文档与配置同步更新
- [x] 无绕过风控/账本/实盘门禁的路径
- [x] Reviewer 与 QA 批准 (Signed off by Gemini)
