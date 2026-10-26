# Runbook: P0 执行事故应急响应与故障恢复手册 (`EXEC-004`)

- **文档版本**: `v1.0.0`
- **关联工单**: `EXEC-004`
- **负责人**: Gemini (Chief Engineer & SRE/Risk Lead)

## 1. 事故分类与自动止损行为

| 故障场景 | 触发条件 | 系统自动止损动作 (Fail-Closed) | 对账与恢复步骤 | 解除熔断审批人 |
|---|---|---|---|---|
| **下单超时 / 5xx (`UNKNOWN`)** | `TradingAdapter.submit_order` 超时或连接重置 | 订单置为 `UNKNOWN`；立即触发 `STRATEGY` 级熔断，阻断该策略新开仓；Outbox 禁止重发同 `client_order_id` | 1. 运行 `ReconciliationEngine.reconcile_order_and_fills()` 查询真实状态；2. 回补漏推的成交 (`fills`)；3. 校验本地累计成交量与远端一致 | Gemini (Execution Lead) |
| **进程在下单前后崩溃** | 进程被信号终止或宿主机重启 | 重启后加载 `order_outbox` 与 `kill_switch_state`；已处于 `DISPATCHED` / `SUBMITTING` 的订单标记为 `SKIPPED_DUPLICATE_SUBMISSION_GUARD` | 启动对账协程遍历所有未终结批次 (`CycleState != SETTLED`)，完成外部查询对齐后才允许恢复扫描 | Gemini (SRE Lead) |
| **跨所双腿单边成交 (`S1-004`)** | 买腿 `FILLED` 但卖腿 `REJECTED` / `EXPIRED` | 计算真实未对冲敞口 (`unhedged_base_exposure`)，批次进入 `CycleState.RECOVERY`（绝不伪造原子回滚） | 若敞口超过 `max_unhedged_exposure_notional` 则触发熔断并执行 `reduce_only` 平仓或人工接管 | Gemini (Risk Lead) |
| **账本与交易所余额不一致 (`CORE-010`)** | `reconcile_balances` 差额超阈值 | 立即触发 `VENUE` 级与 `STRATEGY` 级双重持久化熔断，写入 `reconciliation_discrepancies` 人工接管队列 | 核查是否存在外部手动划转、未入账手续费抵扣或漏单，平账后人工签署重置 `kill_switch_state` | Gemini (Risk Lead) |
