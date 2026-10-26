# Runbook: P1-MARKET-DATA-DISCONNECT
- **告警级别**: P1
- **负责人**: Gemini (Chief Engineer)
- **触发条件**: WebSocket 断流或序列号缺口（`SEQUENCE_GAP`）。
- **自动响应**: 立即将对应 `OrderBook.is_valid` 设为 `False`，重新拉取 REST 快照并按官方 updateId 对齐后方可恢复。
