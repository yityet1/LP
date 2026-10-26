# ADR-0001: 首批交易场所选型（Binance + OKX）、扩展模块冻结与保守风控默认基线

- **状态**: 已批准 (Accepted)
- **日期**: 2026-10-26
- **决策者**: Gemini (总工程师)
- **关联工单**: `META-001`, `META-002`

## 1. 背景
多策略套利平台涵盖现货三角（路线 A）、期现基差（路线 B）、跨 CEX 现货（S1）、跨 CEX 衍生品（S2）、股票相关跨市场（S3）及 DEX/期权/跨链（S4）。若在 Sprint 1 启动前不锁定首批交易场所和风险边界，会导致领域模型过度泛化或适配器返工。

## 2. 决策内容
1. **第二家 CEX 锁定为 OKX (`okx_spot`, `okx_swap`)**：与首发场所 Binance (`binance_spot`, `binance_usdm`) 形成双场所组合。OKX V5 API 具备统一账户、标准化的 WebSocket L2 增量深度与清晰的错误码体系，适合作为检验 `VenueAdapter` 抽象与合约乘数差异（`ctVal`）的标杆。
2. **S3 与 S4 全线标记为 `research-only`**：在 Sprint 1–6 期间，股票相关（`EQ-*`）、链上 DEX（`DEX-*`）、期权（`OPT-*`）及跨链（`XCH-*`）在配置与代码门禁层强制设为 `mode: research_only` 且 `allow_paper_execution: false`、`allow_live_execution: false`。
3. **跨市场套利默认采用预置库存模式**：禁止任何策略假设“低买高卖后立即跨所提币或跨链转币”能在单批次内原子完成。
4. **风控基线采用保守硬默认**：
   - `LIVE_TRADING_ENABLED = false`（硬编码默认关闭，且任何非受控环境尝试开启直接拒绝启动）。
   - 单笔订单最大名义金额：`1,000 USDT`；单批次（Cycle）最大名义金额：`2,000 USDT`；单策略最大名义敞口：`10,000 USDT`；全组合最大名义敞口：`25,000 USDT`。
   - 最大未对冲敞口：`500 USDT`（持续超过 `5,000 ms` 触发策略级熔断）。
   - 最大行情盘口年龄（`max_book_age_ms`）：`500 ms`；跨市场最大时间差（`max_cross_venue_skew_ms`）：`250 ms`。
   - 最小预期净收益门槛（`min_net_edge_bps`）：扣除全部双边 taker 手续费、滑点与成本后 `>= 5.0 bps`。

## 3. 影响与后果
- `Instrument` 模型必须原生支持 `contract_multiplier` 以兼容 OKX Swap 面值。
- `VenueCapability` 配置在系统启动时加载，任何未获批场所的交易请求在 `RiskGateway` 和 `TradingAdapter` 双重拦截。
