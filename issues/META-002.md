# [META-002] 定义初始风险政策、交易模式和实盘审批门禁

- **当前状态**：`CLOSED (已验收)`
- **完成时间**：`2026-10-26`

## 背景与目标
定义版本化的全局与策略级风险限额（单笔 1,000 USDT、单批次 2,000 USDT、盘口最大年龄 500ms、最低净收益 5 bps），确立 `LIVE_TRADING_ENABLED=false` 硬默认及 Strategy/Venue/Portfolio 三级熔断门禁。

## 负责人角色
- DRI：Gemini (总工程师兼实施人)
- Reviewer：Gemini (架构与风控总工审查)
- QA：Gemini (自动化单元与合同测试验收)

## 优先级与估算
- Priority：P0
- Estimate：3 SP
- Sprint：Sprint 0 (项目治理)
- Labels：`planning,risk,p0`

## 依赖
- Blocked by：META-001
- Blocks：CORE-002, RISK-001

## 接口定义
- 输入：YAML 风险政策文件 (`configs/risk-policy.example.yaml`) 与环境变量。
- 输出：`RiskPolicyConfig` 强类型配置对象与 `docs/live-trading-gates.md` 实盘门禁规范。
- 错误/拒绝语义：缺失必填风险限额、限额非正数、或 YAML 默认开启 `live_trading_enabled=true` 时直接抛出 `ConfigurationError` 阻断启动。
- 版本/时间戳/单位：`policy_version: 1.0.0`；金额单位 `USDT` (`Decimal`)；时间单位 `ms` (`int`)；收益率单位 `bps` (`Decimal`)。
- 与其他模块的边界：由 `arb.config` 加载并注入 `LedgerStore` (`config_versions` 表) 与 `RiskGateway`。

## 代码与文档交付物
- [x] 代码路径：`src/arb/config/settings.py` (`RiskPolicyConfig`)
- [x] 配置/迁移：`configs/risk-policy.example.yaml`
- [x] 文档/ADR：`docs/live-trading-gates.md`
- [x] 指标/日志/告警：`LedgerStore.set_kill_switch()` 持久化熔断状态

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
- 已知风险与回滚方案：任何配置异常直接 Fail-Closed 拒绝启动；支持持久化全局 Kill Switch 一键熔断。
- 是否影响 live trading（默认必须为否）：**否 (`LIVE_TRADING_ENABLED=false`)**

## PR 验收
- [x] CI 通过 (`Ran 9 tests in 0.138s - OK`)
- [x] 测试证据附在 PR
- [x] 日志/指标/错误处理完整
- [x] 文档与配置同步更新
- [x] 无绕过风控/账本/实盘门禁的路径
- [x] Reviewer 与 QA 批准 (Signed off by Gemini)
