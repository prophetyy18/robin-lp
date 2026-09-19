# V1 USDG 本位 LP 策略经济规格

状态：已确认基线；其余数值阈值延期到 paper trading 后评审

## 1. 文档职责

本文档定义 V1 策略经济判断必须遵守的稳定口径、决策层次、可替换接口和实验
纪律。它不固定某个预测模型或拍脑袋设置数值阈值；任务、依赖和验收证据由
`todo/README.md` 管理，V4 liquidity 与 USDG 的精确数学由 T049 产出的
`V4_POSITION_SIZING.md` 管理。

## 2. 目标与基准

### `ECO-OBJ-001` — USDG 是唯一主要计价单位

系统所有策略选择、风险预算和最终绩效以 USDG 等值表示并保留原始 token 整数
数量。目标 token 是 LP 运行期间承担的库存，不把依赖 token 上涨作为策略必须
成立的盈利假设。

执行路径不受任何计价替代影响。研究路径按 ADR-014 使用显式计价层级（USDG 优先，
其次合格 USD 稳定币，ETH 仅作标注的波动计价展示），且既无 USD 族资产又无合格
兑换路径的数据集只能输出 `RELATIVE_ONLY` 的相对计价结果。该层级只改变研究报告
的呈现单位，不改变本规格其余任何一条：账本、候选动作、风险预算和两条 5 分钟
极端涨跌规则仍以合格 USDG 换算为准。

### `ECO-OBJ-002` — 主要基准是保持 USDG

主要超额收益比较“执行完整 LP 生命周期”与“同一期间不交易并保持初始 USDG”。
相同初始 token 的 HODL 与理想再平衡/LVR 基准必须保留用于归因，但不能替代
USDG cash benchmark。

报告同时区分：

- `marked_pnl_usdg`：按合格 point-in-time 价格证据估值；
- `liquidatable_pnl_usdg`：按当时可执行退出路径、深度、fee、slippage、tax、
  Hook 和 Gas 估算的可实现价值；
- `cash_benchmark_excess_usdg`：相对保持 USDG 的超额结果；
- `token_beta_pnl_usdg`：token 价格变化产生的库存损益；
- `lp_service_pnl_usdg`：fee/incentive/Hook credit 减 LVR 与执行成本后的做市结果。

缺少合格估值或退出证据时必须显示 `UNAVAILABLE`，不得用 spot price 冒充可实现
价值。

## 3. 候选动作与净经济价值

每个候选动作至少绑定：策略/参数版本、决策时间和可用时间、PoolKey、
`tickLower`、`tickUpper`、`liquidityDelta`、资金上限、最大持有期、出区间等待、
重建和最终退出规则。

对候选动作 `a` 和未来路径 `ω`：

```text
PnL_USDG(a, ω)
= final_principal_liquidatable_value_usdg
  + fees_usdg
  + explicit_incentives_usdg
  + hook_net_credits_usdg
  - initial_cash_usdg
  - entry_swap_cost_usdg
  - gas_usdg
  - slippage_mev_tax_usdg
  - rebalance_cost_usdg
  - exit_cost_usdg
```

`final_principal_liquidatable_value_usdg` 不包含另列的可领取费用、激励或 Hook
credit；每项价值和成本只允许出现一次。`gas_usdg` 汇总所有阶段 Gas，其他 entry、
rebalance 和 exit cost 字段不得再次包含 Gas；报告必须通过分项和总额对账发现重复
或漏计。

实现可以采用不同预测模型，但最终必须输出同一组可审计量：预期净收益、盈利
概率、下行分位数/Expected Shortfall、触及上下边界概率、预计 Range 内时间、
预计 fee、全部成本、数据/模型不确定性和拒绝原因。

## 4. 三层决策结构

### `ECO-GATE-001` — 基本面与合约风险只控制资格和风险预算

Token/Hook 身份、代码、权限、供应/解锁、holder、流动性撤出、安全事件和外部
证据进入准入、最大资金、暂停和人工复核。`ADM-TECH-*` 失败直接阻止；慢速风险
可以降低资金或提高所需安全边际；best-effort 信息缺失保留为不确定性，不伪造
精确概率。

基本面、团队和舆情不得直接成为自动短周期方向交易信号。若未来要改变此边界，
必须先修改产品目标和本规格，而不能通过新增特征偷偷引入。

### `ECO-REGIME-001` — 市场状态模型只使用当时可得市场数据

V1 使用决策时已经可获得的价格、交易量和流动性分布识别至少：
`RANGE`、`UP_TREND`、`DOWN_TREND`、`JUMP_RISK`、`UNCERTAIN`。允许从价格历史
计算波动、下行半方差、偏度、趋势、突破持续、Range 停留/返回时间等派生量。
活动池提供原始相对价格；USDG 价格特征和完整 5 分钟 K 线必须来自 T053 合格、
按时间点可复现的 USDG 换算。换算来源不得带入非市场基本面信号。

USDG 本位对下跌风险采用非对称处理：`DOWN_TREND`、向下跳跃、流动性撤离或
下边界持续失效必须比同幅上涨得到更严格的资金、入场或退出响应。传统 CTA
特征主要作为趋势风险过滤器；Range/均值回归证据负责支持区间 LP，不把“CTA
看多”直接等同于“应该做 LP”。

V1 的策略默认规则中，目标 token 的完整 5 分钟 USDG K 线涨幅超过 100% 时，
策略不得产生增加风险的候选动作，并重新评估已有仓位；这属于策略决策，不改变
系统运行状态，也不是中央风险网关的全局限制。

### `ECO-EDGE-001` — 净经济价值决定是否承担 LP 风险

手续费机会至少基于预计有效交易量、实际/动态 LP fee、预计 active-liquidity
份额和预计 Range 内时间。低流动性本身不是优势；必须同时满足最小退出深度、
最大自身流动性占比、价格操纵和交易真实性检查。

```text
expected_fee_edge
= expected_eligible_volume
   × expected_effective_lp_fee
   × expected_liquidity_share
   × expected_in_range_fraction
```

候选动作只有在技术资格、市场状态、可退出性、资金限制和保守净经济价值同时
通过时才能成为入场意图。`NO_TRADE`/保持 USDG 始终是合法且默认的候选动作。

## 5. 初始可解释模型

V1 首个实现采用可审计规则/统计基线，不以复杂 ML 为完成条件。初始特征族包括：

- 下跌风险：多尺度收益、标准化下行趋势、下行波动、负偏度、最大回撤、负跳跃、
  放量下跌和下边界突破持续；
- Range 状态：中心漂移、历史停留率、触边/突破频率、返回概率和时间、波动率及
  volatility-of-volatility；
- fee 机会：有效交易量持续性、实际 fee、active liquidity、预计份额、单位资金
  fee 和竞争流动性变化；
- 退出与自影响：退出深度、模拟滑点、自身 liquidity 占比、钱包余量与 Gas；
- 不确定性：数据覆盖、新鲜度、来源冲突、样本量和模型漂移。

特征定义必须记录单位、窗口、可用时间、缺失策略和版本。不得以名称相同但计算
不同的特征覆盖旧版本。

## 6. 阈值与实验纪律

除已经由产品目标明确指定的 V1 默认规则外，具体窗口、阈值、权重、最大亏损和
最低安全边际的最终数值延期到 paper trading 阶段决定。前期实现必须先完成不可变参数
版本、配置校验、时间样本切分、实验清单、审计和重放机制。回测和 preliminary paper
可使用明确的实验参数版本，但必须标记为 `EXPERIMENTAL_NOT_LIVE_APPROVED`。最终值在
post-testnet paper/shadow 中用未接触时间样本和 walk-forward 证据评审，live 前锁定并审批。

- 随机打乱时间序列不能作为主要验证；
- 未来路径只用于训练标签和事后评估，不能进入当时特征；
- 先声明训练/验证/测试边界、成本和淘汰规则，再查看最终结果；
- 展示参数邻域/稳定区域，不选择孤立的最高收益点；
- losing、no-trade 和失败实验保留且可复现；
- 阈值调整生成新策略版本并重新走适用晋级门槛，不能改写历史结果；
- 样本或可信度不足时输出 `UNCERTAIN`/`NO_TRADE`，不能降低门槛凑出信号。

## 7. 可维护性边界

策略框架至少分离以下可替换组件：

```text
AdmissionEligibilitySnapshot
MarketFeatureSnapshot
RegimeModel
FeeOpportunityModel
CandidatePolicy
EconomicEvaluator
StrategyDecision
```

组件通过版本化、不可变、带单位和时间语义的结构通信。规则模型、统计模型或未来
ML 模型可以替换 `RegimeModel`/`FeeOpportunityModel`，但不得改变 USDG 账本、
候选动作语义、集中风险检查、交易执行或审计事件格式。引擎不得包含按 token
symbol、具体地址或某个模型名称分支的策略逻辑。

训练模型受同一约束，并额外适用 ADR-014 第 5 条：模型只估计决策所需的未来市场量
（离开区间概率、未来已实现波动、成交量、费率密度、再平衡损失代理），不预测收益，
不直接输出仓位、规模或 tick 区间，不产生任何 paper 或 live 权限，也不得成为活动
默认实现。模型的最终判据是事件驱动回测引擎产生的 LP 经济结果；预测指标改善而
LP 经济恶化必须记为 rejection。浮点只允许存在于模型层自己命名并记录的统计边界
内，边界之外仍须满足 ADR-004 的整数精确性。

每次决策必须保存输入特征版本、模型/参数版本、候选动作、分项预测、被采用或
拒绝的规则、最终 reason codes 和后续实现结果，使旧决策在模型升级后仍可解释。

## 8. 当前暂不固定的量化参数

以下内容等待进入 paper trading、获得真实观测数据后确定并进入版本化策略参数，
而不是继续修改本规格：

- 除 5 分钟上涨超过 100% 这一已确认策略规则外，其余趋势、跳跃和 Range 状态
  的具体窗口与阈值；
- 最低历史长度、有效 episode 和 Swap 样本量；
- 最低预期收益、安全边际和盈利概率；
- 最大 Expected Shortfall、回撤及触及下边界概率；
- 最小退出深度、最大自身 liquidity 份额和成交量真实性阈值；
- paper/live 所需样本外表现、持续时间和容许失败次数。
