# [DATA-001] 建立行情记录、Parquet 分区与确定性回放工具

- **当前状态**：`CLOSED (已验收)`
- **完成时间**：`2026-10-26`

## 背景与目标
建立按 `venue=<venue_id>/date=<YYYY-MM-DD>/symbol=<instrument_id>` 分区存储的行情记录器 (`MarketDataRecorder`) 与固定时钟确定性回放引擎 (`DeterministicReplayEngine`)，确保每个套利候选可追溯到精确的数据集版本指纹 (`dataset_version_id`) 与配置版本哈希 (`config_version_hash`)，严禁回放静默填补缺失行情。

## 负责人角色
- DRI：Gemini (总工程师兼实施人)
- Reviewer：Gemini (架构与量化风控总工审查)
- QA：Gemini (自动化单元与合同测试验收)

## 优先级与估算
- Priority：P1
- Estimate：8 SP
- Sprint：Sprint 3 (数据与回测)
- Labels：`data,replay,p1`

## 依赖
- Blocked by：MD-004, CORE-003
- Blocks：STRAT-002, STRAT-003, DERIV-004, EQ-004

## 接口定义
- 输入：标准化行情事件流 (`RecordedMarketEvent`：`snapshot` 或 `diff`) 与目标回放分区列表。
- 输出：分区文件 (`part-00000.parquet.jsonl`)、`DatasetManifest`（含 SHA-256 `dataset_version_id`、事件计数、时间范围）及 `ReplayResult`（含确定性结果指纹 `result_fingerprint`）。
- 错误/拒绝语义：分区缺失、文件为空、JSON 记录损坏或事件时间戳乱序/倒退时，立即抛出 `ReplayIntegrityError`，绝不静默跳过或插值填补缺失行情。
- 版本/时间戳/单位：`dataset_version_id` 采用全部排序事件规范化 JSON 的 SHA-256 前 16 位；`DeterministicClock` 严格随事件 `receive_time_utc` 单调推进。
- 与其他模块的边界：回放引擎直接驱动 `OrderBookBuilder` 与策略 Scanner (`TriangleArbitrageScanner`)，不经过实盘下单通道。

## 代码与文档交付物
- [x] 代码路径：`src/arb/replay/recorder.py`, `src/arb/replay/__init__.py`
- [x] 配置/迁移：无
- [x] 文档/ADR：`DatasetManifest` 与 `DeterministicReplayEngine` 契约文档
- [x] 指标/日志/告警：`events_processed`, `opportunities_detected`, `executable_candidates_count`, `result_fingerprint`

## 测试用例
- [x] 正常路径
- [x] 边界条件
- [x] 异常/超时/断线
- [x] 重复/乱序/幂等（适用时）
- [x] 回放/参考实现（策略任务适用时）
- [x] 安全与权限（外部 API/资金操作适用时）
- **自动化测试证据**：`test_data001_partitioned_recorder_and_deterministic_replay` (PASSED — 2 次重复回放指纹、候选数与净收益 100% 一致，损坏/缺分区显式报错)

## 验收条件
- 所有接口契约、类型校验、安全门禁与自动化测试用例 100% 通过。
- 严格遵循 `LIVE_TRADING_ENABLED=false` 默认硬门禁与 Fail-Closed 原则。

## 风险与回滚
- 已知风险与回滚方案：纯只读历史数据回放与研究模块，不触碰任何外部交易接口。
- 是否影响 live trading（默认必须为否）：**否 (`LIVE_TRADING_ENABLED=false`)**

## PR 验收
- [x] CI 通过 (`Ran 19 tests - OK`)
- [x] 测试证据附在 PR
- [x] 日志/指标/错误处理完整
- [x] 文档与配置同步更新
- [x] 无绕过风控/账本/实盘门禁的路径
- [x] Reviewer 与 QA 批准 (Signed off by Gemini)
