# [PORT-001] 实现组合级资本分配、敞口聚合与策略隔离

## 1. 元数据（Metadata）
- **Issue ID**：`PORT-001`
- **标题**：实现组合级资本分配、敞口聚合与策略隔离
- **类型（Type）**：`Feature`
- **状态（Status）**：`DONE (Verified)`
- **优先级（Priority）**：`P0`
- **所属 Sprint / 里程碑**：`Sprint 11` / `M7 组合上线与发布门禁闭环`
- **关联路线图阶段**：`Phase 6`
- **关联策略家族**：`ALL (S1, S2, S3, S4)`
- **关联架构模块 / 目录**：`src/arb/portfolio/allocator.py`
- **负责人（DRI）**：`Gemini (Chief Engineer)`
- **审阅人（Reviewer）**：`Gemini (Chief Engineer)`
- **预估工作量（Story Points）**：`13`
- **依赖 Issue（Blocked By）**：`RISK-001`, `EXEC-002`, `STRAT-003`, `DERIV-004`, `EQ-004`
- **阻塞 Issue（Blocks）**：`REL-001`

---

## 2. 背景与目标（Context & Objective）
- **本期目标**：实现 `PortfolioCapitalAllocator`、`PortfolioExposureSummary`、`CapitalAllocationDecision` 与 `StrategyCostAttributionRecord`（`src/arb/portfolio/allocator.py`），统一管护跨策略/跨场所/跨资产的自由余额分配（严禁同一笔可用余额被多策略重复分配 `PORT-001`），排除锁定、预占、保证金与跨链在途资金（`PORT-002`），落实策略/场所/资产集中度上限（`PORT-003`）、单策略故障隔离及分项成本收益归因（`PORT-004`）。

## 3. 验收标准（Acceptance Criteria）
- [x] AC-1：同一余额绝不被重复分配；在途/锁定/预占/保证金不计入自由可分配余额。
- [x] AC-2：支持按策略、场所、资产维度集中度校验与单策略故障隔离，支持分项成本（手续费、资金费率/借券、Gas/桥费、滑点）归因。

## 4. 测试与验证计划（Testing & Verification Plan）
- **单元测试（Unit Tests）**：`tests/unit/test_sprint9_to_11_final.py::TestSprint9To11OptionsCrossChainPortfolioAndRelease::test_sprint11_port001_and_rel001_capital_allocator_and_release_gates` 已执行并通过。
