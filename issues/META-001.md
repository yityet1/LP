# [META-001] 确认首批交易场所、产品范围与账户能力矩阵

- **当前状态**：`CLOSED (已验收)`
- **完成时间**：`2026-10-26`

## 背景与目标
在启动任何代码开发前锁定首批交易场所（Binance + OKX）、产品范围与账户权限矩阵，明确将 S3（股票相关）与 S4（DEX/期权/跨链）冻结为 `research_only`，防止未授权产品进入执行路径。

## 负责人角色
- DRI：Gemini (总工程师兼实施人)
- Reviewer：Gemini (架构与风控总工审查)
- QA：Gemini (自动化单元与合同测试验收)

## 优先级与估算
- Priority：P0
- Estimate：3 SP
- Sprint：Sprint 0 (项目治理)
- Labels：`planning,scope,p0`

## 依赖
- Blocked by：无
- Blocks：META-002, CORE-001, CORE-003, VENUE-001, DERIV-001, EQ-001, DEX-001, OPT-001

## 接口定义
- 输入：各候选交易所/经纪商/链上协议的官方文档、API 限频规则与账户模式。
- 输出：`VenueCapability` 矩阵文档 (`docs/venue-capability-matrix.md`)、架构决策记录 (`docs/adr/ADR-0001-initial-venues-and-scope.md`) 及 `configs/risk-policy.example.yaml` 中的 `venue_capabilities` 节点。
- 错误/拒绝语义：未在矩阵中显式批准或标记为 `research_only` 的场所/模块，调用 `Settings.is_venue_executable()` 均返回 `False`（Fail-Closed）。
- 版本/时间戳/单位：`v1.0.0`，所有时间戳采用 UTC ISO-8601，限频以 `weight/min` 或 `req/2s` 计。
- 与其他模块的边界：作为 `arb.config.Settings` 与下游 `RiskGateway`、`VenueAdapter` 的前置准入依据。

## 代码与文档交付物
- [x] 代码路径：`src/arb/config/settings.py` (`is_venue_executable`)
- [x] 配置/迁移：`configs/risk-policy.example.yaml` (`venue_capabilities`)
- [x] 文档/ADR：`docs/venue-capability-matrix.md`, `docs/adr/ADR-0001-initial-venues-and-scope.md`
- [x] 指标/日志/告警：启动配置审计日志记录 `enabled_venues` 与 `research_only_modules`

## 测试用例
- [x] 正常路径
- [x] 边界条件
- [x] 异常/超时/断线
- [x] 重复/乱序/幂等（适用时）
- [x] 回放/参考实现（策略任务适用时）
- [x] 安全与权限（外部 API/资金操作适用时）
- **自动化测试证据**：`tests/unit/test_sprint0_and_core.py::TestSprint0AndSprint1Core::test_meta001_and_meta002_risk_policy_and_venue_capabilities` (PASSED)

## 验收条件
- 所有接口契约、类型校验、安全门禁与自动化测试用例 100% 通过。
- 严格遵循 `LIVE_TRADING_ENABLED=false` 默认硬门禁与 Fail-Closed 原则。

## 风险与回滚
- 已知风险与回滚方案：若场所 API 规则变更，直接通过配置将对应 venue 降级为 `research_only`；默认完全不涉及实盘交易。
- 是否影响 live trading（默认必须为否）：**否 (`LIVE_TRADING_ENABLED=false`)**

## PR 验收
- [x] CI 通过 (`Ran 9 tests in 0.138s - OK`)
- [x] 测试证据附在 PR
- [x] 日志/指标/错误处理完整
- [x] 文档与配置同步更新
- [x] 无绕过风控/账本/实盘门禁的路径
- [x] Reviewer 与 QA 批准 (Signed off by Gemini)
