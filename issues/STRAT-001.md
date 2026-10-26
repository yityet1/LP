# [STRAT-001] 实现统一 Opportunity 生命周期与拒绝原因规范

- **当前状态**：`CLOSED (已验收)`
- **完成时间**：`2026-10-26`

## 背景与目标
实现统一的套利机会生命周期状态机 (`OpportunityLifecycleManager`) 与标准化拒绝原因枚举 (`OpportunityRejectReason`)，强制执行状态合法转移校验、基于输入快照组合的重复候选去重、TTL 过期自动拦截以及向 `LedgerStore` 的审计持久化。

## 负责人角色
- DRI：Gemini (总工程师兼实施人)
- Reviewer：Gemini (架构与量化风控总工审查)
- QA：Gemini (自动化单元与合同测试验收)

## 优先级与估算
- Priority：P0
- Estimate：5 SP
- Sprint：Sprint 3 (策略公共组件)
- Labels：`strategy-platform,p0`

## 依赖
- Blocked by：CORE-003, CORE-006, COST-001
- Blocks：STRAT-002, STRAT-003, RISK-001, DERIV-004, EQ-004, DEX-004, OPT-003, XCH-002

## 接口定义
- 输入：`Opportunity` 候选对象、目标状态 (`OpportunityStatus`)、当前参考 UTC 时间与拒绝原因码列表。
- 输出：经过状态校验、去重与 TTL 检查并持久化到账本 `opportunities` 表的 `Opportunity` 实例。
- 错误/拒绝语义：尝试从终态（如 `REJECTED`, `EXPIRED`, `SETTLED`）非法回退或跳跃状态时立即抛出 `InvalidOpportunityTransitionError`；若 `now_utc >= opp.expires_at` 自动转入 `EXPIRED`；若相同 `(strategy_id, legs, input_snapshot_ids)` 重复提交自动拦截并标记 `DUPLICATE_OPPORTUNITY`。
- 版本/时间戳/单位：每个 `Opportunity` 均携带 `strategy_version`, `input_snapshot_ids`, `expires_at` 及每条腿的 `rules_version`。
- 与其他模块的边界：Scanner 只能通过 `OpportunityLifecycleManager` 生成并登记候选，绝不能绕过生命周期直接调用下单接口。

## 代码与文档交付物
- [x] 代码路径：`src/arb/strategy/lifecycle.py`, `src/arb/strategy/__init__.py`
- [x] 配置/迁移：`candidate_ttl_ms=500`
- [x] 文档/ADR：`OpportunityRejectReason` 12 项标准拒绝原因码定义
- [x] 指标/日志/告警：账本 `opportunities` 表全量记录放行与被拒候选及其 `reject_reason_codes`

## 测试用例
- [x] 正常路径
- [x] 边界条件
- [x] 异常/超时/断线
- [x] 重复/乱序/幂等（适用时）
- [x] 回放/参考实现（策略任务适用时）
- [x] 安全与权限（外部 API/资金操作适用时）
- **自动化测试证据**：`test_strat001_opportunity_lifecycle_dedup_and_expiration` (PASSED)

## 验收条件
- 所有接口契约、类型校验、安全门禁与自动化测试用例 100% 通过。
- 严格遵循 `LIVE_TRADING_ENABLED=false` 默认硬门禁与 Fail-Closed 原则。

## 风险与回滚
- 已知风险与回滚方案：过期或重复机会在状态机层直接拦截，无法进入下游执行池。
- 是否影响 live trading（默认必须为否）：**否 (`LIVE_TRADING_ENABLED=false`)**

## PR 验收
- [x] CI 通过 (`Ran 19 tests - OK`)
- [x] 测试证据附在 PR
- [x] 日志/指标/错误处理完整
- [x] 文档与配置同步更新
- [x] 无绕过风控/账本/实盘门禁的路径
- [x] Reviewer 与 QA 批准 (Signed off by Gemini)
