# Runbook: P0-VENUE-IP-BAN-OR-418
- **告警级别**: P0
- **负责人**: Gemini (Chief Engineer)
- **触发条件**: 收到 HTTP 418 或连续 HTTP 429 触发限频封禁。
- **自动响应**: 立即停止向该 Venue 发送任何非对账类 REST 请求，标记盘口 `INVALID`。
