# [CORE-002] 实现分环境配置、密钥注入和交易功能开关

- **当前状态**：`CLOSED (已验收)`
- **完成时间**：`2026-10-26`

## 背景与目标
实现 `Settings`、`VenueCredentials`、`SecretStr` 与 `FeatureFlags`，确保生产密钥绝不落盘或打印到日志/异常中，并防止 `paper`/`dev` 环境通过普通参数误开实盘。

## 负责人角色
- DRI：Gemini (总工程师兼实施人)
- Reviewer：Gemini (架构与风控总工审查)
- QA：Gemini (自动化单元与合同测试验收)

## 优先级与估算
- Priority：P0
- Estimate：3 SP
- Sprint：Sprint 1 (公共核心)
- Labels：`platform,security,p0`

## 依赖
- Blocked by：CORE-001, META-002
- Blocks：MD-001, RISK-001

## 接口定义
- 输入：环境变量 (`APP_ENV`, `LIVE_TRADING_ENABLED`, `BINANCE_API_*`, `OKX_API_*`) 与风险策略 YAML。
- 输出：`Settings` 实例、脱敏导出字典 `to_safe_dict()` 及确定性配置版本哈希 `compute_config_hash()`。
- 错误/拒绝语义：在非 `production` 环境开启 `live_trading_enabled=True` 或缺少 `live_approval_ticket_id` 时立即抛出 `ConfigurationError`。
- 版本/时间戳/单位：SHA-256 配置版本哈希（64位十六进制字符串），与账本 `config_versions` 强绑定。
- 与其他模块的边界：向所有 Adapter 提供受保护的凭证 (`SecretStr`)，向账本提供脱敏快照。

## 代码与文档交付物
- [x] 代码路径：`src/arb/config/settings.py`, `src/arb/config/__init__.py`
- [x] 配置/迁移：`.env.example`, `configs/risk-policy.example.yaml`
- [x] 文档/ADR：`docs/live-trading-gates.md`
- [x] 指标/日志/告警：`config_version_hash` 记录于每次运行与每个交易批次

## 测试用例
- [x] 正常路径
- [x] 边界条件
- [x] 异常/超时/断线
- [x] 重复/乱序/幂等（适用时）
- [x] 回放/参考实现（策略任务适用时）
- [x] 安全与权限（外部 API/资金操作适用时）
- **自动化测试证据**：`test_core002_live_trading_cannot_be_accidentally_enabled_in_paper_env`, `test_core002_secret_redaction_never_leaks_credentials` (PASSED)

## 验收条件
- 所有接口契约、类型校验、安全门禁与自动化测试用例 100% 通过。
- 严格遵循 `LIVE_TRADING_ENABLED=false` 默认硬门禁与 Fail-Closed 原则。

## 风险与回滚
- 已知风险与回滚方案：默认锁定 `LIVE_TRADING_ENABLED=false`，任何配置错误均 Fail-Closed。
- 是否影响 live trading（默认必须为否）：**否 (`LIVE_TRADING_ENABLED=false`)**

## PR 验收
- [x] CI 通过 (`Ran 9 tests in 0.138s - OK`)
- [x] 测试证据附在 PR
- [x] 日志/指标/错误处理完整
- [x] 文档与配置同步更新
- [x] 无绕过风控/账本/实盘门禁的路径
- [x] Reviewer 与 QA 批准 (Signed off by Gemini)
