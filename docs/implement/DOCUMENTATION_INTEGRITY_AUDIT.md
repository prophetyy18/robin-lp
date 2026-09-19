# 文档与代码一致性审计（2026-09-19）

状态：审计发现与处置记录，由 Owner direction A0008 驱动，并已并入 Owner 2026-09-19
对 A0008 所提 9 条问题的逐条裁定（见第 2 节与第 5 节）。
本文件不是验收证据：任何任务的完成仍由独立 Reviewer 对 exact candidate commit 判定。
本文件不定义产品意图，也不新增任务义务；未修的项与已排期的后续事项见第 4、5 节。

## 来源与方法

- Owner direction：A0008（2026-09-19，记录于 `todo/amendments/A0008/`）。
- 审计时的仓库基准：本 worktree 的 base commit 与 main HEAD 均为 `02c8f16`。
- 读取日期：2026-09-19。
- 方法：只读。`git log` / `git diff --name-status` / `grep` / 只读解析脚本；
  `/home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m tools.workflow validate`
  返回 `status: OK`（69 个任务）。本文件的每个数字都在写就时从仓库重新计算，
  没有沿用 direction 或旧文中的数字。

## 1. `ARCHITECTURE.md` §2.2 模块路径与层归属

已验收任务的模块路径写成了仓库中不存在的模块。逐条用该任务既有 candidate commit 的
`git diff --name-status <base> <candidate> -- src` 与仓库文件清单核对后修正。
下表的「原路径」列按纯文本写出，故意不用代码字体：这些名字已经不存在，一旦被写成
引用，就会把本记录自己变成 T005 要报告的缺陷。

| 任务 | 原路径（不存在） | 修正为 |
| --- | --- | --- |
| T013 | robinhood_lp.protocol.vectors | `tests/test_oracle_drift.py`、`tools/reviewers/allowed_signers` |
| T025 | robinhood_lp.admission.lifecycle | `robinhood_lp.discovery.asset_admission` |
| T031 | robinhood_lp.storage.raw | `robinhood_lp.storage.{partition,manifest,reader,writer,measurement}` |
| T032 | robinhood_lp.application.ingest | `robinhood_lp.ingestion` |
| T034 | robinhood_lp.storage.quality | `robinhood_lp.quality` |
| T040 | robinhood_lp.replay.engine | `robinhood_lp.replay.replayer` |
| T042 | robinhood_lp.replay.validate | `robinhood_lp.replay.state_comparison` |
| T043 | robinhood_lp.replay.evidence | `robinhood_lp.qualification.hook_pack` |
| T062 | robinhood_lp.backtest.baselines | `robinhood_lp.strategy.baselines` |

上表 9 行是 Owner direction 点名的错误路径。原表里其余已验收任务的行本来就指向真实模块
（T010–T012、T020–T024、T026、T027、T030、T033、T039、T041、T050–T053、T060、T061），
未改动；未验收任务的行是计划路径（见第 4 节第 5 条）。补充的行：
T014、T015、T035、T036、T037、T038（并标注它已由 T039 取代）、T049，
以及 qualification 包（T036 / T038 / T043 三行）。

层的归属按 §2.1 既有词汇给出，这 9 行原有的层除 T043 外未改；qualification 包
映射到既有的 `presentation / reports`（§2.1 该层拥有 "release evidence"）。该包在代码里
自称 "the qualification layer per ADR-006"（`src/robinhood_lp/qualification/hook_pack.py:27`），
但 ADR-006 只列举十层，§2.1 / §2.2 都没有这一层，因此 §2.2 表下写明了这个映射，差异本身
列在第 4 节第 1 条。

§2.2 表头由 `Module (planned)` 改为 `Module / artifact`，并写明：`APPROVED` 任务的行必须
指向仓库中真实存在的路径，未 `APPROVED` 任务的行是可能尚不存在的计划路径。

## 2. 其它已修正的文档错误

| 文档 | 缺陷（改正前） | 处置 |
| --- | --- | --- |
| `AGENTS.md` §2.1 | 把 `tools/workflow/`、`.claude/`、`todo/schemas/`、`.github/`、`src/`、`tests/` 与依赖清单一起说成「不在任何角色或层的范围内，只能由显式 bootstrap 动作变更」 | 改为三句按范围陈述的真话：前三个路径任何角色与任何层都不得改；依赖清单（`pyproject.toml`、`requirements.in`、`requirements.lock.txt`）不在任何修正层或维护通道的范围内、只有任务合同明确要求时才由该任务的 Developer 修改；`src/`、`tests/`、`.github/` 是普通开发任务的工作面，维护通道只能改它预声明的具体路径、不得触碰 `.github/` 或 execution、risk、signer 代码（`todo/maintenance/M0002` 确实改过 `src/robinhood_lp/storage/reader.py` 与 `tests/test_storage_reader.py`，所以维护通道对 `src/`、`tests/` 只能按路径白名单限制） |
| `docs/spec/README.md` | 声称规格缺陷会产生 `SPEC_BLOCKED` 状态 | 改为真实机制：以 `TRIAGE_REQUIRED` 上报、由独立 triager 分类为 `SPEC_DEFECT`、经 `PLANNING` 由新 Planner 修正并由独立 Plan Reviewer 批准后继续 |
| `ADR-008` 优先级阶梯 | 只列到「`todo/README.md` → `THREAT_MODEL.md` → `ARCHITECTURE.md`/ADR → 源码」，没有任务合同层 | 在 `todo/README.md` 与 `THREAT_MODEL.md` 之后、`ARCHITECTURE.md`/ADR 之前插入「当前任务合同」层，并写明它只能收紧、不能放松更高层设定的 `Must not`、验收、安全、风险、晋级与精度规则 |
| `README.md`、`CLAUDE.md` | 把第二个 Owner 指定池说成「resolved on chain by T038」，而 T038 已由 T039 取代 | 改为 T039 |
| `ARCHITECTURE.md` §2.2、§5 | T022 / T023 行与 §5 的「verified hook source and semantics …（T023, T043）」把已退役任务写成现行归属 | T022 行标注 retired; superseded by T026，T023 行标注 retired; superseded by T027，§5 改为 (T027, T043) |
| `P00` / `P01` phase README | 任务表与 `todo/config.yaml` 不一致：P00 缺 T015，P01 缺 T014 | 补齐；P00 同时列入本次新建的 T005、T006 |
| `CLAUDE.md:61-63` | 依赖清单「in no route's scope and change only by an explicit bootstrap act」，与实测不符 | 改为与 `AGENTS.md` 一致的三句表述：前三个路径 bootstrap 专有；依赖清单不在任何修正层或维护通道范围内、只有任务合同明确要求时才由该任务的 Developer 修改；`src/`、`tests/`、`.github/` 是普通开发任务工作面（维护通道只能改它预声明的具体路径，不得触碰 `.github/` 或 execution、risk、signer 代码） |
| `ADR-008:78` | 用不存在的文件名 `threat-model.md` 指 `docs/spec/security/THREAT_MODEL.md` | 改为完整路径 `docs/spec/security/THREAT_MODEL.md` |

`README.md` 与 `CLAUDE.md` 的 T038 引用是 Owner direction 只点名 README.md 的同类缺陷；
ARCHITECTURE 的两行退役标注与 §5 的 T023 同属「把退役任务当作现行归属」这一类。四处都
按同一条可核对规则处置：只改把退役任务写成现行归属的位置，历史性引用（合同与 ADR 的
References、说明取代关系的正文）不动，见第 4 节第 4 条。

Owner 于 2026-09-19 裁定「全部一并修正」：接受上述同类扩展，并追加两处同规则修正
（`CLAUDE.md` 的依赖清单措辞、`ADR-008` 的 `threat-model.md` 文件名），见上表末两行。
同一裁定还要求把 `AGENTS.md` 第一句的维护通道部分写准，因此两处都按可核对的
路径白名单表述（维护记录里预声明的具体路径）。

## 3. 机制缺口：本次新增的两个任务

改正前的仓库没有任何机器检查覆盖上述类别：

- `tests/test_workflow_contracts.py` 只禁止一组旧布局的路径字面量；
- `tests/test_documentation_links.py` 只解析 markdown 相对链接；
- ADR-006 写有 "CI runs a `depend` / `import-linter` style check"，但仓库中不存在该检查：
  `.github/workflows/ci.yml` 的 job 只有 quality、lockfile-integrity、network-tests。

因此新建两个 P00 任务（`PLANNED`，依赖 T003，均无实现尝试）：

- **T005** — 引用解析：模块路径、合格符号、需求编号、任务编号，以及 phase README 任务表
  与 `todo/config.yaml` 的一致；
- **T006** — 层依赖图检查（ADR-006 承诺的那一个）与 §2.2 映射一致性。

两个任务的合同在 `todo/phases/P00-engineering-baseline/`，`todo/config.yaml` 中对应条目。

## 4. 已发现、本次未修，记录在案的残留

1. `src/robinhood_lp/qualification/hook_pack.py:27` 的 "qualification layer per ADR-006"
   与本仓库的 ADR-006 十层不一致（`src/` 不在本层可写范围）。已裁定为后续事项，见第 5 节
   第 1、2 条。
2. `ADR-009:53-56` 引用两个已被删除的 protocol float helper（该 ADR 自身说明它们必须被删除），
   属历史引用；`docs/spec/` 只有 PROPHET 能改，因此由 T005 的有期限例外条目覆盖，而不是
   去改 ADR-009。
3. `.claude/agents/planner.md:78-81` 仍写 `CONTRACT` / `SPEC` 两个层与「multiple
   still-`PLANNED` task contracts」（A0007 已记录；`.claude/` 不在任何角色的范围内）。
4. 历史性引用退役任务、语义正确、本次未改的位置：`ADR-005:57,119`、
   `ADR-013:8,108`、`ADR-014:8,114-115`、`ADR-015:75,102`、
   `docs/spec/security/THREAT_MODEL.md:135,138,143`、
   `docs/spec/protocol/PROVIDER_FACTS.md:96,161-162`，以及 P02 / P03 phase README、
   T026 / T027 / T039 / T040 / T041 / T042 / T043 合同中说明取代关系的正文。
5. §2.2 中 17 个计划路径确实尚不存在（T063–T066、T070–T072、T080–T086、T100–T104 的行）；
   这是计划路径，T005 的规则把它记为 planned，不判为缺陷。
6. T005 的边界：不检查普通文件名与普通文件路径引用。实测该规则会在仓库中产生大量
   误报——外部来源路径（如 v4-core 的 `src/libraries/Hooks.sol`）、声明为未来工作的
   文件、以及彼此相对的链接写法；markdown 链接已由 `tests/test_documentation_links.py`
   覆盖，引用退役任务的规则由 T005 按「现行归属」范围限定。
7. `ARCHITECTURE.md` §4 称 "Each ADR is immutable once accepted"，而 ADR-008 自身写明
   优先级规则 "can be amended by editing ADR-008 alone"；本次按 Owner direction 就地修改
   ADR-008，但未在两处加写例外条款。已裁定为后续事项，见第 5 节第 3 条。

## 5. 已排期的后续事项（Owner 2026-09-19 裁定）

Owner 对 A0008 的 9 条问题逐条裁定，其中两项按「先记账、后续单独变更」处理，一项是激活
时序，均不在本变更内执行：

1. **设立 qualification 层。** 代码自称 "the qualification layer per ADR-006"，而 ADR-006
   只列举十层。本次不新增 ADR、不改 ADR-006 的十层图，保留 §2.2 把该包映射到
   `presentation / reports` 与 T006 的有期限例外条目；正式设立该层需要一次单独的 ADR 变更。
2. **`robinhood_lp.config` 的 platform 层。** ADR-006 的 Migration trigger 要求用新 ADR
   引入「所有层都可以依赖的 platform 层」，本次同样只在 T006 里以有理由、有期限的例外声明，
   不自行发明一层；正式设立需要一次单独的 ADR 变更。
3. **ADR-008 就地修改与 `ARCHITECTURE.md` §4 的关系。** 本次按 Owner direction 就地修改
   ADR-008 的优先级阶梯，但不在 ADR-008 或 §4 加写「优先级阶梯属可原地修订的例外」；该说明
   留待一次单独变更。
4. **T005 / T006 的激活时序。** 两个任务保持 P00、`PLANNED`、依赖 T003 不变；Owner 决定
   这两道检查在 P06 收尾后才激活。本条只记录在这里，不写进任务合同。

同一裁定对 9 条问题的其它处置：Q1 / Q2 / Q6 为「全部一并修正」，已并入第 2 节（含追加的
`CLAUDE.md` 依赖清单措辞与 `ADR-008` 的 `threat-model.md` 文件名两处）；Q5（ADR-009 历史
符号由 T005 例外条目覆盖）、Q8（§2.2 中 T015 记为 none (repository test)）、Q9
（`spec_revision` 提升为 v1-2026-09-19-documentation-integrity、`intent_revision` 未改）
按现状接受，无后续动作。

## 6. 复核用命令（只读）

```bash
git log --oneline -1
git diff --name-status <base> <candidate>          # 各任务候选提交的真实交付文件
python -m tools.workflow validate                  # 计划有效性
grep -rn "superseded_by" todo/config.yaml          # 退役任务与继任者
```
