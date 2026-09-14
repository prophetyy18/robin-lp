# V1 目标追踪索引

状态：计划级映射；不替代目标、规格、TODO 或验收证据
最后更新：2026-09-14

## 使用规则

本文件回答“每个 V1 目标由哪些任务交付”。目标含义以 `PROJECT_GOALS.md` 为准，
任务合同以 `TODO.md` 为准；本文件不新增要求。任务勾选只代表任务验收完成，不
自动证明目标完成。T096 必须把本索引扩展成目标 → 规格条款 → 测试/链上证据的
最终矩阵。

当前只完成 Phase 0–1。下表中包含后续任务的目标均为 `PLANNED`。

## 目标到任务

| 目标 | 主要任务 | 最终证据 |
| --- | --- | --- |
| `G-RESEARCH-01` | T020–T066 | 数据、replay、估值、实验 manifest 与报告 |
| `G-PAPER-01` | T071–T083、T093 | 实时 paper ledger、soak 与 post-testnet shadow 报告 |
| `G-STRATEGY-01`、`G-RECOMMEND-01` | T060–T066、T084–T086 | 版本化候选、样本外结果、人工选择审计 |
| `G-SIGNAL-01`、`G-STRATEGY-SPIKE-01` | T050、T053、T060、T065、T066 | point-in-time 市场特征、合格 USDG 价格、5 分钟规则和策略决策记录 |
| `G-NUMERAIRE-01`、`G-ECONOMIC-01` | T049、T052、T053、T060、T063–T066 | USDG 估值、PnL 归因、净经济价值与基准报告 |
| `G-STRATEGY-MAINTAIN-01` | T060、T063、T065、T066 | 稳定接口、不可变模型/阈值版本、重现实验 |
| `G-LIFECYCLE-01` | T051、T052、T061、T062、T065、T071 | 入场至退出的 episode ledger 与反事实路径 |
| `G-UI-01` | T084–T086 | 七页 Web 控制台和端到端用户旅程 |
| `G-AUTO-01` | T072、T080–T082、T090–T095 | 后台恢复、soak、testnet/mainnet 自动运行证据 |
| `G-AUTH-01` | T025、T070、T085、T090、T094 | 版本化策略授权、短时请求和重放拒绝测试 |
| `G-RISK-01`、`G-MARKET-RISK-LEVEL-01` | T049、T052、T053、T070、T085、T091 | 集中、带作用域的风险决策与绕过失败测试 |
| `G-LOSS-CONTROL-01` | T052、T070、T071、T085、T094 | Episode PnL、单位净值、高水位回撤及资金流测试 |
| `G-CONTROL-01`、`G-TAKEOVER-01`、`G-PAUSE-01` | T080、T081、T085、T092、T095 | 状态机、人工接管、通知、恢复和演练记录 |
| `G-EMERGENCY-01` | T050、T070、T081、T091、T092、T095 | 5 分钟应急熔断、预执行和退出演练 |
| `G-NOTIFY-01` | T080、T081、T084–T086 | 企业微信去重/重试/脱敏和 UI 状态证据 |
| `G-CHAIN-01`、`G-ENV-01` | T002、T020、T021、T024、T072、T092、T095 | mainnet/testnet 身份、部署、数据和交易证据 |
| `G-TARGET-01`、`G-TARGET-02` | T002、T022、T025、T084、T085 | contract 输入、人工审批、禁止自动切换测试 |
| `G-SCOPE-V1`、`G-DISCOVER-01` | T002、T022–T025、T084 | 多候选发现与单活动 PoolKey 约束 |
| `G-POOL-SELECT-01`、`G-POOL-SWITCH-01` | T023–T025、T043、T084、T085 | Pool 资格、单选/切换版本和审计记录 |
| `G-TOKEN-01`、`G-TOKEN-PERM-01`、`G-TOKEN-LIFECYCLE-01` | T025、T070、T084、T085 | 双资产独立资格、权限/敞口和失效状态机 |
| `G-ASSET-HARD-GATE-01`、`G-HOOK-01`、`G-POOL-01` | T021–T025、T034、T042、T043、T070 | 硬门槛、Hook evidence pack 和支持等级 |
| `G-LIVE-01`、`G-LIVE-GATE-01` | T083、T086、T090–T096 | testnet、post-testnet paper、安全复核、晋级和 mainnet lifecycle |
| `G-EXEC-01` | T070、T081、T090–T095 | allowlist、simulation、risk、sign、receipt 与 reconciliation 链路 |
| `G-SIGNER-01` | T081、T090、T092、T095 | Keystore 生成/导入/备份、交互解锁和隔离签名证据 |
| `G-FUNDING-01` | T070、T081、T085、T090、T094、T095 | 独立钱包、可用额度、超额余额和无外部转账证明 |
| `G-VALUATION-01` | T049、T052、T053、T070、T084 | 原始数量、USDG 换算来源、route 独立状态和缺失处理 |
| `G-GAS-01` | T049、T053、T070、T084、T091、T095 | ETH 储备、Gas 预算/消耗和链上费用对账 |
| `G-LIMIT-01`、`G-V4-SIZING-01` | T025、T049、T051、T070、T085、T091、T094 | 双层上限、liquidityDelta 反算和两边最坏敞口测试 |
| `G-EXIT-01` | T070、T081、T085、T091、T092、T095 | 收回执行钱包、撤权、禁止自动外转的演练与交易证据 |
| `G-RECON-01` | T033、T071、T072、T081、T091、T092、T095 | 重启、RPC、nonce、replacement、reorg 与账本核对测试 |

## 规格到任务

| 规格 | 主要任务 |
| --- | --- |
| `ADM-TECH-*`、`ADM-RISK-*`、`ADM-PAIR-*`、`ADM-HOOK-*` | T023、T025、T043、T049、T070、T084、T085、T094 |
| `WEB-GLOBAL-*`、`WEB-PAGE-*` | T084–T086 |
| `ECO-*` | T049、T050–T053、T060–T066、T070 |
| `CTRL-*` | T070、T080、T081、T085、T090–T095 |

## 更新要求

- 新增、删除或改变 `G-*`、`ADM-*`、`WEB-*`、`ECO-*`、`CTRL-*` 时同步本文件。
- 调整 TODO 任务编号、依赖或职责时同步映射，但不得借此改变目标含义。
- 每个任务完成后只链接真实存在的测试、manifest、报告或链上证据；没有证据就
  保持目标未完成。
