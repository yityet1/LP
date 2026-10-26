# [EXEC-001] CEX 订单状态机、事务性 Outbox 与未知状态保护

## 1. 元信息（Metadata）

- **Issue ID**：`EXEC-001`
- **标题**：CEX 订单状态机、事务性 Outbox 与未知状态保护
- **工作流（Workstream）**：`EXEC`
- **负责人（DRI）**：Gemini（总工程师）
- **审阅人（Reviewer）**：Gemini（架构与风控负责人）
- **优先级（Priority）**：`P0`
- **风险等级（Risk Level）**：`Critical`
- **所属 Sprint**：`Sprint 5 — 风控、订单状态机、对账、Paper Engine 与故障演练`
- **关联策略 / 场景**：`ALL`（所有 CEX 现货与衍生品订单执行）
- **关联文档章节**：
  - 《总体架构与研发路线图》：`3.2 (G) Execution Orchestrator`、`5.2 CEX 订单状态机`、`Sprint 5`
  - 《策略专项验收矩阵与发布门禁》：`2.1 CORE-004 ~ CORE-007`
- **状态（Status）**：`CLOSED (Verified)`

## 2. 背景与目标（Context & Objective）

- **背景**：网络超时、进程崩溃、撤单后晚到成交、多 Worker 并发抢占是量化交易系统中最致命的资金风险源。
- **目标**：实现 `OrderStateMachine` 与 `ExecutionOrchestrator`（`src/arb/execution/fsm.py`），通过 `order_outbox` 先落库后发单（`CORE-006`）、`cycle_worker_leases` 排他锁（`CORE-007`）、超时进入 `UNKNOWN` 严禁盲目重发（`CORE-004`）以及撤单后晚到成交单调递增累计（`CORE-005`），保障订单生命周期绝对一致。

## 3. 范围与交付物（Scope & Deliverables）

- **交付物清单**：
  - [x] 代码/模块：`src/arb/execution/fsm.py`（`OrderStateMachine`、`ExecutionOrchestrator`、`OutboxOrderRecord`、`InvalidOrderStateTransitionError`、`ConcurrentWorkerConflictError`）
  - [x] 数据库/表结构：`src/arb/ledger/migrations/0001_initial_ledger_schema.sql`（新增 `order_outbox`、`cycle_worker_leases` 表）
  - [x] 自动化测试：`tests/unit/test_sprint5_execution_and_risk.py::TestSprint5ExecutionAndRisk::test_exec_001_order_fsm_outbox_lease_and_late_fill`

## 4. 强约束与安全不变量（Hard Constraints & Invariants）

- [x] **Outbox 先落库**：在向交易所发送任何网络请求前，必须在同一 ACID 事务内写入 `cycles`、`risk_decisions`、`orders (PREPARED)` 与 `order_outbox`。
- [x] **UNKNOWN 零盲发**：下单超时或网络中断进入 `OrderStatus.UNKNOWN`，自动触发策略级 Kill Switch，且重启或重试调用 `dispatch_leg()` 时检测到状态非 `PREPARED` 立即返回 `SKIPPED_DUPLICATE_SUBMISSION_GUARD`。
- [x] **晚到成交保护**：允许 `CANCELED -> PARTIALLY_FILLED / FILLED` 状态迁移，`cumulative_filled_qty` 严格单调不减。

## 5. 验收标准（Acceptance Criteria / Gherkin）

- [x] **场景 1（非法状态迁移阻断）**：When 尝试直接从 `PREPARED` 跳转到 `FILLED`，Then 抛出 `InvalidOrderStateTransitionError`。
- [x] **场景 2（单周期排他租约 `CORE-007`）**：Given `worker_A` 已持有 `cyc_exec_1` 租约，When `worker_B` 尝试抢占该周期，Then 抛出 `ConcurrentWorkerConflictError`。
- [x] **场景 3（超时 UNKNOWN 与防重发 + 撤单后晚到成交 `CORE-004` / `CORE-005`）**：Given 下单超时进入 `UNKNOWN`，When 再次调度同一腿，Then 跳过网络发送；When 订单标记 `CANCELED` 后收到晚到 `Fill`，Then 累计成交量正确更新且订单状态转为 `FILLED`。

## 6. 实测验证结果（Verification Evidence）

- **测试命令**：`PYTHONPATH=src python3 -m unittest discover -s tests/unit -p "test_*.py" -v`
- **实测结论**：`test_exec_001_order_fsm_outbox_lease_and_late_fill ... ok`
