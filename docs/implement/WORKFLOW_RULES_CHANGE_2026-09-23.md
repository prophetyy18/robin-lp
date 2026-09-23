# Bootstrap 动件：状态图完备性——堵死三处断头路

- 起草时间：2026-09-23
- 触发事件：Owner 要求从第一性原理重估 `task.status` 的状态模型，并明确要求
  「不要出现流程的断头路，或者是完全无法改正、进行不下去的路线」，且指出这**与每个
  Agent 的权限有关**，要求把流程转成 graph 来校验
- 变更类型：bootstrap act（`tools/`、`todo/schemas/` 之外的角色与层均无权修改）
- 实现者：Owner 指定的会话；**不由 Manager 会话顺手完成**
- 状态：**增量 1、1b、2、3、4a 已落地**。增量 4b（守卫迁移）尚未实施，见第 10 节
- 文档范围：本文档记录同日推进的多个增量。第 2–5 节是最初增量 1 的目标与边界（其中
  「目标 4」「非目标」只对该增量成立），第 6–9 节按落地顺序记录后续增量，第 10 节是唯一
  尚未实施的部分

---

## 1. 事实（图校验实测，不是推测）

把「状态机 + 每条命令的守卫条件 + 每个 Agent 的权限」一起建模后逐项检查，当前模型存在
三处真实断头路：

**1.1 `ESCALATED` 无法到达任何终态**

maintenance Developer 报告 `TRIAGE_REQUIRED` 时写入 `ESCALATED`（`core.py:2455`）。
而 `prepare_maintenance_retry` 只接受 `CHANGES_REQUESTED` / `BLOCKED`（`core.py:2561`），
`status()` 与 `prepare_maintenance` 的活动判定都只列
`IN_DEVELOPMENT / AWAITING_REVIEW / CHANGES_REQUESTED`。于是这条记录**任何人都关不掉**，
并且因为它不算「活动」，车道还照常放行新的维修——一条死记录长期悬挂。

**1.2 agent 未写 handoff 时 `IN_DEVELOPMENT` 没有出路**

- `finish-develop` 要求 `.workflow/developer-result.json` 存在（`core.py:1331-1332`）；
- `continue-develop` 要求该文件是 `CONTINUATION_REQUIRED`，或显式 `--max-turns-exhausted`
  （`core.py:1412-1416`），而该 flag 只在 Claude Code 确实因硬 turn 上限停住时才被授权；
- 其余每条命令都要求别的状态（`prepare-develop` 要 `READY`、`prepare-review` 要
  `AWAITING_REVIEW`、`prepare-triage` 要 `TRIAGE_REQUIRED`、`prepare-retry` 要
  `CHANGES_REQUESTED`/`BLOCKED`）。

因此 agent 因崩溃/网络/权限被拒而中止时，工作项与「一次一个活动工作」的车道被一起卡死。
今天只靠人工越权手术存活——`workflow-attempt-recovery` 记录的那次「工作树被 reset、
逐字节重建配置并用 `<T>-protected.json` 校验」正是这种手术。

**1.3 非 `PLANNED`/非 `APPROVED` 的任务无法被任何路线退役**

`SUPERSEDE` 要求目标是 `APPROVED`（`core.py:1658`），`CONTRACT`/`SPEC` 要求 `PLANNED`，
删除被 `_prophet_path_allowed` 一律拒绝（`core.py:578`）。一个卡在 `BLOCKED` /
`TRIAGE_REQUIRED` / `PLANNING*` 的任务既不能前进（那正是失败的东西），也不能关闭。

**1.4 任务车道 12 个状态里 10 个没有人工授权出口**——只有 `OWNER_DECISION_REQUIRED`
能由 Owner 打开，其余状态的唯一出路都是机械前进。

**1.5 旧的不变量在结构上否认了合法死胡同**

`tests/test_workflow.py` 原先断言「除 `APPROVED` 外每个状态都能到达 `APPROVED`」。
这条不变量本身排除了「合法地不交付而结束」，正是它让 1.1–1.3 无法被表达。

---

## 2. 目标 / 非目标

**目标**

1. 让每个非终态都有**不依赖被阻塞者行动**的出口。
2. 让「一次一个活动工作」的车道**总能被释放**。
3. 让每状态所需的角色**确实持有该步所需的权限**，并把这一点变成机械校验。
4. 落点最小：**不改变任何现有迁移语义**，不引入新的状态机。

**非目标（明确不做）**

- 不动 `APPROVED` 的不可变性与 `SUPERSEDE` 注记语义；
- 不放松 `_ensure_clean_main`、受保护路径、生产者/检查者分离、detached 评审环境；
- 本次**不**做状态分解（把 readiness / actor / verdict / blocker 从 `status` 拆出）。
  这是更大的动件，且必须先有无状态写入的派生与历史等价性判定，见第 6 节。

---

## 3. 变更 1：`ABANDONED` 终态与 Owner 出口

`STATES` 增加 `ABANDONED`；`ALLOWED_TRANSITIONS` 中**每个非终态**都增加指向它的边。

- `APPROVED` **没有**出边：已批准工作只由 `SUPERSEDE` 注记退役，绝不由改写状态退役。
  这一点有测试锁死（`ALLOWED_TRANSITIONS["APPROVED"] == set()`）。
- 新增 `python -m tools.workflow abandon-task <T> --reason "..."`：
  - 只写 `todo/config.yaml` 与 `todo/abandoned/<T>.md`，**落在主检出**而不落在任务分支上——
    分支上任何内容都不得进入产品；
  - 分支、工作树、attempt、评审记录**一律保留**，作为「尝试过什么」的证据；
  - 运行时记录（`.git/robinhood-lp-workflow/<T>.json`）在该持久记录写下保留位置**之后**
    才删除，指针不再只存在于不受版本控制的文件里；
  - 状态取**在途工作树里的活状态**（与 `status()` 的解析规则一致），因此记录不会写成上次
    提交的陈旧值；
  - 对 `APPROVED`/`ABANDONED` 一律拒绝；
  - 当仍有**未关闭**任务依赖它时拒绝——`_check_dependencies` 要求依赖为 `APPROVED`，
    这类依赖者将永远无法激活。这个拒绝是**改道而非陷阱**：依赖图是 DAG，按反向依赖序关闭
    必然终止（已在 2,000 个随机 DAG 上验证）。

**一处被图校验抓出的守卫缺陷**：初版用「非 `RESTING_STATES`」判断搁浅，而 `RESTING_STATES`
含 `PLANNED`，于是**计划中**的依赖者会被漏掉。改用 `CLOSED_TASK_STATES`
（`APPROVED` / `ABANDONED`）。

## 4. 变更 2：maintenance 车道的终态

`ESCALATED` 保留原语义（该维修不属于本车道），但新增
`python -m tools.workflow abandon-maintenance <M> --reason "..."`，走
`withdraw-amendment` 的同一形状：理由写入 `todo/maintenance/<id>/withdrawal.md` 并在该分支
提交，记录置 `ABANDONED`，车道随即放行下一条维修。

## 5. 为什么不削弱

- 撤回/放弃**只能由 Owner 发起、必须写明理由**，失败评审与尝试一并入档；一行实现都不放行；
- 分支与工作树保留在 Git 里，`todo/abandoned/<T>.md` 记下保留位置，可追溯性反而比改动前强；
- `APPROVED` 的不可动性、`SUPERSEDE` 的注记语义、`_validate_supersede_candidate` 的全部约束
  均未触碰；
- 「没有死胡同」不再是一条断言，而是**可校验的性质**。

---

## 6. 增量 2 已落地：派生投影与历史等价性判定

`tools/workflow/core.py` 新增 `derive_status()`：给定一次修订中已提交的构件（review、triage、
owner-decision、planner/developer evidence），返回该修订**蕴含**的状态；**不读** `status`
本身，**不读** `.git/` 下的运行时记录（那些在提交修订里根本不存在，这正是等价性可证明的前提）。

**实测结果（全量，不是抽样）**：扫描 `todo/config.yaml` 的全部 **280 个历史修订**，共
**18,493 个任务·修订**。

| 类别 | 数量 |
| --- | --- |
| 精确复现 | 9,124 |
| 控制面值（无构件可区分） | 9,368 → `PLANNED` 9,308、`READY` 59、`IN_DEVELOPMENT` 1 |
| 无法复现 | **1** |

除 `PLANNED` 外，9,185 个记录值里 9,124 个精确复现、60 个是控制面值、**1 个**例外。

**那 1 个例外是历史语义漂移，且必须保留。** `63cfb2cc`（2026-09-15）记录 T001 为 `BLOCKED`，
而 review 的 verdict 是 `FAIL`。当时的 controller 是
`if verdict == "BLOCKED" or "UNKNOWN" in statuses or unknowns: return "BLOCKED"`——那次 review
带着未解决的 unknowns，所以 `BLOCKED` 在当时语义下是**正确**的值。现行语义只把
`verdict == "BLOCKED"` 映射为 BLOCKED。把它「修好」等于改写历史，所以它被作为具名例外钉在
`tests/test_workflow_state_derivation.py:PRE_SEMANTICS_EXCEPTIONS` 里：多出一条就是新缺陷，
少一条说明例外表过期。

**由此得到的模型结论（有数据支撑，不再是推断）：**

- 12 个状态里 **8 个完全可从构件推导**（`APPROVED`、`AWAITING_REVIEW`、`CHANGES_REQUESTED`、
  `TRIAGE_REQUIRED`、`PLANNING`、`AWAITING_PLAN_REVIEW`、`OWNER_DECISION_REQUIRED`、`BLOCKED`）；
- **2 个是控制面事实**，任何已提交构件都区分不出：`READY`（Manager 选中）与 `IN_DEVELOPMENT`
  （attempt 在跑）——它们与 `PLANNED` 的区别只是一个不留痕迹的决定；
- 这恰好对应目标模型里仅有的两个**显式写入**字段（`lifecycle` 与 `active_work` claim），
  其余全部派生。

**`IN_DEVELOPMENT` 不是持久状态，已被测量证实**：全历史被提交过**恰好 1 次**，且那一次是手工
bootstrap 提交（`d697dd7`）。`test_history_records_in_development_exactly_once` 把这个 SHA
钉住：若将来 controller 开始提交这个值，测试会直接说出来。

## 7. 增量 1b 已落地：丢失的 attempt

**实测发现**：`.git/robinhood-lp-workflow/` 下六条运行时记录的**工作树全部已消失**。
A0022–A0026 是终态记录（按设计保留、不阻塞任何东西，无害）；`T015` 是唯一的任务车道记录，
而它是**活的死胡同**：

```
load_attempt('T015')     -> True
worktree 目录存在         -> False
prepare_develop('T015')  -> "runtime record already exists"   （永久拒绝）
```

今天无害（T015 已 `APPROVED`），但若该任务处于 `PLANNED`/`READY`，它就再也无法开工；由于
「已启动的任务占用唯一活动车道」是既定不变量，**整个计划都会停住**。`T015` 的记录来自旧版
controller（现行 `_record_review_result` 在 APPROVED 时会删除记录），所以这类遗留物也包括
历史版本残留。

`discard-attempt <T> --reason "..."`：把损耗的 attempt 记为证据
（`todo/evidence/<phase>/<T>/attempt-NNN-lost.json`），删除不可用的运行时记录，并把
`attempt` 提升到至少等于丢失的那次，使**已消耗的编号永不复用**。`<T>-protected.json` 刻意
保留——逐字节重建的恢复流程要读它。`status` 新增 `orphaned_attempts`，让这类记录在卡住任何
人之前就可见。

**它不改状态，因此不放宽状态表。** 任务分支只有经过批准才进入主检出，所以 attempt 在途时
主检出只可能显示 `PLANNED` 或 `READY`，两者都不需要迁移边。其他**未终结**状态一律拒绝并指向
`abandon-task`（该路线在每个状态都可达）——拒绝是**改道而非陷阱**，这一点有测试锁死
（`test_discard_attempt_redirects_instead_of_trapping` 在拒绝之后真的用 `abandon-task` 走通）。

**已终结任务（`APPROVED`/`ABANDONED`）也受理，但不递增编号。** 这是初版的一个缺口：终结任务
永不再开发，残留无所谓「阻塞」，但如果清不掉，它会永远留在 `status` 里，让诊断变成狼来了。
而且 `APPROVED` 任务的 `attempt` 属于评审者检查过的内容，清残留时不得改动它——`git diff`
级别的测试断言 `attempt`/`approved_commit`/`candidate_commit` 三者逐字节不变。

守卫因此按「**是否存在路线**」判定，而不是按枚举的状态集合：终结态无需路线，`PLANNED`/`READY`
需要「重开」这条路线，其余状态则指向一条确实可达的路线。

## 8. 增量 3 已落地：记录的状态必须有证据支撑

**动手前才想通的一件事，它缩小了这一步能做的范围。** 原计划「路由只读派生事实」在
`READY`/`IN_DEVELOPMENT` 上是循环的：`derive_status` 对这两个值只能返回标记（`UNSTARTED` /
`IN_FLIGHT`），因为**「Manager 选中了这个任务」这个事实在仓库里没有任何持久归属**——`ready`
只写 `config.yaml`，不留构件、不留运行时记录。要读派生值就得先有车道声明，要有车道声明就得
改 schema（增量 4）。所以纯路由迁移**做不到**，强行做只会把循环藏起来。

因此本增量按它**能真正交付**的形态落地：

**`status_conflicts(config, root)`** — 拿 `root` 那棵树里已提交的构件去校验每个任务记录的状态。
`validate_repository` 对冲突**直接拒绝**；`status()` 报告在 `status_conflicts` 字段里。

它拦的正是仓库自己的痛点：T026/T027/T039/T100–T104 是**手改 config** 加进去的，作者角色和
评审都不存在，当时没有任何东西能发现状态被写成了别的东西。

**豁免只有三个值**（`PLANNED`/`READY`/`IN_DEVELOPMENT`），而且是有界的：哪个可采纳仍取决于
是否已启动 attempt——`attempt == 0` 时只接受 `PLANNED`/`READY`，`attempt > 0` 且该 attempt
没有任何构件时只接受 `READY`/`IN_DEVELOPMENT`。测试把这条边界逐项钉住。

**接入前先测真仓库**：87 个任务 0 冲突，才把它接成门禁。

**顺带补上的真实缺口**：`derive_status` 初版不认识 `ABANDONED`——而 `abandon_task` 写的
`todo/abandoned/<T>.md` **本身就是一件构件**，所以它必须可推导，否则一致性检查会在每个被放弃
的任务上报冲突。

**测试夹具的两处失真也一并修了**：`_make_repo` 只造了 7 个 agent 定义（`validate_repository`
要 8 个）和 6 个 schema（要 8 个），因此 `validate_repository` 在夹具上**根本跑不起来**。

### 为什么这不是「路由改用派生事实」

因为那一步需要先给车道事实一个持久归属。本增量让 controller **校验并报告**不一致，但判定
仍读 `status`。真正迁移路由的判据仍然是 golden 等价测试：对每一次 `prepare-*`/`finish-*`
的接受与拒绝，新判定必须与旧判定完全相同。

## 9. 增量 4a 已落地：把 status 藏起来的两件事写出来

**动手前的推演缩小了范围。** 原以为需要「车道声明」这个复合对象，推演后发现路由真正需要的
只有**一个**新事实：

| 现在混在 `status` 里的控制面事实 | 路由真的需要吗 |
| --- | --- |
| `PLANNED` vs `READY`（是否被选中） | **需要**，且无其他归属 → 新增 `claimed` |
| `READY` vs `IN_DEVELOPMENT`（是否在跑） | **不需要**：`continue_develop` 靠运行时记录的存在判断，其余守卫只看「当前 attempt 有没有封存候选」——可派生 |

于是每个任务记录新增两个字段：

- `lifecycle`：`OPEN` / `DELIVERED` / `ABANDONED`——任务自身的状态；
- `claimed`：是否占用唯一活动车道。

`status` 变成「这两个事实 + 已提交构件」的**投影**。`admitted_statuses()` 是投影本身（返回
可采纳集合，因为有一处真的未定），`LIFECYCLE_OF` 是它的逆（把 status 分解回两个事实），
`_set_state` 三者一起写，因此不可能各自漂移。

### 两条被强制而不是被假设的性质

1. **配置自相矛盾即拒绝。** 同一事实存两遍只在「不一致不可能发生」时才是安全的。手改
   `status` 而不改分解、或改分解而不改 `status`，`validate_config` 直接拒绝并指出两边各是什么。
   这不是空话——**迁移时它精确地抓出了 62 处**测试里改了 status 却没改分解的地方。
2. **`lifecycle` 不能自证。** 初版 `admitted_statuses` 让 `lifecycle=DELIVERED` 单独就承认
   `APPROVED`——而交付是**证据声明**。现在 `DELIVERED` 只有在 `approved_commit` 与评审结论
   都在场时才被承认，`ABANDONED` 同理。**这个漏洞是被我自己的表驱动测试当场抓出来的。**

### 唯一仍然未定的一处

一个 claimed 且尚无任何构件的任务，究竟是「已封存待审」还是「仍在开发」——**没有任何已提交
构件能区分**，因为这是会话事实。工作流也不需要这个区别（controller 从自己的运行时记录就知道
attempt 是否在跑），所以字段不再假装知道。

### 迁移是纯新增

`todo/config.yaml` 的回填是 **174 行新增、0 行删除**（87 个任务 × 2 个字段），没有任何既有
行被改写，`status` 的历史取值一个都没动。这是「历史不可改写」在字段级的一次执行。

## 10. 仍待实施：增量 4b

- **守卫迁移**：`prepare-*` / `finish-*` 的判定改读 `lifecycle` / `claimed` 与已提交构件派生出
  的状态；`status` 保留为兼容投影，新修订不再写它。
- **判据**：golden 等价测试——对每一条 `prepare-*` / `finish-*` 的接受与拒绝，新判定必须与旧
  判定逐例相同。这条判据在增量 3 就已经写明（第 8 节），当时缺的是它依赖的持久归属，而不是
  判据本身。
- 增量 3 曾把这一步写成「路由改用派生事实」却无法实施：`READY` / `IN_DEVELOPMENT` 在仓库里没有
  任何持久归属，纯路由迁移是循环的（第 8 节）。**该归属已由增量 4a 的 `claimed` 补上**，所以
  4b 现在可做；第 9 节末尾那处仍未定的情形（claimed 且尚无构件时无法区分「已封存待审」与
  「仍在开发」）只影响显示，`continue_develop` 靠运行时记录判断，不阻塞 4b。

三条状态枚举副本已消除两条：`test_progress.py` 改为**与控制器表断言相等**，`PLAN_STATES`
不再是一份可漂移的手抄本。`tools.progress` **不**导入控制器——它是产品工具，保持独立，
等价性在测试中断言。

---

## 11. 验证

```
PYTHONPATH=src python -m pytest tests/test_workflow.py tests/test_workflow_state_graph.py \
  tests/test_workflow_state_derivation.py tests/test_progress.py tests/test_workflow_contracts.py
python -m ruff format --check . && python -m ruff check .
PYTHONPATH=src python -m mypy tools
```

**实测**：129 个测试通过；`ruff check .` 通过；`mypy tools` 31 个源文件通过。`ruff format --check .`
与 `mypy src tests` 在仓库级是红的，原因见下（两条都与本动件无关，且都早于本动件的任何提交）。

- `ruff format --check .` 报 4 个文件：`src/robinhood_lp/__main__.py`（更早的 T069 候选引入）、
  `src/robinhood_lp/backtest/engine.py`、`src/robinhood_lp/protocol/contracts.py`、
  `tests/test_t109_acceptance.py`。后三个在父提交 `791fc4e` 上仍是格式化状态，由 T109 的候选
  `c79e4c8` 引入并通过了评审：`review-004` 的 `ruff check`（lint）通过，但格式门禁只在报告中
  把 `__main__.py:220` 记为既存项。
- `mypy src tests` / `mypy tools tests` 报 `tests/_storage_t031_fixtures.py` 的 duplicate-module
  错误：`tests/test_storage_block_headers.py:380` 写 `from tests._storage_t031_fixtures import ...`，
  其余文件写 `from _storage_t031_fixtures import ...`，自 2026-09-17 的 T035 起存在。

因此上面第三行**不能**照抄成 `mypy tools tests`：那一行今天不通过，且失败原因不属于本动件。

`tests/test_workflow_state_graph.py` 把图校验固化：状态可达性、每非终态可达终态、每非终态有
Owner 出口、车道占用者可释放车道，以及**从 `.claude/agents/*.md` 解析权限**后逐状态核对所需
角色是否够得着（并断言检查者**没有** `Edit`、生产者**不能** `Agent`、Manager **不能**实现或
写 handoff）。权限表不硬编码，删掉某个定义里的能力会在这里失败，而不是在运行中途才发现。

`tests/test_workflow_state_derivation.py` 把等价性判定固化：遍历全部 280 个历史修订，逐个
任务比对推导值与记录值，并把唯一的例外钉成具名常量。同文件还单测了每条推导规则、构件累积时
「最新事件胜出」的顺序，以及非法取值必须响亮失败而不是悄悄推出一个错值。

`tests/test_workflow.py` 为丢失的 attempt 覆盖了四种情形：正常（丢弃后可开工、编号递增、
证据落盘）、边界（工作树仍在时拒绝）、非法输入（空理由、无记录）、失败路径（状态不在恢复
可达范围时改道到 `abandon-task`，并在同一测试里真的走通那条路）。

一致性检查的测试：正常（夹具仓库 `validate` 通过）、失败路径（把已批准任务的 status 手改成
`PLANNED`，`status` 报告冲突而 `validate` 直接拒绝）、边界（`PLANNED`/`READY` 在 `attempt == 0`
可采纳、`READY`/`IN_DEVELOPMENT` 在 `attempt > 0` 且无构件时可采纳，其余组合一律报冲突）。

**一处单测发现的真实缺陷**：初版 `derive_status` 不认识 planner 的 `NO_CHANGE_REQUIRED`
outcome（现行 `finish_plan` 会把它推进到 `AWAITING_PLAN_REVIEW`），历史遍历直接把它暴露为
`WorkflowError`。

## 12. 与在途工作的关系

本次不触碰任何任务状态、不迁移任何任务、不改写任何历史提交。`todo/config.yaml` 中 T109 及其
A0026 影响链（T073/T084/T087/T088/T096/T103/T107/T108/T110/T111 的 10 条开放 impact）与本次
变更完全不相交。

唯一对 `todo/config.yaml` 的写入是增量 4a 的回填：87 个任务各新增 `lifecycle` / `claimed`
两个字段（174 行新增、0 行删除），**没有任何 `status` 取值被改写**，T109 与 A0026 影响链的
状态、attempt、候选/批准提交、依赖图均逐字节未变。
