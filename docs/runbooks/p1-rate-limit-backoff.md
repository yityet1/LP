# Runbook: P1-RATE-LIMIT-BACKOFF
- **告警级别**: P1
- **负责人**: Gemini (Chief Engineer)
- **触发条件**: 单次 HTTP 429 限频告警。
- **自动响应**: 按 `retry_after_ms` 执行指数退避，降低只读轮询频率。
