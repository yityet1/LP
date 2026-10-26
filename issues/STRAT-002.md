# [STRAT-002] 开发路线 A：Binance 现货三角套利扫描器

- **当前状态**：`CLOSED (已验收)`
- **完成时间**：`2026-10-26`

## 背景与目标
实现路线 A：Binance 现货三角套利扫描器 (`TriangleArbitrageScanner`)，支持合法三腿闭环图预计算（如 `USDT -> BTC -> ETH -> USDT` 正向与反向）及 `symbol -> affected_paths` 增量索引；逐腿按真实盘口深度（买用 Ask、卖用 Bid）、交易规则步长取整、手续费与残余资产（dust）核算净收益，并与独立参考计算器 (`ReferenceCostCalculator`) 交叉核验一致。

## 负责人角色
- DRI：Gemini (总工程师兼实施人)
- Reviewer：Gemini (架构与量化风控总工审查)
- QA：Gemini (自动化单元与合同测试验收)

## 优先级与估算
- Priority：P1
- Estimate：8 SP
- Sprint：Sprint 3 (S1 现货套利)
- Labels：`strategy,triangle,p1`

## 依赖
- Blocked by：STRAT-001, COST-001, MD-004
- Blocks：EXEC-003

## 接口定义
- 输入：已注册的现货 `Instrument` 列表、实时或回放的 `OrderBookBuilder` 字典、第三资产手续费价格字典。
- 输出：通过门禁的 `PAPER` 模式可执行候选列表 (`executable_paper_pool`) 与全量评估记录（含 `REJECTED` 原因码）。
- 错误/拒绝语义：1) 任一腿交易对停牌 (`HALTED`) 拒绝 (`INSTRUMENT_NOT_TRADABLE`)；2) 任一腿盘口无效、过期或三腿时间差超 250ms 拒绝 (`MARKET_HEALTH_INVALID`)；3) 任一腿深度不足或低于 `min_notional` 拒绝；4) 净收益 `<= 0` (`NON_POSITIVE_NET_PNL`) 或低于 `min_net_edge_bps=5.0 bps` (`BELOW_MIN_NET_EDGE_BPS`) 均标记为 `REJECTED`，绝不进入可执行池。
- 版本/时间戳/单位：`strategy_id='strat_route_a_triangle_spot'`, `strategy_version='1.0.0'`，硬性锁定 `OpportunityStatus.PAPER`。
- 与其他模块的边界：纯候选扫描器，输出交由 `OpportunityLifecycleManager` 持久化，不直接调用任何交易接口。

## 代码与文档交付物
- [x] 代码路径：`src/arb/strategies/triangle/scanner.py`, `src/arb/strategies/triangle/__init__.py`
- [x] 配置/迁移：`initial_capital_quote=1000.00`, `min_net_edge_bps=5.0`
- [x] 文档/ADR：增量扫描索引 `symbol_to_cycles` 与三腿闭环推导文档
- [x] 指标/日志/告警：三腿逐档成交量、费用、残余资产与独立参考实现误差 `< 1e-6`

## 测试用例
- [x] 正常路径
- [x] 边界条件
- [x] 异常/超时/断线
- [x] 重复/乱序/幂等（适用时）
- [x] 回放/参考实现（策略任务适用时）
- [x] 安全与权限（外部 API/资金操作适用时）
- **自动化测试证据**：`test_strat002_and_s1_010_triangle_scanner_reference_match_and_incremental_scan` (PASSED — 覆盖 `S1-010` 独立参考比对、增量与全量扫描一致性、停牌过滤)

## 验收条件
- 所有接口契约、类型校验、安全门禁与自动化测试用例 100% 通过。
- 严格遵循 `LIVE_TRADING_ENABLED=false` 默认硬门禁与 Fail-Closed 原则。

## 风险与回滚
- 已知风险与回滚方案：硬性锁定 `PAPER` 决策模式，不触发任何实盘委托。
- 是否影响 live trading（默认必须为否）：**否 (`LIVE_TRADING_ENABLED=false`)**

## PR 验收
- [x] CI 通过 (`Ran 19 tests - OK`)
- [x] 测试证据附在 PR
- [x] 日志/指标/错误处理完整
- [x] 文档与配置同步更新
- [x] 无绕过风控/账本/实盘门禁的路径
- [x] Reviewer 与 QA 批准 (Signed off by Gemini)
