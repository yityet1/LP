# Runbook: P0-LEDGER-RECONCILIATION-MISMATCH
- **告警级别**: P0 (账本与外部场所资产不一致)
- **负责人**: Gemini (Chief Engineer)
- **触发条件**: 定时或启动对账发现本地 `orders`/`fills`/`balance_snapshots` 与交易所快照存在未解释差异。
- **自动响应**: Venue 级熔断开启，暂停涉及该交易所的所有策略。
- **排障与恢复步骤**:
  1. 检查 `order_events_audit` 与 `fills` 表是否遗漏 WebSocket user stream 推送。
  2. 执行 REST 全量历史成交回补，核实手续费币种（如 BNB/USDT）抵扣差异。
  3. 对账平账后人工签署解除熔断。
