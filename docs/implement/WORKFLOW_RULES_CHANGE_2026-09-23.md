# Bootstrap 动件：状态图完备性——堵死三处断头路

- 起草时间：2026-09-23
- 触发事件：Owner 要求从第一性原理重估 `task.status` 的状态模型，并明确要求
  「不要出现流程的断头路，或者是完全无法改正、进行不下去的路线」，且指出这**与每个
  Agent 的权限有关**，要求把流程转成 graph 来校验
- 变更类型：bootstrap act（`tools/`、`todo/schemas/` 之外的角色与层均无权修改）
- 实现者：Owner 指定的会话；**不由 Manager 会话顺手完成**
- 状态：**增量 1 已落地**（堵断头路）。增量 2–4（状态分解）尚未实施，见第 6 节

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

## 7. 仍待实施

- **增量 3**：路由改用派生事实，`status` 仍存为兼容投影。
- **增量 4**：新修订只存 `lifecycle`；`READY`/`IN_DEVELOPMENT` 改由显式的车道声明承载。

三条状态枚举副本已消除两条：`test_progress.py` 改为**与控制器表断言相等**，`PLAN_STATES`
不再是一份可漂移的手抄本。`tools.progress` **不**导入控制器——它是产品工具，保持独立，
等价性在测试中断言。

---

## 8. 验证

```
PYTHONPATH=src python -m pytest tests/test_workflow.py tests/test_workflow_state_graph.py \
  tests/test_workflow_state_derivation.py tests/test_progress.py tests/test_workflow_contracts.py
python -m ruff format --check . && python -m ruff check .
PYTHONPATH=src python -m mypy tools tests
```

`tests/test_workflow_state_graph.py` 把图校验固化：状态可达性、每非终态可达终态、每非终态有
Owner 出口、车道占用者可释放车道，以及**从 `.claude/agents/*.md` 解析权限**后逐状态核对所需
角色是否够得着（并断言检查者**没有** `Edit`、生产者**不能** `Agent`、Manager **不能**实现或
写 handoff）。权限表不硬编码，删掉某个定义里的能力会在这里失败，而不是在运行中途才发现。

`tests/test_workflow_state_derivation.py` 把等价性判定固化：遍历全部 280 个历史修订，逐个
任务比对推导值与记录值，并把唯一的例外钉成具名常量。同文件还单测了每条推导规则、构件累积时
「最新事件胜出」的顺序，以及非法取值必须响亮失败而不是悄悄推出一个错值。

**一处单测发现的真实缺陷**：初版 `derive_status` 不认识 planner 的 `NO_CHANGE_REQUIRED`
outcome（现行 `finish_plan` 会把它推进到 `AWAITING_PLAN_REVIEW`），历史遍历直接把它暴露为
`WorkflowError`。

## 9. 与在途工作的关系

本次不触碰任何任务状态、不迁移任何任务、不改写任何历史提交。`todo/config.yaml` 中 T109 及其
A0026 影响链（T073/T084/T087/T088/T096/T103/T107/T108/T110/T111 的 10 条开放 impact）与本次
变更完全不相交。
