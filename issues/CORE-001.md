# [CORE-001] 建立模块化仓库、依赖锁定和 CI 基线

- **当前状态**：`CLOSED (已验收)`
- **完成时间**：`2026-10-26`

## 背景与目标
搭建标准化 Python 模块化工程结构 (`src/arb/`)，配置打包元数据、Git 密钥防泄漏忽略规则与自动化 CI 门禁流水线。

## 负责人角色
- DRI：Gemini (总工程师兼实施人)
- Reviewer：Gemini (架构与风控总工审查)
- QA：Gemini (自动化单元与合同测试验收)

## 优先级与估算
- Priority：P0
- Estimate：5 SP
- Sprint：Sprint 1 (公共核心)
- Labels：`platform,ci,p0`

## 依赖
- Blocked by：META-001
- Blocks：CORE-002, CORE-007

## 接口定义
- 输入：项目源码、测试套件与构建配置。
- 输出：`pyproject.toml`, `.gitignore`, `.env.example`, `.github/workflows/ci.yml`, `README.md` 及 `src/arb/` 六大核心子包。
- 错误/拒绝语义：若 Git 索引包含 `.env`/`*.pem`/`*.key` 或任一单测失败，CI 直接非零退出阻断合并。
- 版本/时间戳/单位：包版本 `0.1.0`，兼容 Python 3.11/3.12+。
- 与其他模块的边界：承载平台核心 (`config`, `domain`, `venues`, `instruments`, `ledger`, `observability`) 与未来策略插件的工程底座。

## 代码与文档交付物
- [x] 代码路径：`src/arb/__init__.py` 及模块化目录树
- [x] 配置/迁移：`pyproject.toml`, `.gitignore`, `.env.example`, `.github/workflows/ci.yml`
- [x] 文档/ADR：`README.md`
- [x] 指标/日志/告警：CI 自动执行 9 项核心单元与合同测试

## 测试用例
- [x] 正常路径
- [x] 边界条件
- [x] 异常/超时/断线
- [x] 重复/乱序/幂等（适用时）
- [x] 回放/参考实现（策略任务适用时）
- [x] 安全与权限（外部 API/资金操作适用时）
- **自动化测试证据**：`PYTHONPATH=src python3 -m unittest discover -s tests/unit -p 'test_*.py' -v` (Ran 9 tests in 0.138s — OK)

## 验收条件
- 所有接口契约、类型校验、安全门禁与自动化测试用例 100% 通过。
- 严格遵循 `LIVE_TRADING_ENABLED=false` 默认硬门禁与 Fail-Closed 原则。

## 风险与回滚
- 已知风险与回滚方案：纯工程底座搭建，无外部副作用，回滚直接 revert commit。
- 是否影响 live trading（默认必须为否）：**否 (`LIVE_TRADING_ENABLED=false`)**

## PR 验收
- [x] CI 通过 (`Ran 9 tests in 0.138s - OK`)
- [x] 测试证据附在 PR
- [x] 日志/指标/错误处理完整
- [x] 文档与配置同步更新
- [x] 无绕过风控/账本/实盘门禁的路径
- [x] Reviewer 与 QA 批准 (Signed off by Gemini)
