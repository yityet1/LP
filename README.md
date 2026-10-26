# 多策略套利系统工程底座 (`arb-platform`)

本项目为多策略套利系统（覆盖路线 A 现货三角、路线 B 期现套利、S1 跨所现货、S2 跨所衍生品及 S3/S4 扩展框架）的公共核心工程底座。当前已完成 **Sprint 0（`META-001`, `META-002`）** 与 **Sprint 1（`CORE-001` 至 `CORE-007`）** 的全部交付与自动化测试验证。

## 1. 目录结构

```text
arb-platform/
├── configs/
│   └── risk-policy.example.yaml          # [META-002] 版本化风险策略与场所能力配置
├── docs/
│   ├── venue-capability-matrix.md        # [META-001] 首批场所(Binance+OKX)与S3/S4冻结矩阵
│   ├── live-trading-gates.md             # [META-002] 实盘门禁与三级熔断规范
│   └── adr/
│       └── ADR-0001-initial-venues-and-scope.md
├── issues/                               # 实时填写的标准 Git Issue 验收工单 (META-001~002, CORE-001~007)
├── src/arb/
│   ├── config/                           # [CORE-002] 分环境配置、密钥脱敏与实盘防误触开关
│   ├── domain/                           # [CORE-003] 统一领域模型、7种价格类型分离与事件契约
│   ├── venues/                           # [CORE-004] Venue Adapter 四层抽象接口与错误分类体系
│   ├── instruments/                      # [CORE-005] Instrument Registry、规则版本化与精确量化引擎
│   ├── ledger/                           # [CORE-006] SQLite/PostgreSQL 事件存储与订单/成交幂等账本
│   └── observability/                    # [CORE-007] 结构化 JSON 日志、敏感字段脱敏、指标与告警规则
└── tests/
    ├── unit/                             # 单元测试套件
    └── contract/                         # 适配器与公共平台 P0 合同测试套件
```

## 2. 快速复现与运行测试

```bash
PYTHONPATH=src pytest -v
```
