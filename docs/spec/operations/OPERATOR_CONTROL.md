# V1 人工接管、暂停与退出规格

状态：已确认控制原则；其余市场数值阈值延期到 paper trading 后评审

## 1. 文档职责

本文档定义 V1 自动运行、暂停、人工接管和保护性退出的状态、允许动作、触发
分类与审计要求。市场策略阈值属于版本化策略参数；实现任务由 `todo/README.md` 管理。

本规格区分四种不同意图：停止新自动动作、暂停新增风险、将控制权交给用户、
减少/移除 LP。任何界面或 API 都不得用一个含糊的“停止”同时代表这些动作。

## 2. 运行状态机

```text
RUNNING
  ├─ transient fault ───────────────→ PAUSED_RECOVERABLE
  ├─ identity/account/key fault ────→ LOCKED_REVIEW
  └─ user takeover ─────────────────→ MANUAL_CONTROL

PAUSED_RECOVERABLE
  ├─ evidence + reconciliation pass → RUNNING
  └─ fault escalates ───────────────→ LOCKED_REVIEW

LOCKED_REVIEW
  └─ explicit user decision ────────→ MANUAL_CONTROL

MANUAL_CONTROL
  ├─ approved reduce-only plan ─────→ EXITING → SAFE_HOLD
  └─ full evidence + user approval ─→ RUNNING
```

每次转换都产生不可变事件，记录旧/新状态、原因代码、证据、触发者、时间、区块、
受影响意图/交易/仓位、通知状态和允许的下一步。

### `CTRL-STATE-001` — `RUNNING`

仅在数据、资格、授权、风险、signer、交易和账本均处于允许状态时运行。策略可在
批准范围内产生和执行日常 LP/Swap 意图。

### `CTRL-STATE-002` — `PAUSED_RECOVERABLE`

用于暂时 RPC/数据/估值或 signer 可用性问题。停止新增 LP、Swap、增仓和
复投，继续补齐数据、跟踪交易和核对；不得仅因暂停自动退出。原因消失后仍须完成
预定义的新鲜度、完整性、pending transaction、nonce、余额、仓位和账本核对，
全部通过才可自动恢复。

### `CTRL-STATE-003` — `LOCKED_REVIEW`

用于链/PoolKey/Token/Hook/代理代码或权限身份变化、未知交易/nonce、钱包或账本
不一致、持续 RPC 分歧、深度重组、风险服务失效、模拟语义改变或密钥疑似泄露。
停止新增风险且不得自动恢复，也不得假定 Remove Liquidity 必然安全；必须通知
用户并等待明确决定。

### `CTRL-STATE-004` — `MANUAL_CONTROL`

自动策略停止产生新意图，尚未广播的旧意图失效。已广播/pending 交易不能标记为
“已取消”；系统必须继续跟踪，只有确认被替换、回滚或最终上链后才能关闭。用户
基于最新核对结果选择继续暂停、恢复、领取、减仓、完全移除、独立批准的退出
Swap、撤销授权或外部钱包接管。

### `CTRL-STATE-005` — `EXITING` 与 `SAFE_HOLD`

`EXITING` 只执行一份已确认、带 deadline 和状态绑定的风险降低计划。完成并核对
后进入 `SAFE_HOLD`：LP 已按计划降低或移除，所得资产保留在执行钱包，自动策略
保持停止。`SAFE_HOLD` 不表示资产已兑换成 USDG、转入主钱包或风险已经消失。

## 3. 事件分类与默认动作

| 事件 | 默认状态/动作 | 不允许的隐含动作 |
| --- | --- | --- |
| 短暂 RPC、数据、估值或 signer 不可用 | `PAUSED_RECOVERABLE` | 自动退出或假装已核对 |
| 通知发送失败 | 安全动作照常、持久重试通知 | 因通知失败延迟暂停 |
| 价格出 Range | 交给批准的策略等待/重建/退出规则 | 直接紧急清仓 |
| 价格上涨或降限造成仓位超限 | 停止追加和复投 | 强制卖出已有仓位 |
| 已批准的下跌/损失/流动性市场阈值触发 | 策略退出或预授权保护性退出 | 扩大权限或改变 Range |
| Token/Hook/代理代码或关键权限变化 | `LOCKED_REVIEW` | 调用未重新验证的自动退出路径 |
| 未知交易、nonce、余额或账本差异 | `LOCKED_REVIEW` | 猜测交易失败并重复发送 |
| 密钥疑似泄露 | `LOCKED_REVIEW`，禁用该 signer | 继续用可疑密钥自动签名 |
| 用户主动紧急接管 | `MANUAL_CONTROL` | 自动恢复或自动外转资产 |

## 4. 自动退出边界

### `CTRL-MARKET-001` — 三级市场风险响应

市场信号采用与运行状态机正交的响应级别；它不新增或替代 `RUNNING`、暂停、锁定
和接管状态：

| 级别 | 行为 |
| --- | --- |
| `WARNING` | 告警、展示并保存证据；不单独改变策略权限。 |
| `NO_NEW_RISK` | 禁止新建/增加 LP、复投和增加 token 敞口的 Swap；保留监控、持有、领取、减仓和退出。 |
| `AUTO_EXIT` | 在 `NO_NEW_RISK` 基础上尝试已批准的减仓/退出计划；最新预执行不通过时转 `LOCKED_REVIEW`。 |

`WARNING` 只是观测事件；`NO_NEW_RISK` 是实际强制约束；`AUTO_EXIT` 是确定的风险
降低意图。界面、API、审计和通知必须使用这三个完整名称，不得用含义不清的
`RISK_OFF`。`NO_NEW_RISK` 不是全局运行状态；每次结果必须绑定明确的作用域和
reason code，只拒绝该作用域内增加风险的 intent。例如某策略达到 USDG 上限时，
不影响其他策略，也不强制处置该策略的已有仓位。

V1 使用目标 token 的 USDG 计价价格生成完整 5 分钟 K 线，并在周期结束时计算
`return_5m = close_usdg / open_usdg - 1`。`return_5m <= -80%` 作为策略模型之外
的应急熔断条件，触发 `AUTO_EXIT`；未完成周期不得触发。

### `CTRL-AUTO-001` — 市场风险可以预授权

只有策略已理解、数据仍可靠、交易可预执行的市场事件可以绑定自动退出，例如
版本化的持续下跌、最大损失/回撤、流动性撤离、最低退出深度、Range 外等待时间
或继续持有净经济价值阈值。阈值、观察窗口、确认规则、允许动作、最大 Gas/滑点
和适用 PoolKey 必须随策略授权版本保存。

### `CTRL-AUTO-002` — 技术不确定性默认不自动退出

合约/Hook/身份、账本、钱包、nonce、密钥和执行语义异常默认锁定并通知。只有
异常类型已预先建模、退出路径的当前代码身份仍有效、最新模拟成功且全部净
`BalanceDelta`、授权、Gas、deadline 和最小输出检查通过时，才可执行明确预授权
的 reduce-only 计划；否则等待人工接管。

### `CTRL-MARKET-002` — Episode 亏损与峰值回撤

系统分别计算当前 LP episode 相对初始获批 USDG 资金的净损益，以及相对同一
episode 历史最高风险权益的回撤。两者都同时保存 USDG 绝对金额和百分比，并各自
配置 `WARNING`、`NO_NEW_RISK`、`AUTO_EXIT`；任一指标触发时采用最严格的响应。

```text
episode_pnl_usdg
= current_risk_equity_usdg
  - initial_approved_cash_usdg
  - net_external_cash_flow_usdg

unit_nav_usdg       = current_risk_equity_usdg / outstanding_episode_units
episode_pnl_pct     = unit_nav_usdg / initial_unit_nav_usdg - 1
drawdown_pct        = unit_nav_usdg / high_watermark_unit_nav_usdg - 1
drawdown_usdg       = (unit_nav_usdg - high_watermark_unit_nav_usdg)
                      * outstanding_episode_units
```

Episode 开始时以初始资金按 `initial_unit_nav_usdg = 1` 创建份额。真正的外部入金
或取款按资金流发生前的单位净值发行或赎回份额；策略内部的 Swap、增加/减少
liquidity、费用领取、复投和 Range 重建不改变份额。单位净值及其 high-water mark
因此只随真实净损益变化，不会因追加资金改善，也不会因取款重置。取款可以合理
降低仍在险资金对应的 `drawdown_usdg`，但不得改变 `episode_pnl_usdg` 或抹去历史
触发记录；所有阈值响应仍取最严格结果。

`current_risk_equity_usdg` 覆盖分配给该策略的钱包余量、LP principal 和可领取
费用，并扣除当前退出所需的 Gas、fee、slippage、tax、Hook 和其他可验证成本。
系统同时保留 marked 与 liquidatable 口径及来源；无法可靠估值时至少进入
`NO_NEW_RISK`，无法安全预执行退出时不得盲目 `AUTO_EXIT`。

外部入金/取款只调整 cash flow 和份额；增加/减少 liquidity、复投、领取、Swap
和 Range 重建均属于同一 episode，不能重置初始基准或 high-water mark。只有
完全结束该 episode、结清并固化最终损益后，下一次独立入场才建立新基准。实现
必须防止通过拆分仓位、重启服务、修改策略版本或反复重建隐藏累计亏损和回撤。

## 5. 人工接管流程

1. 原子地禁止新策略意图，并为未广播意图记录失效原因。
2. 盘点所有已广播/pending/replaced/unknown 交易，锁定 nonce 序列。
3. 在固定区块核对 chain、PoolKey、Token/Hook code hash、position liquidity、
   fee、余额、allowance 和内部账本。
4. Web 展示每个可选动作、预计 `BalanceDelta`、成本、风险、会做和明确不会做的
   事情；证据不足的动作不可选择。
5. 用户重新身份确认；需要签名时只在隔离终端解锁 signer。
6. 使用最新状态重新模拟整笔原子 V4 操作，并校验授权版本、nonce、deadline、
   `amountMin/amountMax`、Hook delta、Gas 和风险降低性质。
7. 广播后持续跟踪 replacement、revert、reorg 和 finality；不因前端超时重发。
8. 链上结果、钱包、position 和账本完全核对后进入 `SAFE_HOLD` 或继续锁定。
9. 恢复 `RUNNING` 必须是独立的用户决定，退出成功不能自动恢复策略。

## 6. V4 保护性退出计划

默认完全退出计划按当前状态解析并执行：失效未广播新增风险意图；处理已存在的
pending nonce；预执行明确的 Decrease/Remove Liquidity；校验 principal、fees
和 Hook 修改后的 caller `BalanceDelta`；领取资产；在独立模拟和批准下撤销
PositionManager/Permit2/Token 相关授权；最后核对。

V4 Hook 可以在 Remove Liquidity 前后执行并返回 delta，因此 Hook 身份或语义
变化时不能只依赖无 Hook 的金额公式。一次 unlock 中的 Swap、Modify Liquidity、
Take/Settle 等动作必须按最终净 delta 整体校验，不能用某个中间余额证明成功。

默认退出不包含自动卖出目标 token、兑换 USDG、转入主钱包、切换 Pool 或重启
策略。需要退出 Swap 时，它是独立动作，必须具有 `AUTO_SWAP`/退出授权、可用
route、价格保护和单独审计结果。

## 7. CLI 控制面

### `CTRL-CLI-001` — 只读查询与 reduce-only 写入

CLI 保留只读查询入口：查看当前权限版本、审批与授权记录、仓位、账本、通知状态和
审计记录。只读子命令不得写入任何状态。

CLI 的写入面只包含降低风险的操作：减少或移除 LP、领取已计资产、退出、关闭
`HOLD`/`LP`/`AUTO_SWAP`、降低任一 USDG 敞口上限。每一次被接受的写入都使用与 Web
相同的版本化记录，并产生带 actor、目标、旧/新版本、原因和证据的不可变审计事件；
被拒绝的写入同样留下记录和原因代码。

任何提高权限、提高敞口或新增风险的动作都不得由 CLI 完成。此类请求必须 fail closed，
不产生任何版本或动作，并指向 Web 写入入口（`WEB-GLOBAL-003`）。CLI 不得绕过风险
网关、预执行或签名边界，不提供通用交易或任意调用入口，也不接受私钥、Keystore
密码或助记词作为参数、环境变量、文件或交互输入；解锁仍只通过不回显的交互式终端。

降低风险的操作不得被增加风险的流程、待处理审批或数据不足阻塞；其可用性以最新核对
结果为准。退出成功不自动恢复策略，也不代表资产已兑换或已转出（见第 5、6 节）。

## 8. 外部钱包接管与密钥事件

用户可以在系统之外使用备份的加密 Keystore 和本人密码恢复钱包。系统不导出
明文密钥，也不把外部动作当作自身意图。观测到外部 nonce/交易后立即锁定，导入
链上证据并重建账本；完全核对和用户重新批准前不得恢复自动执行。

若怀疑 signer 或私钥泄露，系统不得继续使用该 signer 执行所谓“自动救援”。
用户通过独立可信环境决定外部接管、资产迁移和旧授权处置；系统只做只读跟踪和
后续核对，直到新执行钱包和授权完成独立审批。

## 9. 尚待实验确定的市场阈值

除已经确认的完整 5 分钟 USDG 跌幅达到 80% 应急条件外，以下参数的最终值延期到
paper trading 阶段，再在 `STRATEGY_ECONOMICS.md` 的实验纪律下版本化确定：其他持续
下跌和跳跃条件、最大
损失/回撤、流动性撤离、最低安全退出深度、Range 外等待时间、继续持有净经济
价值、安全缓冲以及允许的 Gas/滑点。技术身份不一致、未知 nonce、非零账本差异
和密钥疑似泄露属于事件触发，不通过统计阈值弱化。开发阶段仍必须实现参数 schema、
版本、测试夹具、证据和审批绑定；临时测试值不得成为 live 默认值。
