# Multi-Strategy Arbitrage Platform (`arb-platform`)

**版本**：`v1.0.0`（Sprint 0 ~ Sprint 11 全量 43 个工单 100% 验收完成，41 项自动化测试全部通过）  
**总工程师 / 实施人 / 负责人 (DRI)**：Gemini  
**GitHub 远程仓库**：`https://github.com/yityet1/LP.git`

---

## 1. 项目简介与核心设计哲学

`arb-platform` 是一套面向生产级工程标准构建的**模块化多策略套利与量化执行底座**，完整覆盖从市场数据接入、确定性历史回放、真实盘口深度成本核算、下单前风控与资金预占、事务性 Outbox 订单状态机、账实对账与崩溃恢复，到六大套利策略家族与组合级资本分配的全生命周期：

1. **安全默认（Fail-Closed & Paper-First）**：
   - 全局硬性默认 `LIVE_TRADING_ENABLED=false`、`APP_ENV=paper`。
   - 任何非 `production` 环境尝试开启实盘、缺失风控策略配置、或者未获得 `Quant Lead + Risk Lead + SRE Lead` 三方发布门禁签字（`REL-001`），系统均在启动或风控网关层直接拒绝执行。
2. **可执行价格与参考价格严格隔离**：
   - 所有策略扫描器与成本引擎买入严格遍历 L2 `ASK` 档位、卖出严格遍历 L2 `BID` 档位，严禁将 `last_trade_price`、`mark_price`、`index_price` 或 AMM 池中间价（`mid_price`）当作可成交价格。
3. **非原子执行假设与未知状态零盲发**：
   - 严禁假设跨所、期现、股票或 CEX–DEX 双腿能够原子成交。下单超时或网络中断一律转入 `OrderStatus.UNKNOWN`（链上为 `ChainTxStatus.PENDING`），先查单对账或同 Nonce 替换，绝不盲目重发。
4. **全链路可追溯账本与确定性回放**：
   - 每笔交易通过 `cycle_id` 在 SQLite/PostgreSQL 账本中完整串联 `Opportunity -> RiskDecision -> ExecutionPlan -> Outbox -> OrderEvent -> Fill -> Fee`。

---

## 2. 仓库目录结构与模块速查

```text
arb-platform/
├── configs/
│   └── risk-policy.example.yaml       # 全局与策略级风险限额、新鲜度门禁与场所能力矩阵配置
├── docs/
│   ├── adr/ADR-0001-initial-venues-and-scope.md
│   ├── venue-capability-matrix.md     # 交易场所（Binance/OKX/Broker/EVM/Options/Bridge）能力矩阵
│   ├── live-trading-gates.md          # G1~G4 实盘晋级与签字门禁规范
│   └── runbooks/                      # 6 份 P0/P1 标准应急演练与故障处置手册
├── issues/
│   ├── Git_Issue_任务索引_实时状态追踪.csv # 全量 43 个工单实时状态追踪总表 (100% DONE)
│   └── META-001.md ~ REL-001.md       # 43 份已验收的标准 Git Issue 工程工单
├── src/arb/
│   ├── config/settings.py             # 环境配置加载器、SecretStr 自动脱敏与实盘门禁校验 (CORE-002)
│   ├── domain/models.py               # 统一领域模型：Instrument, OrderBook, Opportunity, Fill 等 (CORE-003)
│   ├── instruments/registry.py        # 交易规则注册表与 Decimal 步长/价格/乘数精确量化 (CORE-005)
│   ├── ledger/store.py                # ACID 交易账本、Outbox、租约锁与审计链查询 (CORE-006)
│   ├── observability/telemetry.py     # 结构化脱敏 JSON 日志、Prometheus 指标与分级告警 (CORE-007)
│   ├── venues/
│   │   ├── base.py                    # 四层 Venue Adapter 抽象接口与统一错误分类 (CORE-004)
│   │   ├── binance_spot/              # Binance Spot REST & WebSocket 适配器 (MD-001, MD-002)
│   │   ├── okx_spot/                  # OKX V5 Spot REST & WebSocket 适配器 (VENUE-001)
│   │   └── derivatives/adapters.py    # Binance USDM & OKX Swap 衍生品适配器与 ctVal 乘数建模 (DERIV-001)
│   ├── marketdata/
│   │   ├── orderbook.py               # 官方增量同步 L2 OrderBook 构建器与深度切片引擎 (MD-003)
│   │   └── health.py                  # 行情健康度、新鲜度与跨所时钟偏差门禁 (MD-004)
│   ├── replay/recorder.py             # 按 venue/date/symbol 分区的行情录制与确定性回放引擎 (DATA-001)
│   ├── costs/engine.py                # L2 逐档滑点模拟、多级手续费、BNB 抵扣与净收益引擎 (COST-001)
│   ├── strategy/lifecycle.py          # Opportunity 状态机、快照指纹去重与 TTL 过期管理 (STRAT-001)
│   ├── risk/gateway.py                # 下单前风控网关、并发安全资金预占与持久化 Kill Switch (RISK-001)
│   ├── execution/fsm.py               # 订单状态机、事务性 Outbox、单 Worker 排他租约与晚到成交保护 (EXEC-001)
│   ├── reconciliation/engine.py       # 订单/成交/余额定时对账与漂移自动熔断引擎 (EXEC-002)
│   ├── paper/engine.py                # Paper 模拟撮合适配器与端到端多腿仿真器 (EXEC-003)
│   ├── testing/fault_injection.py     # P0 四大混沌故障注入演练套件 (EXEC-004)
│   ├── equities/                      # S3 股票产品条款审批注册表、代币/永续与券商适配器 (EQ-001~003)
│   ├── dex/                           # S4-A 链上 RPC 共识、AMM 池曲线模拟器与 10 态交易状态机 (DEX-001~003)
│   ├── options/                       # S4-B 期权合约模型、Black-76 定价、IV 反解器与 Greeks 引擎 (OPT-001~002)
│   ├── bridge/ledger.py               # S4-C 跨链桥状态机、在途资金隔离账本与超时熔断器 (XCH-001)
│   ├── portfolio/allocator.py         # 组合级资本分配、集中度控制、策略隔离与成本归因 (PORT-001)
│   ├── release/gates.py               # G1~G4 发布门禁、Paper Soak 评估与多角色签字控制器 (REL-001)
│   └── strategies/
│       ├── triangle/scanner.py        # S1-A 同所现货三角套利扫描器 (STRAT-002)
│       ├── cross_cex_spot/scanner.py  # S1-B 跨交易所现货预置库存套利扫描器 (STRAT-003)
│       ├── basis/scanner.py           # S2-A 期现 Cash-and-Carry 与永续基差套利扫描器 (DERIV-002)
│       ├── funding/scanner.py         # S2-B 永续资金费率套利与强平/保证金成对减仓监控 (DERIV-003)
│       ├── equity_arb/scanner.py      # S3 股票代币/挂钩永续/传统股票跨市场扫描器 (EQ-004)
│       ├── cex_dex/scanner.py         # S4-A CEX-DEX 跨市场套利扫描与非原子恢复规划器 (DEX-004)
│       ├── options_arb/scanner.py     # S4-B 期权 Put-Call Parity 平价与组合风险限额扫描器 (OPT-003)
│       └── cross_chain/scanner.py     # S4-C 跨链价差与在途时间波动折扣扫描器 (XCH-002)
├── tests/unit/                        # 覆盖 Sprint 0 ~ Sprint 11 的全量自动化测试套件 (41 项测试)
└── push_to_github.sh                  # 一键推送至 https://github.com/yityet1/LP.git 的辅助脚本
```

---

## 3. 环境准备与快速开始

### 3.1 环境依赖
- **Python 版本**：Python `3.11+` 或 `3.12+`
- **核心依赖**：`pydantic >= 1.10`, `PyYAML >= 6.0`（均已内置于标准量化环境，SQLite3 使用 Python 标准库）

### 3.2 配置文件初始化
1. 复制环境变量模板（默认锁定 `APP_ENV=paper` 与 `LIVE_TRADING_ENABLED=false`）：
   ```bash
   cp .env.example .env
   ```
2. 检查风险策略配置 `configs/risk-policy.example.yaml`：
   - `max_single_order_notional`: `1000.00` USDT
   - `max_cycle_notional`: `2000.00` USDT
   - `max_strategy_notional`: `10000.00` USDT
   - `max_portfolio_notional`: `25000.00` USDT
   - `max_book_age_ms`: `500` ms，`max_cross_venue_skew_ms`: `250` ms
   - `min_net_edge_bps`: `5.0` bps

### 3.3 运行全量自动化测试套件（Sprint 0 ~ Sprint 11）
在项目根目录下运行：
```bash
PYTHONPATH=src python3 -m unittest discover -s tests/unit -p "test_*.py" -v
```
也可以单独运行任意 Sprint 的专项测试文件：
```bash
# Sprint 0 & 1 公共核心与全量回归测试
PYTHONPATH=src python3 tests/unit/test_sprint0_and_core.py -v

# Sprint 2 行情同步与健康度门禁测试
PYTHONPATH=src python3 tests/unit/test_sprint2_marketdata.py -v

# Sprint 3 历史回放、成本引擎与 S1 三角套利测试
PYTHONPATH=src python3 tests/unit/test_sprint3_strategy.py -v

# Sprint 4 OKX V5 适配器与 S1 跨所现货套利测试
PYTHONPATH=src python3 tests/unit/test_sprint4_cross_cex.py -v

# Sprint 5 风控网关、订单状态机、对账、Paper 撮合与 P0 故障注入演练
PYTHONPATH=src python3 tests/unit/test_sprint5_execution_and_risk.py -v

# Sprint 6 S2 衍生品基差、资金费率套利与强平保护测试
PYTHONPATH=src python3 tests/unit/test_sprint6_derivatives.py -v

# Sprint 7 S3 股票相关套利条款审批、券商时段与跨市场扫描测试
PYTHONPATH=src python3 tests/unit/test_sprint7_equity.py -v

# Sprint 8 S4-A 链上 CEX-DEX 模拟、10 态交易状态机与恢复策略测试
PYTHONPATH=src python3 tests/unit/test_sprint8_dex.py -v

# Sprint 9 ~ 11 期权定价/平价、跨链在途账本、组合资本分配与发布门禁测试
PYTHONPATH=src python3 tests/unit/test_sprint9_to_11_final.py -v
```

---

## 4. 核心模块与六大套利策略使用示例

### 4.1 加载安全配置与初始化事务账本 (`CORE-002` / `CORE-006`)
```python
from arb.config.settings import Settings
from arb.ledger.store import LedgerStore
from arb.risk.gateway import RiskGateway

settings = Settings.from_env(risk_policy_path="configs/risk-policy.example.yaml")
ledger = LedgerStore(settings.ledger_db_url)
config_hash = ledger.record_config_version(settings)
risk_gateway = RiskGateway(settings=settings, ledger=ledger)
```

### 4.2 S1 现货套利：同所三角套利 (`STRAT-002`) 与跨 CEX 预置库存套利 (`STRAT-003`)
- **同所三角套利 (`TriangleArbitrageScanner`)**：调用 `build_graph(instruments)` 预计算闭环路径（如 `USDT -> BTC -> ETH -> USDT`），行情更新时调用 `scan_incremental(updated_instrument_id, order_books)`，仅重算受影响路径。
- **跨所现货套利 (`CrossCexSpotScanner`)**：传入 `binance_spot` 与 `okx_spot` 双边 `OrderBookBuilder` 与 `BalanceSnapshot`，自动按双边可用预置库存与较粗 `step_size` 向下对齐数量，并在单所持仓集中度超阈值时输出再平衡告警。

### 4.3 S2 衍生品套利：期现基差 (`DERIV-002`) 与永续资金费率 (`DERIV-003`)
- **期现基差 (`BasisArbitrageScanner`)**：自动将现货数量与合约张数×合约乘数（如 OKX `ctVal = 0.01 BTC`）精确对齐，扣除建平仓 4 笔手续费后计算年化净基差。
- **资金费率与强平守护 (`FundingRateArbitrageScanner` & `MarginAndLiquidationMonitor`)**：将不同交易所 1h/4h/8h 资金费率折算为标准 8h 等效 bps，拦截结算时钟错位；当空头仓位距离强平价 `< 800 bps` 时自动生成**衍生品平仓 + 现货同步卖出**的成对减仓计划。

### 4.4 S3 股票相关套利 (`EQ-001` ~ `EQ-004`)
- 先在 `EquityTermsRegistry` 中登记 `EquityProductTerms`（校验 ISIN、赎回权、结算周期 `24x7` vs `T+1`、卖空允许性）与 `ComparabilityApproval`。
- 使用 `EquityCrossMarketScanner.evaluate_pair()` 扫描股票代币/永续与传统券商正股，自动拦截美股休市（`CLOSED`/`HALTED`）、除权拆股冻结窗口（`CORPORATE_ACTION_FREEZE`）或借券额度不足（`SHORT_BORROW_UNAVAILABLE`）。

### 4.5 S4 扩展套利：链上 CEX–DEX (`DEX-001`~`004`)、期权 (`OPT-001`~`003`) 与跨链 (`XCH-001`~`002`)
- **CEX–DEX (`CexDexArbitrageScanner`)**：结合 `EvmChainAdapter` 多节点共识检查与 `DexPoolSimulator.simulate_swap()` 恒定乘积曲线模拟，扣除 EIP-1559 Gas 与 MEV 预算，输出绑定 `CexDexRecoveryPlan` 的候选机会。
- **期权平价 (`OptionsRelativeValueScanner`)**：使用 `OptionPricingEngine` 计算 Black-76 理论价、隐含波动率与 Greeks，检验欧式看涨-看跌平价转换，拦截净 Delta/Vega 或压力损失超限组合。
- **跨链套利 (`CrossChainArbitrageScanner`)**：通过 `BridgeTransferLedger` 将在途跨链资产隔离在 `in_transit_balance` 中（绝不计入可用余额），扣除跨链桥费、封装资产折价与在途时间平方根波动折扣。

### 4.6 组合资本分配 (`PORT-001`) 与发布门禁签字 (`REL-001`)
- 使用 `PortfolioCapitalAllocator.request_allocation()` 统一管理跨策略资金池与集中度上限，防止同一笔可用余额被多策略并发双花。
- 使用 `ReleaseGateController.evaluate_strategy_gate(strategy_id)` 验证策略是否已完成 `>= 72h` Paper Soak、零未解释账本差异，并集齐量化、风控、SRE 三方签字。

---

## 5. Git 仓库推送指南 (`https://github.com/yityet1/LP.git`)

本仓库已配置远端 `origin = https://github.com/yityet1/LP.git`，并提供了一键推送脚本 `push_to_github.sh`。在具备外网访问权限的终端中解压仓库归档包后，直接运行：

```bash
chmod +x push_to_github.sh
./push_to_github.sh
```
即可将 `main` 分支上的全部代码、43 份验收工单与测试套件完整推送到 GitHub 仓库。

---

## 9. 跨平台代码可迁移性加固 (Cross-Platform Portability & Compatibility)

为保证本仓库在不同操作系统（Linux / macOS / Windows WSL & Native / Docker 容器）以及不同 Python/Pydantic 版本环境下的零摩擦迁移，系统内置了 `src/arb/portability.py` 兼容层与自检探针：

| 检查项 ID | 迁移性加固维度 | 解决的跨环境隐患 | 验证状态 |
| :--- | :--- | :--- | :--- |
| `PORT-ENV-01` | Python 运行时兼容 (`>=3.10`) | 统一 `Decimal` 高精度上下文与标准库依赖，零 C 扩展编译门槛 | `PASS` |
| `PORT-PYD-02` | Pydantic v1 (`1.10.x`) / v2 (`2.x`) 双栈兼容 | 提供 `compat_root_validator`（自动适配 v1 `allow_reuse=True` 与 v2 `skip_on_failure=True`）及 `model_to_json(sort_keys=True)`（修复 Pydantic v2 移除 `.json(sort_keys=...)` 导致的报错） | `PASS` |
| `PORT-SQL-03` | SQLite 跨平台 URI 与目录自愈 | `normalize_sqlite_db_path()` 兼容 POSIX `/path`、Windows 盘符 `C:/path` 与 `:memory:`，自动创建缺失的父级目录并开启 `WAL` 并发模式 | `PASS` |
| `PORT-PATH-04` | 确定性归档路径哈希归一化 | `normalize_partition_key()` 将 Windows `\` 统一转换为 POSIX `/`，确保跨平台计算的 `dataset_version_id` (SHA-256) 100% 一致 | `PASS` |
| `PORT-NUM-05` | 确定性 `Decimal` 金融精度不变量 | 严禁 `float` 参与净边际、资金费率与复式账本核销，跨架构 (`x86_64` / `arm64`) 零浮点漂移 | `PASS` |

运行迁移性自检命令：
```bash
PYTHONPATH=src python3 -c "from arb.portability import run_portability_check; import json; print(json.dumps(run_portability_check(), indent=2))"
```

---

## 10. 真实市场数据端到端测试 (Real Market Data Benchmark — 2026-10-26)

仓库内置了采集自真实市场（2026-10-26 盘中快照）的基准数据集 `datasets/real_market_20261026.json`，涵盖 **BTC/USD** (`$79,438.75 ~ $80,432.40`)、**ETH/USD** (`$2,299.86 ~ $2,334.65`)、**BNB/USDT** (`$735.72`) 及 **NASDAQ: NVDA** (`ISIN: US67066G1040`, `$210.18 ~ $213.85`)：

```bash
# 运行真实市场数据端到端基准测试并生成 datasets/real_data_test_report.json
PYTHONPATH=src python3 -m arb.testing.real_data_runner
```

---

## 11. 完整 Web UI 量化终端与 Docker 部署 (Interactive Web UI & Docker)

### 11.1 启动本地全栈 Web 管控终端
```bash
PYTHONPATH=src python3 -m arb.ui.server --host 127.0.0.1 --port 8080
```
启动后浏览器访问 `http://127.0.0.1:8080`，即可使用包含 **6 大工作台视图** 的交互式量化套利终端：
1. **Command Center (总控驾驶舱)**：实时展示 2026-10-26 真实行情走势、8 大策略信号流与组合净值曲线；支持点击 **"Re-Run Real Data Benchmark"** 实时重跑底层引擎。
2. **Strategy Matrix (6 大策略族)**：按分类筛选 S1~S6 策略，支持 **Interactive Spread Stress Simulator** 动态调节滑点与延迟预算。
3. **M7 Capital & Risk (组合分配与风控)**：展示 M7 边际效用资金分配漏斗、场内敞口上限及 **Live Kill-Switch 演练开关**。
4. **ACID Ledger & Recon (复式账本与对账)**：逐笔查阅真实行情触发的借贷平衡分录 (`SUM(debit) == SUM(credit)`)。
5. **Portability & Replay (迁移性与确定性回放)**：实时查看 5 项跨平台迁移性探针状态与 SHA-256 回放指纹对比。
6. **Sprint 0-11 Audit (43 项工程验收总览)**：支持按 Sprint 检索全部 43 项 Issue 的源码实现与单元测试映射。

### 11.2 Docker 一键构建与运行
```bash
docker compose up --build
```
