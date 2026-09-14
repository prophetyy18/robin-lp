# V1 Token 与 Pool 准入要求

状态：草案，逐步确认中
最后更新：2026-09-14

## 1. 文档用途

本文档定义 V1 中目标 token、配对 token 和 PoolKey 进入研究、回测、paper
trading 与 live execution 前必须满足的产品要求。具体实现任务由 `TODO.md`
管理。

## 2. 基本原则

- Token 的链上技术资格与项目主体可信度必须分开判断。
- 匿名、Meme 属性、缺少官网或没有实名团队本身不构成技术硬性否决。
- “未知”既不等于安全，也不自动等于恶意，必须明确显示为未知。
- 用户可以接受已识别、可理解的高风险，但不能绕过技术正确性硬门槛。
- 所有结论必须绑定 Robinhood Chain 环境、contract address、代码身份、证据
  来源和评估时间，不能绑定 token symbol。
- 只有保证系统身份、会计和执行正确性所必需的数据属于强制信息；外部项目、
  holder 标签和舆情等增强信息采用 best-effort 获取。

## 3. 信息可用性规则

每个展示字段都必须同时记录值、来源、观测时间和可用状态。建议状态为：

- `AVAILABLE`：已从可追溯来源取得；
- `PARTIAL`：仅取得部分信息或覆盖范围有限；
- `CONFLICTING`：多个来源互相冲突；
- `UNAVAILABLE`：尝试后无法取得；
- `NOT_APPLICABLE`：该字段不适用于此 token。

不得把 `UNAVAILABLE` 显示为零、否、无风险或验证通过，也不得根据相似项目、
token symbol 或未经证实的内容补造信息。

### 3.1 技术准入必需信息

保证身份、会计和执行正确性所必需的信息仍受 `ADM-TECH-*` 约束。例如 chain
ID、contract address、实际执行代码，以及系统参与 Swap/LP 所需的 token 行为。
缺失这些信息时，系统无法通过对应能力等级的技术资格检查。

### 3.2 增强型尽调信息

团队身份、法律主体、精确流通量、团队/交易所/桥地址标签、vesting、未来解锁、
审计、社交舆情等信息不保证一定能够取得。系统应在能够从可追溯来源获得时
展示；无法获得时明确标记状态和最后尝试时间，但不因该字段缺失自动否决。

增强信息缺失表示“证据覆盖不足”，不表示事实不存在或风险较低。用户仍可在
看到信息覆盖情况后决定是否接受。

## 4. 双轨审批模型

每个 token 必须同时产生三个彼此独立的结果。

### 4.1 Technical Eligibility

判断系统能否正确理解和处理该 token 的实际链上行为。建议状态为：

- `UNKNOWN`：证据不足，只允许展示和继续调查；
- `BLOCKED`：存在不可绕过的技术失败；
- `CONDITIONAL`：行为已知，但需要受支持的特殊会计或执行模型；
- `ELIGIBLE`：在指定能力等级下技术检查通过。

### 4.2 Project Risk

评估匿名性、治理、holder、供应、流动性、项目历史和外部信息。建议状态为：

- `UNKNOWN`
- `LOW`
- `MEDIUM`
- `HIGH`
- `VERY_HIGH`

风险等级必须附带事实和证据，不能只输出一个分数。

### 4.3 User Decision

记录用户是否愿意承担已经展示的项目风险：

- `PENDING`
- `APPROVED`
- `APPROVED_WITH_LIMITS`
- `REJECTED`
- `REVOKED`

最终准入结果由技术资格、项目风险、用户决定和适用能力等级共同决定。用户
批准 `VERY_HIGH` 项目风险，不得把 `Technical Eligibility: BLOCKED` 改成可用。

## 5. 技术硬门槛

### `ADM-TECH-001` — 链与代码身份

- 要求：实际连接必须是批准的 Robinhood Chain 环境；输入地址必须存在合约
  代码，代码身份必须能够稳定读取和记录。
- 失败行为：chain ID、地址或代码身份不一致时，禁止所有后续阶段。
- 证据：chain ID、固定区块、contract address、runtime bytecode hash、RPC
  来源与观测时间。

### `ADM-TECH-002` — Metadata 不作为身份

- 要求：symbol、name 和 decimals 仅用于显示；冲突、缺失或异常返回不能改变
  token 身份。
- 失败行为：显示告警；不得根据 symbol 自动替换或合并 token。
- 证据：原始调用结果及解析状态。

### `ADM-TECH-003` — 实际执行代码可确定

- 要求：代理、implementation、diamond/facet 或其他委托执行路径必须能够被
  识别，并记录其管理员和升级能力。
- 失败行为：无法确定实际执行代码时，只允许展示和调查，不得进入策略。
- 证据：代理类型、implementation/facet、相关 storage slot、code hash、管理
  地址和固定区块。

### `ADM-TECH-004` — 行为必须可理解和建模

- 要求：系统必须确认 LP 和 Swap 所需的余额、transfer、transferFrom、approve、
  实际到账、税费及供应变化语义。
- 失败行为：无法理解或无法建模时，不得生成可信 backtest、paper 或 live
  结果。
- 证据：源码/字节码分析、历史成功与失败交易、固定区块调用或模拟，以及会计
  对账结果。

### `ADM-TECH-005` — 明确恶意或不可退出行为

- 要求：不得使用已确认无法正常卖出、存在 honeypot、能未经授权移动用户资产，
  或存在其他已确认恶意执行路径的 token。
- 失败行为：`BLOCKED`，用户不能覆盖；保留证据供查看和审计。
- 证据：可复现的调用/模拟/历史交易、代码路径和失败原因。单一第三方标签不足
  以独立满足证据要求。

### `ADM-TECH-006` — 源码或等价代码证据

- 要求：没有 verified source 不自动否决匿名 token。系统可以通过 runtime
  bytecode 与已经审查的实现精确匹配、可复现构建或其他独立证据确认代码行为。
- 失败行为：如果所有方法都不足以确定实际行为，则保持 `UNKNOWN`，禁止 live。
- 证据：verified source/build provenance，或精确 bytecode 匹配对象及其版本、
  checksum 和差异结果。

### `ADM-TECH-007` — 身份或权限变化使审批失效

- 要求：代码、implementation、facet、管理员或关键权限变化必须使依赖旧状态
  的批准失效。
- 失败行为：停止新增风险，暂停相关策略资格并要求重新调查和人工审批。
- 证据：旧/新状态、首次发现区块、受影响授权和处置记录。

### `ADM-TECH-008` — 外部身份冲突

- 要求：如果项目存在官方或高可信地址声明，必须与用户输入地址比较；匿名项目
  没有官方声明时应记录“不可获得”，不能伪造或推断官方身份。
- 失败行为：存在未解决冲突时禁止 live；没有官方来源本身不构成硬性否决。
- 证据：来源 URL、发布时间、采集时间、声明地址和链上地址。

### `ADM-TECH-009` — 审批必须绑定当前版本

- 要求：策略启动和每次 live 操作前，必须证明当前代码与权限状态仍对应有效
  审批。
- 失败行为：不能证明时禁止新增 live 操作并告警。
- 证据：approval ID、批准时状态摘要、当前状态摘要和比较结果。

### `ADM-TECH-010` — 审批生命周期与重新复核

- 有效期：Token 审批默认没有固定日历到期日。审批在用户未撤销，且其绑定的
  chain ID、contract address、runtime code hash、代理 implementation/facet、
  关键管理权限、已建模行为和技术硬门槛均未变化时持续有效。
- 提醒：系统可以保存 `review_reminder_at` 并通知用户定期复核，但提醒到期不等于
  审批失效，不得单独阻止一个其他条件仍有效的策略。
- 技术变化：上述身份、代码、权限、行为或硬门槛任一变化时，状态立即变为
  `INVALIDATED`；停止新增 LP、Swap、增仓和复投，保留减仓、领取、退出和撤销
  授权能力，并要求以新证据重新调查和人工审批。
- 项目风险变化：可信安全事件、异常增发/解锁、holder 或流动性显著集中、稳定币
  脱锚等实质变化使状态进入 `REVIEW_REQUIRED`；用户复核前不得新增风险。
- 无法验证：短暂 RPC、索引、外部来源或监控故障进入 `UNVERIFIED_PAUSE`，不把
  旧审批静默续期或永久撤销。只有重新取得证据并证明绑定状态未变化后才可恢复；
  无法证明时继续停止新增风险。
- 审计：创建、提醒、暂停、恢复、失效、复核、拒绝和主动撤销均产生不可变记录，
  包含原因代码、证据时间、旧/新状态摘要、受影响权限、PoolKey 和策略授权。
- 禁止：不得因“长时间没有告警”自动续期，不得用 token symbol 继承审批，也不得
  允许用户覆盖技术硬门槛或让复核自动扩大 `HOLD`、`LP`、`AUTO_SWAP` 和敞口。

## 6. 匿名与 Meme Token 的处理

匿名或 Meme token 可以在技术检查充分时取得 `Technical Eligibility`，但项目
信息缺失必须在 `Project Risk` 中体现，不能默认为低风险。

系统应尽可能通过链上证据检查：

- runtime bytecode 与已知实现的匹配；
- owner、管理员、升级、mint、pause、blacklist、tax 和 max-wallet 权限；
- 历史买入、卖出和实际到账比例；
- deployer、管理员、大 holder 与 LP 地址之间的关联；
- holder、供应和流动性变化；
- 固定区块上的交易模拟与 LP 会计结果。

允许出现以下结果：

```text
Technical Eligibility: ELIGIBLE
Project Risk: VERY_HIGH
User Decision: APPROVED_WITH_LIMITS
```

这表示技术上可正确执行，不代表项目安全、优质或不会归零。

## 7. 可由用户接受的项目风险

以下事实不单独构成技术硬性否决，但必须披露并进入项目风险判断：

- 团队匿名或缺少法律主体；
- 没有审计、正式文档或长期项目历史；
- holder 或流动性高度集中；
- 流动性未锁定；
- 存在已经识别且能正确建模的增发、暂停、黑名单、税费或升级权限；
- 交易量、价格和流动性剧烈波动；
- 外部舆情争议或可信信息不足。

用户接受高风险时，审批应支持资金上限、允许操作、有效期、重新审批条件和
更严格退出阈值，但系统不得因为发现已知管理权限就替用户自动拒绝。是否接受
以及是否附加限制由用户决定。

### `ADM-RISK-001` — 已知管理权限完整展示

- 要求：对于 mint、pause、blacklist/freeze、tax、max-wallet/max-transaction、
  upgrade 等能力，系统必须展示权限是否存在、控制者、权限范围、数值上限、
  timelock/多签情况、当前配置和历史调用。
- 展示：同时说明最坏情况下对持仓、卖出、LP 会计和退出能力的影响。
- 决策：只要实际行为可被系统理解和正确处理，这些权限本身不触发技术硬阻断；
  由用户在看到信息后批准、附条件批准或拒绝。

### `ADM-RISK-002` — 不替用户作项目风险决定

- 要求：系统可以给出风险标签、证据和最坏情况说明，但不能仅因为团队匿名、
  无审计、holder 集中、流动性未锁、存在管理员权限或负面舆情自动否决。
- 决策：上述项目风险由用户人工决定；系统必须保留当时展示的信息和决定记录。
- 边界：该规则不能覆盖 `ADM-TECH-*` 技术硬门槛。

### `ADM-RISK-003` — Holder 与供应信息 best-effort 展示

- 展示：在数据可用时展示总供应/流通信息、Top holder 集中度、地址类型调整、
  deployer/管理员关联、锁仓与解锁、holder 趋势、大额转账和 LP 控制情况。
- 决策：集中程度和供应风险不设置自动否决阈值，由用户结合数据覆盖情况判断。
- 缺失：标签、归属、流通量或解锁信息无法可靠取得时标记 `UNAVAILABLE` 或
  `PARTIAL`，不阻止人工审批，也不得推测填充。
- 告警：系统可对已取得数据的显著变化告警，但告警本身不替用户作准入决定。

## 8. 配对 Token 准入

### `ADM-PAIR-001` — 配对资产独立审批

- 要求：每个候选池的配对 token 必须具有独立于目标 token 的技术资格、项目
  风险和用户决定。
- 边界：目标 token 的批准不得自动传递给配对 token，也不得从 token symbol
  或其他池的同名资产继承。
- 失败行为：配对 token 未获批时，对应 PoolKey 不得成为活动池。

### `ADM-PAIR-002` — ERC-20 配对资产

- 要求：ERC-20 配对 token 采用与目标 token 相同的双轨检查、信息可用性规则
  和技术硬门槛。
- 展示：系统应额外说明该资产作为 LP 库存和报价资产时的风险，包括稳定币
  脱锚、冻结、赎回和供应风险；信息不可得时遵守 best-effort 规则。

### `ADM-PAIR-003` — Robinhood Chain 原生 ETH

- 要求：原生 ETH 必须被明确标记为 native currency，而不是地址为零的普通
  ERC-20，不执行不适用的 ERC-20 contract/approve 检查。
- 验证：仍需校验 Robinhood Chain 环境、余额单位、Gas 预留及 PoolKey 中的
  canonical native-currency 表示。
- 决策：原生身份验证通过后仍需用户明确批准其作为配对资产和允许的敞口。

### `ADM-PAIR-004` — Token 操作权限

- 默认值：未审批时 `HOLD=false`、`LP=false`、`AUTO_SWAP=false`。成为活动池前
  用户必须明确开启 `HOLD` 和 `LP`；`AUTO_SWAP` 单独审批，不从 `LP` 自动继承。
- 默认敞口：配对 token 最大 USDG 敞口默认等于当前策略的 LP 资金上限，以容纳
  出区间后的最坏单边库存；用户可以设置更低值，最终仓位必须据此整体缩小。
- 权限：目标 token 和配对 token 分别记录 `HOLD`、`LP`、`AUTO_SWAP` 与最大
  USDG 敞口；`LP`/`AUTO_SWAP` 依赖 `HOLD`，但依赖校验不得自动扩大权限。
- 活动池要求：两种资产必须允许 `HOLD` 和 `LP`；需要自动配比或再平衡 Swap
  的策略还要求相关方向的 `AUTO_SWAP` 权限。
- 降权：撤销 `AUTO_SWAP` 或 `LP` 只阻止新的对应动作；领取、减仓、退出和
  撤销授权保持可用。撤销 `HOLD` 时已有或退出所得余额必须如实展示并等待人工
  处置，不能删除、隐藏或自动外转。
- 审批：任何提权或提高敞口都生成新版本并使相关 live 授权失效；降低权限立即
  生效，不触发自动清仓。
- V4 仓位缩放：任一 token 上限降低时保持获批 `tickLower`/`tickUpper` 不变，
  缩小单一 `liquidityDelta`，再按当前 `sqrtPriceX96` 和 canonical V4 整数数学
  计算 `amount0`/`amount1`。不得独立削减一边或自动修改 Range；区间外新增仓位
  只需要单一 currency 属于 V4 正常结果。
- 最坏敞口：准入检查必须分别计算价格到达上下边界后的单边库存，并计入钱包
  余量、已计费用和 Hook 返回的 `BalanceDelta`。包含 canonical native ETH 时，
  LP 可用余额必须排除独立批准的 Gas 储备。
- 执行约束：使用明确 liquidity 与 `amount0Max`/`amount1Max`、deadline 和预执行
  结果限制 Mint/Increase；舍入后任一边超限、Hook 结算影响无法验证，或缩小后
  不满足最小经济仓位时不得进入。不得使用缺少最低 liquidity 保护的
  delta-derived Mint/Increase 路径。

## 9. PoolKey 与 Hook 准入

### `ADM-POOL-001` — PoolKey 独立身份

- 要求：每个池必须使用完整 PoolKey 和 canonical PoolId 独立识别，包括
  currency0、currency1、fee、tick spacing 和 hooks。
- 边界：同一 token pair 的不同 fee、tick spacing 或 hook 是不同池，一个池
  的批准不得自动传递给另一个池。
- 证据：Initialize 事件、PoolKey、派生 PoolId、PoolManager 身份和固定区块。

### `ADM-POOL-002` — Live USDG 估值资格

- 要求：进入 live 前，目标 token、配对 token 和 ETH 必须能够通过合格、带
  来源和时效的价格证据换算为 USDG 等值；是否存在可执行的 USDG Swap route
  作为独立字段记录，不是获得估值资格的必要条件。
- 可接受证据：已验证的独立预言机，或经过流动性、时效、操纵风险和来源一致性
  检查的间接换算。USDG/USD 偏离必须进入换算，不能固定假设二者始终为 1。
- 失败行为：活动池即时价格不得单独承担 live 风险估值；没有合格估值时禁止
  新增 LP、Swap、增仓或复投，但不得阻止减少/移除 LP、领取资产、撤销授权和
  紧急退出。
- 证据：原始数量、换算路径、每个价格观测、观测/可用时间、置信等级、失效
  时间、USDG 等值和 USDG route 可用状态。

### `ADM-HOOK-001` — 无 Hook 的池

- 要求：hooks 为 canonical zero address 时，按标准 Uniswap V4 模型验证。
- 边界：无 hook 只表示不存在该类扩展，不代表 token、流动性或策略风险较低。

### `ADM-HOOK-002` — Hook 身份与行为展示

- 要求：有 hook 时，系统应展示 hook address、runtime code hash、代理实现、
  管理员、permission flags、源码/字节码证据、动态 fee、回调、外部依赖及其
  可能改变的 Swap、LP、fee 和余额结果。
- 可用性：项目背景等增强信息遵守 best-effort 规则；确定实际执行代码和影响
  交易会计所需的信息属于技术必需信息。

### `ADM-HOOK-003` — 未知或无法建模的 Hook

- 要求：permission flags 只能证明可能调用哪些回调，不能证明回调具体行为。
- 失败行为：如果系统无法理解、模拟并验证 hook 对交易和 LP 结算的实际影响，
  该池可以继续发现和展示，但不得成为 V1 活动池。
- 边界：用户选择不能覆盖此技术限制。

### `ADM-HOOK-004` — 已知 Hook 风险由用户决定

- 要求：hook 行为已经被正确识别和建模后，系统展示其控制者、权限、限制、
  历史行为和最坏影响。
- 决策：这些已知项目风险不自动否决，由用户批准、附条件批准或拒绝。

### `ADM-HOOK-005` — Hook 变化使 Pool 审批失效

- 要求：hook code、代理 implementation、权限、关键配置或依赖发生变化时，
  依赖旧证据的 PoolKey 批准必须失效。
- 失败行为：停止新增风险，暂停活动池资格，展示变化并等待重新验证和人工确认。

## 10. 已延期到 Paper Trading 阶段的参数决策

以下内容不阻塞准入证据结构、采集、展示、`UNKNOWN` 语义和人工审批流程的开发。
最终指标优先级、来源等级和高风险数值在进入 paper trading 后根据可获得的真实证据决定：

- 可获得时，holder、流动性、部署时长和交易历史优先展示哪些指标；
- 高风险审批的最大资金、额外复核条件和自动退出条件；Token 审批默认无固定
  日历到期规则已经由 `ADM-TECH-010` 确定；
- 舆情和外部信息的可信来源层级。

在这些值被审批前，系统必须保留来源和缺失状态，不得伪造证据或将临时测试值用于 live。
