# [EXEC-004] P0 故障注入测试套件与应急演练手册

## 1. 元信息（Metadata）

- **Issue ID**：`EXEC-004`
- **标题**：P0 故障注入测试套件与应急演练手册
- **工作流（Workstream）**：`EXEC / QA`
- **负责人（DRI）**：Gemini（总工程师）
- **审阅人（Reviewer）**：Gemini（架构与风控负责人）
- **优先级（Priority）**：`P0`
- **风险等级（Risk Level）**：`Critical`
- **所属 Sprint**：`Sprint 5 — 风控、订单状态机、对账、Paper Engine 与故障演练`
- **关联策略 / 场景**：`ALL`（Milestone M3 平台级 P0 门禁）
- **关联文档章节**：
  - 《总体架构与研发路线图》：`7.3 故障注入与混沌测试`、`Milestone M3`、`Sprint 5`
  - 《策略专项验收矩阵与发布门禁》：`2.1 CORE-004 ~ CORE-010`
- **状态（Status）**：`CLOSED (Verified)`

## 2. 背景与目标（Context & Objective）

- **背景**：平台级 P0 门禁要求所有核心异常路径（下单超时重启、双 Worker 并发抢占、撤单后晚到成交、账实不符熔断）不仅要有操作手册（Runbook），还必须具备每次 CI 可自动执行的故障注入套件。
- **目标**：实现 `FaultInjectionHarness`（`src/arb/testing/fault_injection.py`）与应急响应手册 `docs/runbooks/p0-execution-incident-response.md`，达成 **Milestone M3（模拟盘与对账闭环）**。

## 3. 范围与交付物（Scope & Deliverables）

- **交付物清单**：
  - [x] 代码/模块：`src/arb/testing/fault_injection.py`（`FaultInjectionHarness`、`FaultScenarioResult`）
  - [x] 运维手册：`docs/runbooks/p0-execution-incident-response.md`
  - [x] 自动化测试：`tests/unit/test_sprint5_execution_and_risk.py::TestSprint5ExecutionAndRisk::test_exec_004_p0_fault_injection_drills_all_pass`

## 4. 强约束与安全不变量（Hard Constraints & Invariants）

- [x] **4 项 P0 混沌演练 100% 自动化断言**：
  1. `FAULT_01_ORDER_TIMEOUT_AND_CRASH_RECOVERY`：REST 超时 -> `UNKNOWN` -> 重启防重发守卫 -> 对账找回成交并收敛。
  2. `FAULT_02_DUAL_WORKER_RACE_GUARD`：双 Worker 抢占同一 `cycle_id` 被 `cycle_worker_leases` 阻断。
  3. `FAULT_03_LATE_FILL_AFTER_CANCEL`：撤单 `CANCELED` 后收到晚到成交，累计成交量单调递增更新为 `FILLED`。
  4. `FAULT_04_BALANCE_MISMATCH_AND_PERSISTENT_KILL_SWITCH`：外盘余额漂移触发 `VENUE` + `STRATEGY` 级持久化熔断。

## 5. 验收标准（Acceptance Criteria / Gherkin）

- [x] **场景 1（全量 P0 演练一键通过）**：调用 `FaultInjectionHarness.run_all_p0_drills(settings)` 返回 4 项演练结果且 `passed == True`。

## 6. 实测验证结果（Verification Evidence）

- **测试命令**：`PYTHONPATH=src python3 -m unittest discover -s tests/unit -p "test_*.py" -v`
- **实测结论**：`test_exec_004_p0_fault_injection_drills_all_pass ... ok`
