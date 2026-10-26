# 实盘审批门禁与熔断控制规范 (Live Trading Gates & Kill Switch Policy)

- **文档版本**: `v1.0.0`
- **关联工单**: `META-002`
- **负责人**: Gemini (总工程师兼风险负责人)

## 1. 核心铁律 (Non-Negotiable Rules)
1. **默认非实盘**：代码库、配置文件与容器镜像中 `LIVE_TRADING_ENABLED` 硬性默认为 `false`。
2. **禁止参数意外穿透**：在 `dev` 或 `paper` 环境配置下，即使外部传入 `LIVE_TRADING_ENABLED=true`，配置校验器也必须抛出 `ConfigurationError` 并拒绝启动，除非同时满足：
   - `APP_ENV == "production"`
   - 具备已签名的审批令牌（`live_approval_ticket_id`）
   - 目标策略在 `VenueCapability` 中 `live_approved == true`
3. **Fail-Closed 原则**：当风控网关、PostgreSQL/SQLite 账本、行情健康度检查或余额快照任一组件不可用或超时，系统必须拒绝一切新增风险指令。

## 2. 三级熔断机制 (Tiered Circuit Breakers)

| 熔断级别 | 触发条件 | 影响范围 | 自动动作 | 恢复条件 |
|---|---|---|---|---|
| **Strategy 级** | 单策略出现 `UNKNOWN` 状态订单、未对冲敞口超 `500 USDT` 达 5s、单策略日亏损达 `200 USDT` | 仅冻结该策略实例 | 拒绝该策略一切新 `ExecutionPlan`；允许受控减风险平仓（`reduce_only`） | 对账完成消除 `UNKNOWN` 订单 + 负责人人工重置状态表 |
| **Venue 级** | 交易所返回 HTTP `418`、连续 `429` 退避超限、盘口序列缺口未恢复、场所余额对账不一致 | 所有涉及该 Venue 的策略 | 标记该 Venue 盘口 `INVALID`；暂停该所全部开仓计划 | 场所连接与盘口快照重建对齐 + 余额对账无差异 |
| **Portfolio 级** | 全组合日亏损达 `500 USDT`、账本数据库写失败、人工触发全局 Kill Switch | 全系统所有策略与场所 | 持久化 `GLOBAL_KILL_SWITCH=LOCKED` 到数据库；进程重启后依然保持熔断 | 事故复盘完成 + 风险负责人签名执行解锁命令 |

## 3. 逐策略实盘放行清单 (Per-Strategy Live Gate Checklist)
任何策略从 `paper` 晋升至 `live` 必须逐项打钩并记录在发布档案中：
- [ ] `VenueCapability` 已确认场所 API 权限与地区合规资格。
- [ ] 公共平台 `CORE-001` 至 `CORE-012` 全部 P0 用例 100% 通过。
- [ ] 该策略专属 P0 验收矩阵（如 `S1-001`~`S1-012`）100% 通过。
- [ ] 连续 Paper Soak Test 期间零未知订单遗留、零账本差错。
- [ ] 风险负责人、后端负责人、量化负责人与 QA 签字批准。
