# V1 项目状态 — 2026-09-19

> 本文件是当前工作区的事实快照，不是目标或任务定义。
> 目标以 `docs/intent/PROJECT_GOALS.md` 为准，任务合同以 `todo/README.md` 为准。
> 代码存在不等于任务完成；只有依赖、阶段入口和全部验收均有当前证据时，
> `todo/config.yaml` 才能标记 `APPROVED`。
>
> 事实来源：本文的数字与状态在 2026-09-19 从 `todo/config.yaml`、
> `docs/spec/architecture/adr/` 和仓库文件清单重新读取，基准是本地 `main`
> （HEAD `d22bfa9`）。无法从文件核实的项标为 UNKNOWN。

## 1. 当前结论

项目已经形成 Robinhood Chain / Uniswap V4 的协议身份与整数数学、独立一致性向量、
只读 RPC 适配器与链能力探测、Pool 发现与候选准入、版本化历史采集与追加存储、
重组处理与完整性报告，以及确定性事件回放、tick 流动性重建、与链上状态的对账和
Hook 语义证据包。这些实现对应的 P00–P04 任务全部为 `APPROVED`，最后完成的是 T043。

当前仍不能完成以下用户流程：

- 输入目标 Token 后，通过真实链证据完整发现并审批候选 PoolKey；
- 选择一个活动池并查看经过核对的历史数据；
- 运行可复现回测并查看 USDG 收益与完整 PnL 归因；
- 运行实时信号和 preliminary paper trading；
- 通过 Web 页面进行审批、监控、暂停和接管；
- 在 testnet 或 mainnet 执行 LP/Swap。

这些流程依赖尚未开始的 P05–P09：目前可运行的实现入口只有 Python 包和只读 CLI
（`python -m robinhood_lp ingest`），没有 Web 控制台（P08），也没有策略、风险、
paper 与执行能力（P05–P07、P09）。

因此，当前阶段应描述为：**数据与状态重建基线已经完成，策略、风险、Web 与执行
阶段尚未开始**。P00–P04 的 `APPROVED` 不等于 V1 交付；V1 的完成标准仍是
`G-LIVE-01`，并且只能逐级通过晋级门禁。

## 2. 状态词含义

- **VERIFIED**：依赖和阶段入口满足，全部验收有当前独立审查证据，任务可标
  `APPROVED`。
- **IMPLEMENTED_NOT_VERIFIED**：已有部分或大部分代码，但验收、依赖或证据仍缺失。
- **NOT_IMPLEMENTED**：没有达到该任务 Outcome 的实现。
- **BLOCKED_EXTERNAL**：本地工作可继续，但最终验收还需要 RPC、链上交易、外部
  安全复核或用户批准等外部证据。

## 3. 证据基线

### Git

- 主检出 `/home/lpdev/lp` 位于分支 `main`，HEAD 为 `d22bfa9`（2026-09-19 读取）。
- 文档分层、`todo/` 状态机、独立 Agent 与 worktree 门禁已在 `main` 的历史中；
  Agent 控制面迁移到可见 Claude Code Manager 的工作也已提交（`7fbdf20`，
  2026-09-19 读取 Git 历史）。
- 本地记录的 `origin/main` 为 `0ca34e7b`，落后于本地 `main`。远端真实状态为
  UNKNOWN：本环境未联网核对，remote-tracking 引用可能过期。
- 旧目录下的未提交内容保存在 `todo/evidence/legacy/2026-09-14/`，只作为历史候选
  证据，不参与任何任务的验收。

### 测试与工具

- `tests/` 下有 48 个 `tests/test_*.py` 文件
  （`find tests -maxdepth 1 -name 'test_*.py'`，2026-09-19）。静态文件数不等于用例数：
  pytest 参数化与 fixture 会让实际 case 数不同。
- 当前 pytest 的 passed/skipped 数、Ruff 与 mypy 结果不在本文件记录：没有仓库内
  文件保存这一数字，UNKNOWN。需要时以当前 commit 的交付证据为准
  （`todo/evidence/`、`todo/reviews/`），而不是本快照。
- 系统 shell 的 `python3` 为 3.10.12，项目要求 Python 3.12+；项目环境在
  `/home/lpdev/miniconda3/envs/robinhood-lp`（Python 3.12.14，2026-09-19 读取）。
- 依赖锁定：`requirements.lock.txt` 存在，由
  `pip-compile --extra=dev --generate-hashes` 从 `pyproject.toml` 生成，含 586 条
  `--hash=sha256:` 行（2026-09-19 读取）。
- 仓库中没有 `.pytest_cache/`（2026-09-19 检查）；任何缓存都不是测试通过证据。
- GitHub Actions 的当前结果未能从本环境验证。任何“全部测试通过”声明都必须
  等待可访问的 CI run 或 Python 3.12 clean-room 本地运行。
- Agent runtime 记录在 `todo/config.yaml` 的 `agent_runtime`：MiniMax
  Anthropic-compatible API、`MiniMax-M3[1m]`、禁止 fallback；端点与 Claude Code
  版本见 `todo/WORKFLOW.md`。密钥来自用户级设置，不进入仓库或验证证据。

### 安全表面

- `src/robinhood_lp` 当前没有生产 signer、Keystore 解密或交易广播实现
  （2026-09-19 文件清单）；`tests/test_no_signing_paths.py` 对当前主包执行静态
  禁止项检查。
- 这只证明当前没有 live 能力，不证明未来执行设计已经安全。
- Phase 0–8 继续禁止签名/广播；Phase 9 只允许 T090–T095 明确规定的隔离能力。

## 4. 阶段状态板

| Phase | VERIFIED / total | 已有实现 | 当前判断 |
| --- | ---: | --- | --- |
| 0 | 6 / 6 | T000–T004、T015 | 全部 `APPROVED` |
| 1 | 5 / 5 | T010–T014 | 全部 `APPROVED` |
| 2 | 6 / 8 | T020–T025 已验收；T026、T027 未开工 | T022、T023 已退役，继任者为 T026、T027 |
| 3 | 9 / 10 | T030–T038 已验收；T039 未开工 | T038 已退役，继任者为 T039 |
| 4 | 4 / 4 | T040–T043 | 全部 `APPROVED` |
| 5 | 0 / 5 | 无任务级实现 | T049–T053 全部 `PLANNED` |
| 6 | 0 / 7 | 无任务级实现 | T060–T066 全部 `PLANNED` |
| 7 | 0 / 3 | 无任务级实现 | T070–T072 全部 `PLANNED` |
| 8 | 0 / 7 | 无任务级实现 | T080–T086 全部 `PLANNED` |
| 9 | 0 / 7 | 只有 `RunMode.LIVE` 枚举，不算执行实现 | T090–T096 全部 `PLANNED` |
| 10 | 0 / 5 | 无任务级实现 | T100–T104 全部 `PLANNED` |

统计口径：`VERIFIED` 取 `todo/config.yaml` 中 `status: APPROVED` 的任务数，`total`
取该 phase 下的全部任务（2026-09-19 读取，共 67 个任务：30 个 `APPROVED`、37 个
`PLANNED`）。已退役的 T022、T023、T038（`superseded_by` 分别指向 T026、T027、T039）
仍计入 `APPROVED`。

`todo/config.yaml`（2026-09-19 读取）的 `active_phase` 为 `P04`、`active_task` 为
`T043`、`workflow_state` 为 `APPROVED`：没有正在进行的任务。P04 之后没有阶段开工。

## 5. 已验证内容

### T000 — 架构决策

- 已有 `docs/spec/architecture/ARCHITECTURE.md` 和 ADR-001 至 ADR-015
  （`docs/spec/architecture/adr/`，2026-09-19 读取）；ADR-009 至 ADR-015 分别覆盖
  展示小数边界、免费双 provider 历史采集、项目自有 JSON-RPC transport、block header
  时间持久化与采集调用量、finalized 窗口固定与分区对账、研究范围与计价层级、
  每池扩展历史窗口。
- 文档优先级、依赖方向、配置/秘密、精度、存储和支持生命周期已有明确归属。
- 当前架构已包含 V1 Web、隔离 signer、testnet、正式 paper/shadow 和 mainnet
  canary 的模块映射。

### T004 — 威胁模型与当前无签名边界

- 已有信任边界、威胁、严重度、控制与任务 owner。
- `tests/test_no_signing_paths.py` 对当前主包执行静态禁止项检查。
- 本项只覆盖“当前主包没有签名/广播能力”；T090 后必须以隔离、认证、短时请求和
  testnet/live 证据替换单纯的缺席证明。

## 6. 已实现但不能标完成的任务

当前没有这类任务。

`todo/config.yaml`（2026-09-19 读取）中 P00–P04 的实现都有对应的 `APPROVED` 任务，
其余 37 个任务均为 `PLANNED` 且没有实现 attempt。P02、P03 中仍为 `PLANNED` 的
T026、T027、T039 将复用其已批准前任（T022、T023、T038）的代码；这是
`superseded_by` 记录的退休关系，不是未验收的实现。

本节此前逐条列出的 T001–T030 缺口（lockfile、clean install、T012/T013 向量校验、
T021 artifact、T022 重叠扫描、T024 端到端能力报告、T030 migration 等）已随各任务的
独立复查归档，逐条命令、结果、外部来源与残余风险见 `todo/evidence/` 与
`todo/reviews/`。

## 7. 尚未实现的产品能力

- T026、T027、T039：研究范围的按池入口与分类，以及每池扩展历史窗口（P02、P03
  中已退役任务的继任者）。
- T049–T053：V4 liquidityDelta/USDG sizing、features、position、PnL、合格 USDG quote。
- T060–T066：策略合同、回测、baseline、自适应 Range、实验和 provisional thresholds。
- T070–T072：集中风险、paper ledger、实时数据恢复。
- T080–T086：监控、企业微信通知、状态机、备份、Web 控制台和 owner journeys。
- T090–T096：隔离 signer、交易 planner、testnet、正式 paper/security、人工晋级、
  mainnet canary 和最终验收 dossier。
- T100–T104：研究数据集与计价资格、面板与训练评估、模型作为策略组件、研究控制台
  和 fee-growth surface。

## 8. 外部依赖与阻塞边界

以下仍未验收的工作可以先用 fixture 完成接口和失败测试，但最终验收需要外部证据
（2026-09-19 读取 `todo/config.yaml`，这些任务仍为 `PLANNED`）：

- T053：合格、可追溯且 point-in-time 的 USDG 换算来源；
- T082/T093：预声明 observation window 的 paper/shadow 运行；
- T092：Robinhood testnet Gas/faucet 和真实生命周期交易；
- T093：独立安全复核；
- T094/T095：用户明确批准的 mainnet 范围与资金。

外部证据缺失不允许把任务标 `APPROVED`，但也不阻止任务合同内的本地实现与 fixture
测试。任何无法验证的链、合约、Hook、价格或账本事实必须返回 UNKNOWN、暂停或
阻止晋级，不能猜默认值。

## 9. 正确的下一步顺序

`todo/config.yaml`（2026-09-19 读取）显示 `active_phase` 为 `P04`、`active_task` 为
`T043`、`workflow_state` 为 `APPROVED`：没有正在进行的任务，下一项由 Owner
指定，控制器不会自动选择。

按 `AGENTS.md` 与 `todo/WORKFLOW.md`，Owner 指定一个依赖已齐备的 `PLANNED` 任务后，
Manager 对该任务执行 `ready <task>`；控制器一次只激活一个任务，并且在仍有未完成的
Owner amendment 时拒绝激活。按 `todo/config.yaml` 的依赖字段推导（2026-09-19），
当前满足该依赖规则的候选是 T026、T039、T049、T050、T051；其余 `PLANNED` 任务仍缺
依赖。各阶段的入口条件（`todo/phases/*/README.md` 的 Entry）仍需在开工前逐项确认。

已退役的已批准任务及其继任者（`superseded_by`）为 T022 → T026、T023 → T027、
T038 → T039。`ready` 只允许继任者依赖它取代的任务；其他任务依赖已退役任务会被
拒绝，必须通过 Owner amendment 把依赖改指向继任者。

计划结构、目标与附属文档的修改走 `PROPHET` 层；退役已 `APPROVED` 的工作走
`SUPERSEDE` 层。两条通道都不激活任务、不消耗实现 attempt，并且都必须由独立审查
通过后才能合并（见 `todo/WORKFLOW.md`）。

P04 之后仍未开工的顺序：

1. P05（T049–T053）：point-in-time features、仓位估值与 PnL 归因；
2. P06（T060–T066）：事件驱动回测与研究协议；
3. P07（T070–T072）：集中风险与 paper 执行；
4. P08（T080–T086）：运维、可观测性与发布证据，含 Web 控制台；
5. P09（T090–T096）：隔离 signer、testnet 证明、人工晋级、mainnet canary 与最终
   dossier；
6. P10（T100–T104）：研究数据集、模型实验室与研究控制台；入口是 P06 出口门禁、
   P08 认证控制台，以及至少两个达到 `backtest` 支持级别的池。

每个任务完成时应在交付报告中保存命令、结果、环境版本、fixture/区块、外部来源、
checksum 和残余风险。只有独立审查接受这些证据后，工作流控制器才更新任务状态；
本快照随 `PROPHET` 层或任务内的文档变更更新，它本身不是验收证据。

## 10. V1 最终路径

V1 的正式能力路径为：

```text
verified chain + approved target token/one PoolKey
  -> immutable history + deterministic replay
  -> USDG valuation + complete LP accounting
  -> backtest + provisional strategy parameters
  -> preliminary paper + Web/operations validation
  -> isolated signer/planner on testnet
  -> post-testnet paper/shadow + independent security review
  -> explicit human mainnet promotion
  -> capped mainnet canary lifecycle
  -> final traceability dossier
```

Preliminary paper 可以在 testnet 前运行，但不产生 live 资格。除已确认的 5 分钟
USDG 极端涨跌规则外，其余经济和风险数值只实现可配置、版本化和可审计机制；进入
paper trading 后再用实际证据确定，最终在 T093 锁定并审批，T094 前不得作为 live
默认值。
