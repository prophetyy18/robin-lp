# V1 项目状态 — 2026-09-14

> 本文件是当前工作区的事实快照，不是目标或任务定义。
> 目标以 `docs/intent/PROJECT_GOALS.md` 为准，任务合同以 `todo/README.md` 为准。
> 代码存在不等于任务完成；只有依赖、阶段入口和全部验收均有当前证据时，
> `todo/config.yaml` 才能标记 `APPROVED`。

## 1. 当前结论

项目已经形成 Robinhood Chain / Uniswap V4 的协议、配置、只读 RPC、Pool
Initialize 解码、初步资格分类和数据 schema 原型，但尚未形成可供用户运行的
V1 产品。

当前不能完成以下用户流程：

- 输入目标 Token 后，通过真实链证据完整发现并审批候选 PoolKey；
- 选择一个活动池并查看经过核对的历史数据；
- 运行可复现回测并查看 USDG 收益与完整 PnL 归因；
- 运行实时信号和 preliminary paper trading；
- 通过 Web 页面进行审批、监控、暂停和接管；
- 在 testnet 或 mainnet 执行 LP/Swap。

因此，当前阶段应描述为：**基础协议与发现原型，正在重新完成工程基线验收**。
不应描述为“Phase 0–2 已完成”，也不应直接从 T031 继续开发。

## 2. 状态词含义

- **VERIFIED**：依赖和阶段入口满足，全部验收有当前独立审查证据，任务可标
  `APPROVED`。
- **IMPLEMENTED_NOT_VERIFIED**：已有部分或大部分代码，但验收、依赖或证据仍缺失。
- **NOT_IMPLEMENTED**：没有达到该任务 Outcome 的实现。
- **BLOCKED_EXTERNAL**：本地工作可继续，但最终验收还需要 RPC、链上交易、外部
  安全复核或用户批准等外部证据。

## 3. 证据基线

### Git

- 当前分支：`main`。
- Workflow Bootstrap 的起始版本为 `6c31778`，当时 `origin/main` 指向同一提交。
- 当前工作区包含待提交的 Workflow Bootstrap：Intent/Spec/Implement 文档分层、
  `todo/` 状态机、独立 Agent、worktree 控制器和测试。完成验证并推送前，远端尚不
  包含这套工作流。
- 原 `docs/HANDOVERS/` 未提交内容已原样保存到
  `todo/evidence/legacy/2026-09-14/`，只作为历史候选证据，不批准 T001。

### 测试与工具

- 当前仓库有 13 个 `test_*.py` 文件，不是旧 STATUS 声称的 18 个。
- 静态计数有 230 个显式 test 函数；pytest 参数化后的实际 case 数不能由此推断。
- `.pytest_cache` 记录过 278 个 node id，并仍含两个历史失败条目；缓存不是当前
  测试通过证据。
- 系统 shell 的 Python 仍为 3.10.12；服务器现有
  `/home/lpdev/miniconda3/envs/robinhood-lp`，提供 Python 3.12.14、pytest 9.1.1、
  Ruff 和 mypy。Workflow Bootstrap 完成后已在该环境得到 282 passed、2 skipped，
  Ruff 和 mypy 通过；但 T001 仍缺少仓库内 dependency lock、独立 clean-install 和
  任务合同要求的连续两次完整质量门证据，因此不能据此批准。
- GitHub Actions 的当前结果未能从本环境验证。任何“全部测试通过”声明都必须
  等待可访问的 CI run 或 Python 3.12 clean-room 本地运行。
- Claude Code 2.1.269 已使用用户级密钥经 MiniMax 官方中国区 Anthropic 兼容端点
  `https://api.minimax.cn/anthropic` 完成最小调用；请求配置为
  `MiniMax-M3[1m]`，响应的 canonical model 为 `MiniMax-M3`。项目工作流已禁止
  Developer/Reviewer 降级到其他模型。密钥不进入仓库或验证证据。

### 安全表面

- `src/robinhood_lp` 当前没有生产 signer、Keystore 解密或交易广播实现。
- 这只证明当前没有 live 能力，不证明未来执行设计已经安全。
- Phase 0–8 继续禁止签名/广播；Phase 9 只允许 T090–T095 明确规定的隔离能力。

## 4. 阶段状态板

| Phase | VERIFIED / total | 已有实现 | 当前判断 |
| --- | ---: | --- | --- |
| 0 | 2 / 5 | T000、T001–T004 均有文件 | T000、T004 verified；T001/T002/T003 需补证据 |
| 1 | 0 / 4 | T010–T013 均有代码/fixture | 依赖未闭合，T012/T013 有实质缺口 |
| 2 | 0 / 6 | T020–T024 有部分实现；T025 无 | 真实部署与完整准入未完成 |
| 3 | 0 / 5 | T030 schema 原型 | storage/ingestion/reorg/quality 未实现 |
| 4 | 0 / 4 | 无任务级实现 | replay/tick/StateView/Hook evidence 未实现 |
| 5 | 0 / 5 | 部分底层数学可复用 | sizing/features/valuation/PnL/USDG quote 未实现 |
| 6 | 0 / 7 | 无任务级实现 | strategy/backtest/experiment 未实现 |
| 7 | 0 / 3 | 无任务级实现 | centralized risk/paper/realtime 未实现 |
| 8 | 0 / 7 | CI/文档有可复用部分 | ops/Web/preliminary dossier 未实现 |
| 9 | 0 / 7 | `RunMode.LIVE` 枚举不算执行实现 | signer/planner/testnet/formal paper/live 未实现 |

`todo/config.yaml` 中只有 T000 和 T004 保留 `APPROVED`。T001 为 `READY`；其他已有
代码的任务保持 `PLANNED` 并在任务合同中记录 `Implementation status`，因为依赖或
验收尚未闭合。这不会删除已有实现；它只是停止把实现进度误报为交付完成度。

## 5. 已验证内容

### T000 — 架构决策

- 已有 `docs/spec/architecture/ARCHITECTURE.md` 和 ADR-001 至 ADR-008。
- 文档优先级、依赖方向、配置/秘密、精度、存储和支持生命周期已有明确归属。
- 当前架构已包含 V1 Web、隔离 signer、testnet、正式 paper/shadow 和 mainnet
  canary 的模块映射。

### T004 — 威胁模型与当前无签名边界

- 已有信任边界、威胁、严重度、控制与任务 owner。
- `tests/test_no_signing_paths.py` 对当前主包执行静态禁止项检查。
- 本项只覆盖“当前主包没有签名/广播能力”；T090 后必须以隔离、认证、短时请求和
  testnet/live 证据替换单纯的缺席证明。

## 6. 已实现但不能标完成的任务

### T001 — Python 工程骨架

已有：`pyproject.toml`、`environment.yml`、package/CLI smoke test、CI 命令。

缺口：Python runtime/dev dependencies 没有可验证 lockfile；没有保留 clean install 后完整
质量门连续运行两次的证据；当前环境无法运行 Python 3.12 质量门。

### T002 — 类型化配置

已有：严格 Pydantic 配置、单链/单活动池、完整 PoolKey、目标 Token 审批字段、
secret env-name 引用和测试。

缺口：T001 未完成；需要按当前 V1 目标重新核对权限、Keystore secret reference、
live promotion binding 和错误脱敏接口。

### T003 — CI 与供应链

已有：CI、gitleaks、Trivy、OSV 和 intentional-failure workflow。

缺口：intentional-failure 结果表仍为空；vulnerable-dependency failure 没有受控证明；
Python dependency lock integrity 无法检查；本次无法核实 GitHub Actions 当前 run。

### T010/T011 — 协议身份与事件身份

已有：ChainId、Address、Currency、PoolKey、PoolId、BlockRef、TransactionRef、
EventKey、canonical/orphaned/removed 状态和测试。

缺口：依赖 T001 未闭合，必须在 Python 3.12 完整质量门下重新验证。当前审查未发现
需要推翻这些模型的产品冲突，但这不是完整代码审计结论。

### T012 — V4 数学

已有：TickMath、floor amount0/amount1 delta、liquidity-for-amounts、usable tick 和
Foundry 数学向量。

缺口：协议模块仍暴露 `sqrt_price_x96_to_price(...)->float` 和
`price_to_sqrt_price_x96(float)`，违反协议路径禁止 float；TODO 要求的 exact-in/out rounding、
0/6/8/18/24 decimals、完整 overflow/revert domain 和 bounded-position amount 证据未完整展示。

### T013 — 独立一致性向量

已有：PoolIdOracle、MathOracle、JSON vectors 和来源版本清单。

缺口：manifest 未为所有向量记录可执行校验的 checksum 和逐 edge-class 人工复核证据；
没有保留 clean generator reproduction 结果；T021 artifact 声称使用的 SelectorOracle 不存在。

### T020 — 只读 RPC adapter

已有：方法 allowlist、bounded getLogs、retry/jitter、failover、range split、metrics、
capability probe 和 fake-transport tests。

缺口：依赖 T002/T011 当前未 verified；完整 Python 3.12 质量门未重跑。本任务代码
可以保留并优先复核，不需要因为任务尚未 `APPROVED` 而重写。

### T021 — V4 artifacts

已有：4 个事件 topic、4 个函数 selector、来源 commit JSON 和 loader。

缺口：缺少 StateView 最小 ABI；artifact 引用不存在的 `SelectorOracle.sol`；selector 测试只
检查 4-byte 长度，没有 regenerate-and-compare；artifact SHA-256 是 `manual-annotation`，测试
遇到非 64-hex 时 skip，因此不能证明 ABI/topic/selector drift 会失败。

### T022 — Pool 发现与 registry

已有：Initialize decoder、PoolId 复算、metadata reader、in-memory registry 和测试。

缺口：metadata reader 接受 `block_number` 却忽略它，内部始终调用 block 0；重叠扫描会增加
`occurrences` 并按最后处理顺序覆盖 `block_number_last_seen`；当前测试明确断言两次
重叠扫描结果不同，与“stop/resume/overlap identical”相反；且依赖 T024 尚未完成。

### T023 — Pool/Hook 初步资格分类

已有：fee、metadata、hook address flags、可选 code hash/upgrade flag 的 reason-coded
初步分类。

缺口：deployment evidence、数据覆盖、真实 Hook 行为、modeled BalanceDelta、代码/
代理变化监控与实际 demotion 流程均未闭合。它不是 T025 的 Token/Pool 人工审批系统。

### T024 — 链能力与部署验证

已有：ExpectedDeployment、fake-transport probe 和期望地址 artifact。

缺口：没有被接受的真实 Robinhood mainnet/testnet capability report；artifact 中两个 code
hash 仍为 `null`；report 没有 genesis hash、部署 block/transaction 或真实 range-limit 证据；
archive depth 是由 latest 和参数推算，不是历史调用探测；unsupported safe/finalized tag 被静默
忽略；所谓逐 endpoint 循环调用带 failover 的内部方法，不能证明请求来自标记的 endpoint；
empty bytecode、provider block/hash 分歧和可复现固定区块仍没有完整 fail-closed 证明。

T024 是当前最重要的真实链阻塞项，但必须先完成 T001/T002/T003/T021 的前置修复。

### T030 — Versioned schema

已有：block/transaction/receipt 和 Initialize/ModifyLiquidity/Swap/Donate dataclass，
raw/unknown fields、schema/decode version 和 canonical JSON。

缺口：`from_canonical_bytes` 只返回 dict，不重建 typed record；没有真正的旧 schema version
migration fixture/runner；wall-clock ingestion time 与可复现字段边界需要明确；依赖 T011/T021
尚未 verified。

## 7. 尚未实现的产品能力

- T025：Token、配对 Token、PoolKey、Hook、HOLD/LP/AUTO_SWAP/USDG 敞口审批。
- T031–T034：append-only storage、checkpoint ingestion、reorg、完整性报告。
- T040–T043：确定性 replay、tick liquidity、StateView 对账、Hook evidence pack。
- T049–T053：V4 liquidityDelta/USDG sizing、features、position、PnL、合格 USDG quote。
- T060–T066：策略合同、回测、baseline、自适应 Range、实验和 provisional thresholds。
- T070–T072：集中风险、paper ledger、实时数据恢复。
- T080–T086：监控、企业微信通知、状态机、备份、Web 控制台和 owner journeys。
- T090–T096：隔离 signer、交易 planner、testnet、正式 paper/security、人工晋级、
  mainnet canary 和最终验收 dossier。

## 8. 外部依赖与阻塞边界

以下工作可以先用 fixture 完成接口和失败测试，但最终验收需要外部证据：

- T024：至少两个独立 endpoint 或 endpoint + fixture node，以及真实部署读取；
- T032/T033/T042：archive RPC、固定区块读取与 reorg/finality 行为；
- T043：活动 PoolKey 的 Hook verified source/bytecode/历史交易/模拟；
- T053：合格、可追溯且 point-in-time 的 USDG 换算来源；
- T082/T093：预声明 observation window 的 paper/shadow 运行；
- T092：Robinhood testnet Gas/faucet 和真实生命周期交易；
- T093：独立安全复核；
- T094/T095：用户明确批准的 mainnet 范围与资金。

外部证据缺失不允许把任务标 `APPROVED`，但也不阻止任务合同内的本地实现与 fixture
测试。任何无法验证的链、合约、Hook、价格或账本事实必须返回 UNKNOWN、暂停或
阻止晋级，不能猜默认值。

## 9. 正确的下一步顺序

按照 `AGENTS.md` 的“一次一个编号任务”和依赖规则，下一项不是 T031，而是 T001：

1. 完成 T001：生成并验证 Python dependency lock，clean install，连续两次完整质量门；
2. 复核 T002，再完成 T003 的故障门禁证据；
3. 按 T010 → T011 → T012 → T013 顺序复核，其中 T012/T013 需要实质修复；
4. 复核 T020，完成 T021 的真实 artifact regeneration；
5. 完成 T024 的 endpoint-pinned fixture 行为，再申请真实 RPC 验收；
6. 修复并完成 T022 → T023 → T025；
7. 完成 T030 migration/round-trip 后，才进入 T031。

每个任务完成时应在交付报告中保存命令、结果、环境版本、fixture/区块、外部来源、
checksum 和残余风险。只有独立审查接受这些证据后，工作流控制器才更新状态和
本状态文件。

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
