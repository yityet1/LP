# Runbook: P0-UNKNOWN-ORDER-RECONCILIATION
- **告警级别**: P0 (资金/订单状态不确定)
- **负责人**: Gemini (Chief Engineer)
- **触发条件**: 下单或撤单请求发生超时/HTTP 5xx/网络中断，订单进入 `OrderStatus.UNKNOWN`。
- **自动响应**: 策略级熔断器立即阻断该策略一切新增开仓（`max_unknown_orders_per_strategy: 1`），严禁盲目重发订单。
- **排障与恢复步骤**:
  1. 调用 `TradingAdapter.query_order(instrument_id, client_order_id)` 与 `fetch_fills()` 查询场所真实状态。
  2. 将查询到的真实状态与成交写入 `LedgerStore.record_order_event()` 和 `record_fill()`（幂等去重）。
  3. 确认账本与交易所持仓/余额完全一致后，由风险负责人解锁策略级熔断。
