# 交易场所、产品范围与账户能力矩阵 (Venue Capability Matrix)

- **文档版本**: `v1.0.0`
- **关联工单**: `META-001`
- **基准日期**: `2026-10-26`
- **总工程师/DRI**: Gemini (Chief Engineer)
- **状态**: **APPROVED & LOCKED**

## 1. 范围决策摘要 (Executive Summary)

根据架构治理纪律（`META-001`），所有 Sprint 1–6 的核心研发锁定在 **Binance** 与 **OKX** 两家中心化交易所（CEX）的现货（Spot）与永续合约（USDⓈ-M / USDT-M Perpetual）产品。股票相关（S3）、链上 DEX（S4-A）、期权（S4-B）及跨链桥（S4-C）统一标记为 `research-only`，在完成专项产品条款审查与合规签字前**硬性禁用实盘与自动执行路径**。

## 2. 场所与产品能力矩阵 (`VenueCapability`)

| Venue ID | 产品类型 (`instrument_type`) | 阶段状态 | 地区/账户资格 | 行情权限 (REST/WS) | 交易权限 | Sandbox / Testnet | REST 限频基线 | 结算资产 | 跨所资金转移策略 | 负责人 | 复核日期 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `binance_spot` | `spot` | **Sprint 1–3 启用 (Paper)** | 机构/母子账户已确认 | 公开 L2 Depth (100ms) + bookTicker | 默认只读 (`LIVE_TRADING_ENABLED=false`) | Spot Testnet 可用 | 6,000 weight / min | `USDT`, `USDC`, `BTC`, `ETH`, `BNB` | **两边预置库存**（禁用交易中自动提币） | Gemini | 2026-10-26 |
| `binance_usdm` | `perpetual`, `dated_future` | **Sprint 6 启用 (Paper)** | 需校验单向/双向持仓与衍生品权限 | 公开 L2 + `markPriceUpdate` (1s) | 默认只读 (`LIVE_TRADING_ENABLED=false`) | Futures Testnet 可用 | 2,400 weight / min | `USDT`, `USDC` | 内部母子划转（非热路径） | Gemini | 2026-10-26 |
| `okx_spot` | `spot` | **Sprint 4 启用 (Paper)** | 第二家 CEX 正式选定（统一账户 Simple/Single-currency 校验） | 公开 `books5` / `books-l2-tbt` / `bbo-tbt` | 默认只读 (`LIVE_TRADING_ENABLED=false`) | OKX Demo Trading 可用 | 20 req / 2s (按接口细分) | `USDT`, `USDC`, `BTC`, `ETH` | **两边预置库存**（禁用交易中自动提币） | Gemini | 2026-10-26 |
| `okx_swap` | `perpetual`, `dated_future` | **Sprint 6 启用 (Paper)** | 需校验 `posMode` (`long_short_mode` / `net_mode`) 与 `ctVal` 乘数 | 公开 `books` + `funding-rate` + `mark-price` | 默认只读 (`LIVE_TRADING_ENABLED=false`) | OKX Demo Trading 可用 | 20 req / 2s | `USDT`, `USDC` | 内部资金划转（非热路径） | Gemini | 2026-10-26 |
| `equity_broker` | `equity`, `equity_token` | **RESEARCH_ONLY (硬性禁用执行)** | 待定（Sprint 7 专项审查） | 仅允许历史/只读研究数据接入 | **DISABLED** | 仅本地回放 | N/A | `USD` / `USDT` | 禁止跨市场自动划转 | Gemini | 2026-10-26 |
| `evm_dex` | `dex_pool` | **RESEARCH_ONLY (硬性禁用执行)** | 链/协议白名单待定（Sprint 8） | 仅限本地回放或只读 RPC 状态研究 | **DISABLED** | 本地 Fork 模拟 | 取决于 RPC 节点配额 | 链原生 Gas + 白名单 ERC20 | 禁止未审批合约交互 | Gemini | 2026-10-26 |
| `crypto_options` | `option` | **RESEARCH_ONLY (硬性禁用执行)** | 期权平台与模型待审批（Sprint 9） | 仅限只读合约元数据与曲面研究 | **DISABLED** | 仅本地回放 | N/A | `BTC`, `ETH`, `USDC` | 禁止执行 | Gemini | 2026-10-26 |
| `cross_chain_bridge` | `wrapped_asset` | **RESEARCH_ONLY (硬性禁用执行)** | 桥与在途账本待建（Sprint 10） | 仅限链上状态监控研究 | **DISABLED** | 禁止真实跨链调用 | N/A | 原生/封装资产 | **严禁在交易热路径调用跨链桥** | Gemini | 2026-10-26 |

## 3. 首批现货与衍生品核心约束

1. **同名 Symbol 严禁默认等价**：`binance_spot:BTCUSDT` 与 `okx_spot:BTC-USDT` 或 `okx_swap:BTC-USDT-SWAP` 必须通过 `InstrumentRegistry` 显式映射，并各自校验 `tick_size`、`step_size`、`min_notional` 与 `contract_multiplier`（例如 OKX 永续合约存在 `ctVal` 面值乘数，绝不可按 1:1 现货数量直接下单）。
2. **禁止交易时跨所充提**：跨 CEX 套利（`STRAT-003`）严格基于两边账户已预置且未被冻结/预留的自由余额（`free_balance`）计算可执行容量。
3. **官方 API 文档基线**：
   - Binance Spot API: `https://developers.binance.com/docs/binance-spot-api-docs`
   - Binance USDⓈ-M Futures API: `https://developers.binance.com/docs/derivatives/usds-margined-futures`
   - OKX V5 API: `https://www.okx.com/docs-v5/en/`
