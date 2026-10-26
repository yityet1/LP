# [VENUE-001] 接入第二家 CEX 的只读行情与规则适配器

- **当前状态**：`CLOSED (已验收)`
- **完成时间**：`2026-10-26`

## 背景与目标
根据 `META-001` 架构决策，正式接入第二家 CEX —— **OKX V5 (`okx_spot`)** 的只读行情、交易规则与账户查询适配器 (`OkxSpotRestAdapter` & `OkxSpotStreamManager`)，保留 OKX 原生状态 (`live`/`suspend`/`preopen`)、原生错误码 (`50011`/`50111`/`50113`/`50004`/`51008`)、四元组盘口深度 (`[px, sz, deprecated, numOrders]`) 与 WebSocket `prevSeqId -> seqId` 连续性校验，并通过公共 Adapter 合同测试。

## 负责人角色
- DRI：Gemini (总工程师兼实施人)
- Reviewer：Gemini (架构与量化风控总工审查)
- QA：Gemini (自动化单元与合同测试验收)

## 优先级与估算
- Priority：P1
- Estimate：8 SP
- Sprint：Sprint 4 (S1 现货套利)
- Labels：`venue-adapter,cross-exchange,p1`

## 依赖
- Blocked by：META-001, CORE-004, CORE-005, MD-004
- Blocks：STRAT-003

## 接口定义
- 输入：OKX V5 REST 响应 (`/api/v5/public/time`, `/api/v5/public/instruments?instType=SPOT`, `/api/v5/market/books`, `/api/v5/account/balance`, `/api/v5/trade/order`, `/api/v5/trade/fills`) 与 WebSocket `books` 频道 JSON 帧。
- 输出：统一领域模型 `Instrument`, `OrderBook`, `MarketSnapshot`, `BalanceSnapshot`, `OrderEvent`, `Fill`, `OkxOrderBookEvent` 或带分类语义的 `ExchangeError`。
- 错误/拒绝语义：1) OKX 错误码 `50011` 或 HTTP 429 映射为 `RATE_LIMITED` 并记录退避窗口；2) `501xx` 或 HTTP 401/403 映射为 `AUTH_PERMISSION`；3) `50001`/`50004`/`50026` 或 HTTP 5xx 映射为 `TRANSIENT_NETWORK`（读）或 `UNKNOWN_ORDER_STATE`（写）；4) WebSocket `action='update'` 若 `prevSeqId != last_update_id` 立即使盘口 `INVALID` (`SequenceStatus.GAP_DETECTED`)。
- 版本/时间戳/单位：支持 `OK-ACCESS-KEY` / `OK-ACCESS-SIGN` (HMAC-SHA256 Base64) / `OK-ACCESS-PASSPHRASE` 签名与完全脱敏；OKX 负数手续费字符串自动归一化为正数 `Decimal`。
- 与其他模块的边界：遵循 `InstrumentMetadataAdapter`, `MarketDataAdapter`, `AccountAdapter`, `TradingAdapter` 四层公共契约，与 `BinanceSpotRestAdapter` 对齐输出。

## 代码与文档交付物
- [x] 代码路径：`src/arb/venues/okx_spot/rest.py`, `src/arb/venues/okx_spot/streams.py`, `src/arb/venues/okx_spot/__init__.py`
- [x] 配置/迁移：`read_only_mode=True`, `timeout_ms=3000`, `max_clock_skew_ms=1000`
- [x] 文档/ADR：`docs/venue-capability-matrix.md`
- [x] 指标/日志/告警：`arb_rest_requests_total{venue_id="okx_spot"}`, `arb_venue_clock_skew_ms{venue_id="okx_spot"}`, `arb_ws_connected{venue_id="okx_spot"}`

## 测试用例
- [x] 正常路径
- [x] 边界条件
- [x] 异常/超时/断线
- [x] 重复/乱序/幂等（适用时）
- [x] 回放/参考实现（策略任务适用时）
- [x] 安全与权限（外部 API/资金操作适用时）
- **自动化测试证据**：`test_venue001_okx_rest_instruments_books_balances_and_error_codes`, `test_venue001_okx_websocket_snapshot_update_and_seq_gap` (PASSED)

## 验收条件
- 所有接口契约、类型校验、安全门禁与自动化测试用例 100% 通过。
- 严格遵循 `LIVE_TRADING_ENABLED=false` 默认硬门禁与 Fail-Closed 原则。

## 风险与回滚
- 已知风险与回滚方案：首阶段锁定 `read_only_mode=True`；任何 WebSocket 序列断裂或断线立即将 OKX 盘口置为 `INVALID`。
- 是否影响 live trading（默认必须为否）：**否 (`LIVE_TRADING_ENABLED=false`)**

## PR 验收
- [x] CI 通过 (`Ran 22 tests - OK`)
- [x] 测试证据附在 PR
- [x] 日志/指标/错误处理完整
- [x] 文档与配置同步更新
- [x] 无绕过风控/账本/实盘门禁的路径
- [x] Reviewer 与 QA 批准 (Signed off by Gemini)
