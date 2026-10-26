# [DERIV-001] 实现衍生品行情、Mark/Index、资金费率与仓位适配器

## 1. 元信息（Metadata）

- **Issue ID**：`DERIV-001`
- **标题**：实现衍生品行情、Mark/Index、资金费率与仓位适配器
- **工作流（Workstream）**：`DERIV`
- **负责人（DRI）**：Gemini（总工程师）
- **审阅人（Reviewer）**：Gemini（架构与风控负责人）
- **优先级（Priority）**：`P1`
- **风险等级（Risk Level）**：`High`
- **所属 Sprint**：`Sprint 6 — S2 衍生品基差与资金费率套利`
- **关联策略 / 场景**：`S2-A`（现期/永续基差）、`S2-B`（永续资金费率套利）
- **关联文档章节**：
  - 《总体架构与研发路线图》：`4.5 价格类型显式隔离`、`Sprint 6`
  - 《策略专项验收矩阵与发布门禁》：`4.1 S2-001 ~ S2-003`、`S2-006`
- **状态（Status）**：`CLOSED (Verified)`

## 2. 背景与目标（Context & Objective）

- **背景**：衍生品套利涉及永续合约与交割合约，必须采集 `mark_price`、`index_price`、`funding_rate`、`next_funding_time`、合约乘数（如 OKX `ctVal = 0.01 BTC`）与强平价（`liquidation_price`），且严禁将 `mark_price`/`index_price` 混用为盘口可成交价。
- **目标**：实现 `BinanceUsdmDerivativesAdapter`（`binance_usdm`）与 `OkxSwapDerivativesAdapter`（`okx_swap`），完整支持衍生品规则、资金费率快照与仓位风险快照归一化。

## 3. 范围与交付物（Scope & Deliverables）

- **交付物清单**：
  - [x] 代码/模块：`src/arb/venues/derivatives/adapters.py`（`BinanceUsdmDerivativesAdapter`、`OkxSwapDerivativesAdapter`、`DerivativeFundingSnapshot`、`PositionMode`）
  - [x] 自动化测试：`tests/unit/test_sprint6_derivatives.py::TestSprint6DerivativesAndS2Matrix::test_deriv_001_binance_usdm_and_okx_swap_adapters_and_ctval_multiplier`

## 4. 强约束与安全不变量（Hard Constraints & Invariants）

- [x] **价格类型显式分离（`S2-001`）**：`MarketSnapshot.mark_price` 与 `index_price` 仅用于保证金/强平缓冲与参考偏离计算，调用 `get_executable_price()` 严格返回 L2 `best_bid` / `best_ask`。
- [x] **合约面值乘数显式建模（`S2-003`）**：OKX Swap `ctVal` 归一化为 `Instrument.contract_multiplier`，杜绝把张数直接当成币数下单。
- [x] **费率周期归一化（`S2-006`）**：`DerivativeFundingSnapshot.standardized_8h_funding_bps` 自动将 1h/4h/8h 周期折算为标准 8 小时等效 bps 与年化 bps。

## 5. 实测验证结果（Verification Evidence）

- **测试命令**：`PYTHONPATH=src python3 -m unittest discover -s tests/unit -p "test_*.py" -v`
- **实测结论**：`test_deriv_001_binance_usdm_and_okx_swap_adapters_and_ctval_multiplier ... ok`
