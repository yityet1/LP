# [DERIV-004] 完成 S2 衍生品套利专项验收与 Paper 稳定性门禁

## 1. 元信息（Metadata）

- **Issue ID**：`DERIV-004`
- **标题**：完成 S2 衍生品套利专项验收与 Paper 稳定性门禁
- **工作流（Workstream）**：`DERIV / QA`
- **负责人（DRI）**：Gemini（总工程师）
- **审阅人（Reviewer）**：Gemini（架构与风控负责人）
- **优先级（Priority）**：`P0`
- **风险等级（Risk Level）**：`High`
- **所属 Sprint**：`Sprint 6 — S2 衍生品基差与资金费率套利`
- **关联策略 / 场景**：`S2-A`、`S2-B`（Milestone M4 S1+S2 Paper 稳定性门禁）
- **关联文档章节**：
  - 《总体架构与研发路线图》：`Milestone M4`、`Sprint 6`
  - 《策略专项验收矩阵与发布门禁》：`4.1 S2-001 ~ S2-012`
- **状态（Status）**：`CLOSED (Verified)`

## 2. 背景与目标（Context & Objective）

- **背景**：在 S2 策略进入模拟盘持续运行前，必须通过 `S2-001` ~ `S2-012` 全部专项验收用例，并在 `PaperExecutionSimulator` 中跑通“现货买入 + 永续做空”建仓与对账全链路。
- **目标**：完成 `tests/unit/test_sprint6_derivatives.py` 自动化验收套件，实现 S1+S2 全仓 30 项自动化测试 100% 绿灯通过。

## 3. 范围与交付物（Scope & Deliverables）

- **交付物清单**：
  - [x] 自动化测试：`tests/unit/test_sprint6_derivatives.py`（合入全仓主回归套件 `tests/unit/test_sprint0_and_core.py`）
  - [x] 验收矩阵覆盖：`S2-001`（Mark/Index 与可执行价隔离）、`S2-002`/`S2-003`（`ctVal` 合约乘数与 Delta 对齐）、`S2-004`/`S2-005`（4 腿往返成本与到期平仓门禁）、`S2-006`/`S2-007`/`S2-008`（费率周期折算、结算时间对齐与盈亏平衡核算）、`S2-009`/`S2-010`/`S2-011`（强平距离监控、双腿成对减仓与费率反转退出）

## 4. 实测验证结果（Verification Evidence）

- **测试命令**：`PYTHONPATH=src python3 -m unittest discover -s tests/unit -p "test_*.py" -v`
- **实测结论**：全仓 30 项单元、契约、回放、故障注入与 S2 衍生品专项测试 100% 通过（`Ran 30 tests — OK`）。
