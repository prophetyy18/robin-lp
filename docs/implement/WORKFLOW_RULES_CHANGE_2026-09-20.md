# Bootstrap 提案：ownership / retirement 规则重述

- 起草时间：2026-09-20
- 触发事件：A0020（退休 T063/T064/T066/T067）被独立评审判 FAIL，原因是「退休」这个动作
  按现行规则**在单层内不可满足**；清理需要一次 Owner bootstrap 动件
  （删运行时记录 + 存档分支 `amendment/a0020-attempt-001-failed`）
- 变更类型：bootstrap act（`tools/`、`todo/WORKFLOW.md`、`todo/schemas/` 之外的角色与层均无权修改）
- 实现者：Owner 指定的会话；**不由 Manager 会话顺手完成**
- 状态：**已落地**（2026-09-20，由 Owner 指定的会话按本提案实现，见 `tools/workflow/core.py`、
  `tools/check_citations/resolvers.py`、`tools/check_imports/architecture.py` 与
  `tests/test_workflow.py`、`tests/test_documentation_citations.py`、`tests/test_import_graph.py`）
- 与提案的差异：**变更 5 未实施**——`todo/WORKFLOW.md` 仍留在 PROPHET 的可写面内。证据是它是
  被真实使用过的：`docs(a0007)`/`docs(a0008)` 两次经由 PROPHET 变更修订过本文件；而规则条文的
  *执行*位于 `tools/`（PROPHET 不可写），所以可写面带来的风险远小于屏蔽正常文档维护的成本。
  若 Owner 仍希望收紧，应作为独立的一次 bootstrap 动件单独评估。
- 未实施的验收：提案第 10 节那条「用新规则重放 A0014→A0020 真实历史」的端到端回放尚未建立；
  现有覆盖为三条单测（后继创建即需注记、关系括注不算引用、注记行与裸行同检）加一条封存门禁
  回归（候选新增 finding 即拒绝）。

---

## 1. 事实（不是推测）

**1.1 两条规则联立无解**

- 规则 A：`tools/check_citations/resolvers.py:466-481` —— 一旦某个任务在 config 里带
  `superseded_by`，它在 `docs/spec/architecture/ARCHITECTURE.md` §2.2 的那一行
  **必须**出现字面量 `superseded by T10x`。
- 规则 B：`tools/workflow/core.py:1704-1706` —— `finish-amendment` 对 SUPERSEDE 层
  只放行 `todo/config.yaml`，其余路径一律拒绝。
- 结果：A 要求改文档，B 禁止改文档。A0020 因此无法靠自身 retry（同层）修复。

**1.2 清理成本被状态机放大**

- `finish_amendment_review`（`core.py:2060-2070`）只在 PASS 时删除运行时记录；FAIL 保留。
- `prepare_amendment`（`core.py:1568`）见到**任何**记录就拒绝新 amendment。
- 于是「本层修不了的失败 amendment」永久堵死整条车道。
- 删除记录后 `_next_amendment_id`（`core.py:1036-1052`）回退编号，下一个 amendment 复用
  A0020，而它的分支名/工作树路径仍被旧尝试占用 → 需要第二次 Owner 动件（移除工作树 + 重命名分支）。

**1.3 检查器把约束落错行**

- `_expand_task_range`（`resolvers.py:506-514`）用 `re.findall(r"T[0-9]{3}", cell)`，
  因此后继行的 `T105 (successor to T063)` 被当成**该行也引用 T063**，进而要求这一行
  也写 `superseded by T105`——要求落在错误的对象上。
- 实测：§2.2 出现 8 条 `architecture-section22` finding（T063/T064/T066/T067 四行 +
  T105–T108 四行），`tests/test_documentation_citations.py` 与 CI 的
  `Document citation check (T005)`（`.github/workflows/ci.yml:56-62`）由绿转红。

**1.4 确定性门禁没有前移**

- `finish_amendment` 只做路径边界、config 冻结漂移与 `git diff --check`；
  它对 `check_citations` 变红毫无察觉（A0020 封存成功即证据），红点只能靠人工评审发现。

---

## 2. 目标 / 非目标

**目标**

1. 让「退休」在**不牺牲任何现有保障**的前提下单次走通，不需要 Owner bootstrap。
2. 让任何**可达状态都有出路**（今天的失败状态没有出路）。
3. 让每条新规则都点名「由哪一层满足」，并证明那一层够得着。
4. 让机械可判定的红在封存时刻就被挡住，而不是消耗独立评审。

**非目标（明确不做）**

- 不放松 APPROVED 不可动、PROPHET 不得改既有合同、一次只允许一个**可变** amendment、
  `tools/` 只能 bootstrap。
- 不收紧记录措辞。今天那些「计数 3 实为 5」「行号过期」「归因写错」的 FAIL 是评审在正常
  工作，压制它们只会降低发现率。

---

## 3. 新增设计检查项（写进 `todo/WORKFLOW.md`）

> **每条新规则必须点名由哪一层满足，并证明该层对规则要求的写入面拥有权限。
> 若没有这样一层，则不得立成硬规则，而应改为「在能够修改它的那一层执行的硬检查」。**

今天的死锁正是漏掉这一步的产物。

## 4. 判据：派生状态 vs 证据状态

> **ownership map、TRACEABILITY、phase README 的归属表述是派生状态**——它们必须与权威状态
> （`todo/config.yaml` + 合同正文）一致，本身是指针，可以随权威状态更新。
> **APPROVED 合同正文、status / attempt / SHA / evidence / review 指针是证据状态**——不可变，
> 任何新规则不得要求它跟着动。

规则可以要求派生状态与配置一致，但不得要求证据状态跟着动。今天无解的两条规则，就是把派生
状态当成了证据。

---

## 5. 变更 1：把「退休时必须改文档」的责任移到能改文档的那一层

**新规则（R1）**
声明 `replaces: [X]` 的后继任务，**必须在创建它的那次变更里**让 X 在 §2.2 的那一行命名它。
后继永远由 PROPHET 创建（只有该层可新增任务），而 PROPHET 本就可写 `docs/spec/`。

**实施点**

- `tools/check_citations/resolvers.py`：`resolve_architecture_section22` 增加对称子规则——
  对每个存在 `replaces` 的任务 S 与其前驱 X，若 §2.2 存在 X 的行，该行必须命名 S
  （沿用现有字面量 `superseded by {S}`，或新增一个等价可接受的措辞；二选一由实现者定，但
  必须与 5.1 的存量迁移一致）。
- `todo/WORKFLOW.md`：在 PROPHET 一节写入 R1 与理由。

**为什么不削弱**

退休后读者仍能从 X 那行找到继任者；这句话只是从「前驱死亡时」提前到「后继诞生时」写下。

**为什么不产生新死锁**

满足 R1 的层是 PROPHET，它对规则要求的写入面（`docs/spec/` + 新增任务）有完整权限。
这正是第 3 节检查项的实例。

**存量迁移**

当前只有 T105–T108 带 `replaces`（`todo/config.yaml:1152/1170/1190/1208`）。A0020 的预注记
恰好让这四条满足 R1；若 A0020 未落地，则先执行 A0020 再启用该子规则，或在同一次 bootstrap
中对这四行做等价修正。

---

## 6. 变更 2：收窄 §2.2 的 token 展开

**实施点**

`_expand_task_range` 在展开前，先屏蔽掉**关系括注**中的任务 token：
形如 `(successor to X)` / `(supersedes X)` / `(replaces X)` 的括号组按注解处理，不参与引用展开；
逗号列表与 `T084-T086` 区间照旧展开。

**风险与抑制**

风险：把「一行真的声明了两个任务」误当注解。
抑制：只豁免**括号内且匹配关系短语**的 token；`T105, T063` 这类并列不受影响，并且新增一条
单测固定这一边界（见第 10 节 T3）。

**效果**

后继行可以继续写 `(successor to T063)` 供人阅读，检查器不再误报。

---

## 6b. 变更 2b：让 `check_imports` 读懂注记（实测补充，2026-09-20）

**实测事实（先假设、后被评审测量推翻一半，据实记录）**

`tools/check_imports/architecture.py:102` 的 `_TASK.fullmatch` 会**跳过**带注解的任务单元格——
这一点成立（既有先例：T022/T023/T038 自 A0008 起即如此）。

但「因此损失覆盖」**不成立**：A0020 的评审判 PASS 时实测，注记前后 §2.2↔layer map 比对的
模块集合完全相同（各 52 行）——`robinhood_lp.reports`、`robinhood_lp.robustness`、
`robinhood_lp.experiments` 及其成员只是从 119/120/122 行移到后继行 155/156/157，层字符串一致。
所以当前没有任何模块失去校验，也没有 disagreement 被掩盖。

**为什么仍然建议修**

它留下的是一个**潜伏**的不对称：一旦某模块只被退役行承载（后继不接手，例如退休而没有等价
后继模块的情形），「跳过」就会静默取消它的比对。修法与测试照旧——只是理由从「正在损失覆盖」
改记为「结构性潜伏风险」，避免提案用一个未验证的断言去驱动改动。

**修法**

让 `check_imports` 把 `T063 (superseded by T105)` 解析为前导任务号 `T063` 后照常比对，而不是跳过。

**测试**

新增一条：注解行与未注解行的 §2.2↔layer map 比对结果必须相同（同一行文本，两种写法）。

---

## 7. 变更 3：终止态 + 「消费 ≠ 删除」+ 单调编号

**新模型**

- `AmendmentRecord.status` 增加终态 `ABANDONED`；`APPROVED` 从「删除记录」改为「写终态记录」。
- 活跃判定只统计非终态：新增 `_active_amendment_records()`，
  `apply to` 四个调用点：`core.py:798`（status 输出）、`888`（ready）、`1568`（prepare_amendment）、
  `2177`（prepare_maintenance）。
- `_next_amendment_id` 统计**全部**记录（含终态）→ 编号只增不减。
- 新 gate：`withdraw-amendment <id>`（Owner 授权）
  - 前置：记录存在且为 `CHANGES_REQUESTED` / `BLOCKED` / `PLANNING`；
  - 行为：把本 attempt 的评审结论与撤回理由写入 `todo/amendments/<id>/`，提交到该 amendment 分支，
    置终态 `ABANDONED`，**不落地任何候选改动**；
  - 输出保留分支名与理由路径，供人追溯。
- `amendment-status` 能报告终态（现已回落到读 review 文件，行为保持）。

**为什么不削弱**

撤回只能由 Owner 发起、必须写明理由、失败评审一并入档；工作区一行都不放行。ABANDONED 分支与
记录都留在 git 里。

**需要覆盖的回归**

- 撤回后同目标可立即重发（不再需要 Owner 动手删记录）；
- 撤回/通过之后编号不回退、分支名不冲突；
- `status` / `ready` / `prepare-maintenance` 不再被终态记录阻塞。

---

## 8. 变更 4：确定性门禁前移到封存时刻

**实施点**

`finish_amendment`（以及 `finish_amendment_review` 的 PASS 路径）在提交前运行仓库自身的确定性检查：
`tools.check_citations`、`tools.check_acceptance`、`tools.check_imports`。

**关键语义：增量判据**

判据是「候选的 finding 集合 ⊆ 基线（`record.base_commit`）的 finding 集合」，而不是「没有 finding」。
否则基线本来就红的仓库会陷入新的死结。实现建议：在 `record.base_commit` 上起一个临时工作树跑一次
（控制器已有同类操作），或对 base commit 做结果缓存。

**为什么不削弱**

语义判定仍归独立评审；这道门只挡机械可判定的红。评审仍应复跑（今天的 review 正是这样做的）。

**不做**

本次**不**顺手改 `finish-develop` / `finish-review`。任务车道的验证契约不同，牵连面更大，
留作后续单独评估。

---

## 9. 变更 5：`todo/WORKFLOW.md` 移出 PROPHET 可写面

现在约束条文的载体（WORKFLOW.md）可被它所约束的角色（PROPHET）修改。规则与实现都在 `tools/`，
所以两者应一同走 bootstrap 动件。本次改动同时更新 `propshet` 路径白名单
（`core.py` 的 `_prophet_path_allowed`）并在 WORKFLOW.md 中写明理由。

---

## 10. 验证

**单测 / 集成测**

- T1 端到端：新建后继（带 `replaces`）→ §2.2 注记 → 退休一次通过，中途不需要任何 Owner 动件；
- T2 撤回：制造一次必 FAIL 的 amendment → `withdraw-amendment` → 同目标重发成功；编号不回退；
- T3 token 边界：`T105 (successor to T063)` 不再触发 finding；`T105, T063` 仍按两行引用处理；
- T4 门禁前移：候选故意破坏 citation → `finish-amendment` 拒绝封存；基线本来就红的项目不受影响（增量判据）；
- T5 兼容：A0001–A0019 的既有记录与已归档的 `amendment/a0020-attempt-001-failed` 不因新状态机而报错。

**验收（最有说服力的一条）**

用新规则**重放今天的真实历史**：A0014 → A0015…A0019 → 退休。要求：
全程不需要 Owner bootstrap 动件，且任何中间 FAIL 都能靠 `withdraw-amendment` + 重发继续。

**命令**

```
python -m tools.workflow validate
python -m pytest tests/test_workflow.py tests/test_workflow_contracts.py \
  tests/test_documentation_citations.py tests/test_documentation_links.py \
  tests/test_acceptance.py tests/test_import_graph.py
python -m tools.check_citations check && python -m tools.check_acceptance check \
  && python -m tools.check_imports check
```

---

## 11. 落地方式与回滚

- **一次 bootstrap 提交**（参考 `1aa6f45 chore(workflow): harden requirement change governance`），
  内容 = 本提案的规则条文（WORKFLOW.md）+ 五个变更点的实现 + 第 10 节测试。
- 提交前：`git status` 应干净（注意 `_ensure_clean_main` 把未跟踪文件也算脏，会堵住 amendment 车道）。
- 回滚：单提交 revert；本次不触碰任何 APPROVED 证据、不迁移任何任务状态，因此 revert 无数据影响。
- 与在途工作的关系：A0020 的预注记在变更 1、2 下从「必需」降级为「无害」，两者不冲突。

---

## 12. 留给 Owner 决定的问题

1. 变更 1 的措辞：沿用字面量 `superseded by T10x`，还是新增更自然的 `designated successor T10x`
   且两者都接受？（影响 §2.2 的书写风格与检查器的严格度。）
2. `withdraw-amendment` 是否要求 Owner 在命令里显式给出理由文本，还是允许引用一份已提交的说明文件？
3. 变更 4 是否在下一轮一并覆盖任务车道（`finish-develop` / `finish-review`）？
4. 是否需要把本提案本身随 bootstrap 提交存档到 `docs/implement/`（作为该次动件的理由记录）？
