# [CORE-007] 建立结构化日志、指标、trace 和告警基线

- **当前状态**：`CLOSED (已验收)`
- **完成时间**：`2026-10-26`

## 背景与目标
建立结构化 JSON 日志器 (`StructuredLogger`)、Prometheus 兼容指标注册表 (`MetricsRegistry`) 与分级告警管理器 (`AlertManager`)，确保日志强制携带 5 大关联字段且自动脱敏密钥，所有 P0/P1 告警绑定负责人与 Runbook。

## 负责人角色
- DRI：Gemini (总工程师兼实施人)
- Reviewer：Gemini (架构与风控总工审查)
- QA：Gemini (自动化单元与合同测试验收)

## 优先级与估算
- Priority：P1
- Estimate：5 SP
- Sprint：Sprint 1 (公共核心)
- Labels：`observability,p1`

## 依赖
- Blocked by：CORE-001, CORE-003
- Blocks：EXEC-004, DEX-001, REL-001

## 接口定义
- 输入：`TraceContext` (`strategy_id`, `venue_id`, `cycle_id`, `client_order_id`, `correlation_id`)、业务日志事件与告警触发规则。
- 输出：脱敏 JSON 日志流、Prometheus 文本格式指标输出、带 Runbook 链接的 `AlertEvent` 及 `docs/runbooks/` 下的 5 份标准应急预案。
- 错误/拒绝语义：任何包含 `api_secret`/`passphrase`/`signature` 的日志字段或消息体均被强制替换为 `***REDACTED***`。
- 版本/时间戳/单位：OpenTelemetry 兼容 `trace_id`/`span_id` 字段，UTC ISO-8601 时间戳。
- 与其他模块的边界：支撑全平台行情监控、订单状态追踪与故障注入演练（`EXEC-004`）。

## 代码与文档交付物
- [x] 代码路径：`src/arb/observability/telemetry.py`, `src/arb/observability/__init__.py`
- [x] 配置/迁移：`LOG_LEVEL`
- [x] 文档/ADR：`docs/runbooks/p0-unknown-order-reconciliation.md` 等 5 份 Runbook
- [x] 指标/日志/告警：`arb_alerts_total{rule,severity,venue_id}` 等 Prometheus 计数器与 Gauge

## 测试用例
- [x] 正常路径
- [x] 边界条件
- [x] 异常/超时/断线
- [x] 重复/乱序/幂等（适用时）
- [x] 回放/参考实现（策略任务适用时）
- [x] 安全与权限（外部 API/资金操作适用时）
- **自动化测试证据**：`test_core007_observability_logging_metrics_and_alerts` (PASSED)

## 验收条件
- 所有接口契约、类型校验、安全门禁与自动化测试用例 100% 通过。
- 严格遵循 `LIVE_TRADING_ENABLED=false` 默认硬门禁与 Fail-Closed 原则。

## 风险与回滚
- 已知风险与回滚方案：观测组件无副作用，告警规则支持动态扩展。
- 是否影响 live trading（默认必须为否）：**否 (`LIVE_TRADING_ENABLED=false`)**

## PR 验收
- [x] CI 通过 (`Ran 9 tests in 0.138s - OK`)
- [x] 测试证据附在 PR
- [x] 日志/指标/错误处理完整
- [x] 文档与配置同步更新
- [x] 无绕过风控/账本/实盘门禁的路径
- [x] Reviewer 与 QA 批准 (Signed off by Gemini)
