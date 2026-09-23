# CI 门禁债务测量（2026-09-23）

- 测量时间：2026-09-23，HEAD `dcf205d`
- 测量者：Owner 指定的 bootstrap 会话；**本文不是任务合同、不是排期、不是评审记录**
- 环境：项目 Python 3.12（`/home/lpdev/miniconda3/envs/robinhood-lp/bin/python`）、ruff 0.16.7、
  mypy 2.3.1，三者与 `requirements.lock.txt` 的钉版一致

## 1. 结论

`.github/workflows/ci.yml` 声明的两道仓库级门禁在当时都是红的：

| 门禁（CI 步骤） | 测量结果 | 处置 |
| --- | --- | --- |
| `ruff format --check .`（`ci.yml:104`） | 4 个文件未格式化 | **已修**：M0004（候选 `0aeeb01`，评审 `570c64e`） |
| `mypy src tests`（`ci.yml:108`） | 先在中止点报 1 个错误，其后还有 **149 个错误 / 15 个文件** | **未修**：超出维修车道（最多 5 个路径），按 `todo/WORKFLOW.md` 需要编号任务或计划层变更 |

两份维修记录（`todo/maintenance/M0004/`、`todo/maintenance/M0005/`）各自保存了当时的请求、开发者结果与
独立评审；本文只汇总**跨任务**的门禁状态与测量方法。

## 2. 格式门禁（已关闭）

4 个文件与它们**首次**进入未格式化状态的提交（用钉住的 ruff 以 stdin 模式逐文件比对父提交得出，
不是抽样）：

| 文件 | 引入提交 |
| --- | --- |
| `src/robinhood_lp/__main__.py` | `2f64e64`（T069 候选，2026-09-21） |
| `src/robinhood_lp/backtest/engine.py` | `c79e4c8`（**T109 候选，已 APPROVED**；其父提交 `791fc4e` 上这三个文件仍是格式化状态） |
| `src/robinhood_lp/protocol/contracts.py` | 同上 |
| `tests/test_t109_acceptance.py` | 同上 |

`todo/reviews/P06/T109/review-004.md` 把 `__main__.py:220` 记为既存项，但没有记录 T109 自己引入的三个：
该次评审跑的是 `ruff check`（lint），没有跑 `ruff format --check .`。

## 3. mypy 门禁（仍红）

### 3.1 中止机制

`mypy src tests` 在第一处重复模块名即停止，原话：

```
tests/_storage_t031_fixtures.py: error: Source file found twice under different module names: "_storage_t031_fixtures" and "tests._storage_t031_fixtures"
...
Found 1 error in 1 file (errors prevented further checking)
```

成因自 2026-09-17（T035 候选 `e272b67`）起存在：`tests/` 内三个文件用 `tests.` 前缀导入兄弟模块，
而其余九十多个文件用裸名（`from _storage_t031_fixtures import ...`）：

- `tests/test_storage_block_headers.py:380`
- `tests/test_storage_partition_columns.py:21`
- `tests/test_strategy_t060.py:1457`

### 3.2 中止之后的真实数量

把上面三行改成裸名（与多数派一致）后，中止消失，`mypy src tests` 报告 **149 个错误 / 15 个文件**：

| 文件 | 错误数 |
| --- | --- |
| `tests/test_experiments_t066.py` | 42 |
| `tests/test_robustness_t106.py` | 31 |
| `tests/test_main_ingest.py` | 24 |
| `tests/test_evaluation_t102.py` | 13 |
| **`src/robinhood_lp/research/evaluation.py`** | **10** |
| `tests/test_t109_acceptance.py` | 7 |
| `tests/test_workflow.py` | 5 |
| `tests/test_risk_t070.py` | 3 |
| `tests/test_ingestion_block_header_source.py` | 3 |
| `tests/test_backtest_t069.py` | 3 |
| `tests/test_storage_block_headers.py` | 2 |
| `tests/test_research_dataset_t100.py` | 2 |
| `tests/test_fee_surface_t104.py` | 2 |
| `tests/test_ingestion_endpoint_client.py` | 1 |
| `tests/_qualification_t036_fixtures.py` | 1 |

错误码分布（按错误行统计，个别行含两个方括号标记）：`no-untyped-def` 59、`arg-type` 44、
`attr-defined` 11、`type-arg` 8、`unused-ignore` 7、`call-overload` 6，其余为 `str`、`assignment`、
`operator`、`union-attr`、`no-any-return`、`no-untyped-call`、`misc` 等。

`src/` 只有 `src/robinhood_lp/research/evaluation.py` 一个文件命中（10 个，如 `clock`/`rng` 参数被推断为
`object` 的 `arg-type`、`int(...)` 的 `call-overload`）；`src/` 其余文件干净。**这 10 个尚未判定
哪些是真实缺陷、哪些只是标注问题。**

### 3.3 中止遮住的真实缺陷（已修）

`src/robinhood_lp/orchestrator/__init__.py:886` 的 `simulation_evidence_path` 是 T109 候选 `fac669a`
新增的**必需**字段（无默认值），而 T069 写下的 CLI `cancel` 分支没有跟着传它，于是取消**非终态**运行
直接抛 `TypeError: RunRecord.__init__() missing 1 required positional argument:
'simulation_evidence_path'`；既有测试只覆盖"对已终态记录 cancel"的 no-op 分支。

已由 M0005 修复（候选 `384ea6b`，评审 `dcf205d`，修复行 `src/robinhood_lp/__main__.py:823`），并补三条
回归测试（RUNNING 取消、QUEUED 重跑源链接保留、未知 run_id 失败路径）。**该缺陷自 2026-09-21 起被
本中止遮住约两天。**

## 4. 为什么历史评审没有发现

不是"没有扫描这一环"，而是**扫描范围**：

- 仓库级格式门禁自 09-17/09-21 起即红，但评审跑的是各自文件范围内的 `ruff check`；
- 仓库级 mypy 命令自 09-17 起就在第一个文件中止，其输出里从来没有第 2 条错误；
- 评审记录里的 mypy 证据是**文件范围内**的（例如 T109 `review-004` 写的是
  `mypy --strict <6 个源文件>`）。

`AGENTS.md` §6 要求"每个任务都跑 pytest、Ruff format/check 与严格 mypy"。在这两道门禁红着的情况下，
任何任务的"通过"都只是文件范围内的通过——**这正是 T109 候选既引入格式漂移、又引入运行时缺陷而仍被
APPROVED 的原因**。

## 5. 复现方法

```bash
# 格式门禁（仓库级）
/home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m ruff format --check .
# 逐个文件判断"某提交是否已未格式化"（不改工作树）
git show <rev>:<path> | /home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m ruff format --check \
  --stdin-filename <path> -
# mypy 门禁：直接跑只会得到中止点
PYTHONPATH=src /home/lpdev/miniconda3/envs/robinhood-lp/bin/python -m mypy src tests
# 想看中止之后的 149 个，必须在副本里先把那三行 tests. 前缀导入改成裸名
```

本机 `pytest`/`mypy` 需要 `PYTHONPATH=src`：editable 安装指向已删除的
`/home/lpdev/lp-worktrees/dev-t102-attempt-001`（见 M0004/M0005 的 residual risks）。

## 6. 未决事项

1. **149 个 mypy 错误的修复**：需要编号任务（维修车道限 5 个路径，且其中一部分需要判定是真缺陷还是
   标注问题）。第一步是那三行 `tests.` 前缀导入，它解除中止但不使门禁转绿。
2. `src/robinhood_lp/orchestrator/__init__.py` 的 `RunRecord` docstring "Field units" 段列了全部字段、
   漏列 `simulation_evidence_path`（1 个路径的小修，尚未做）。
3. 环境：本机 editable 安装指向已删除的工作树，使 CI 形状的裸 `pytest`/`mypy` 在本地无法导入
   `robinhood_lp`；重指会让所有工作树改从主检出导入 `src`，破坏评审隔离，故未动。

## 7. 本文不声称的内容

- 不是任务合同、不是排期、不是审批；不改变任何任务状态或历史；
- 数字是 `dcf205d` 上的快照，随 HEAD 变化；
- 未判定 149 个错误中哪些是真实缺陷、哪些只是类型标注问题，也**没有**为它们指定修复路线。
