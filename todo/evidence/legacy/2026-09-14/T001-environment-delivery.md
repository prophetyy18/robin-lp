# T001 交付报告 — Phase 0 工程基线 / Python 3.12 骨架

> 本文件是 T001 "Create the Python 3.12 project skeleton" 的交付证据。
> 按 `AGENTS.md` §8 的交付报告规范。
> 本文件不对应 `TODO.md` 的某个 Phase——它对应 `TODO.md` §"Phase 0" 中的 T001。

## 1. Outcome

deterministic local development entry point.

`TODO.md` 中的 T001 写明:"deterministic local development entry point"。
本会话已实现:
- `pyproject.toml`(hatchling backend,Python `>=3.12`,Pydantic + eth-hash + httpx runtime)
- `src/robinhood_lp/` package with `__init__.py` 与 `__main__.py` CLI
- `tests/` 含 smoke test
- `tests/`,`ruff check`,`ruff format --check`,`mypy src tests` 都可通过

## 2. Dependencies

- 依赖:T000("Record initial architecture decisions") ✅ 已完成(在 commit `bd0a23a` 之后)。
- 不依赖任何代码模块,只依赖 "用户授权使用 miniconda 用户级 Python 环境"。

## 3. Deliverables

| Deliverable | 位置 | 状态 |
| --- | --- | --- |
| `pyproject.toml` | 仓库 HEAD | ✅ 存在 |
| `src/robinhood_lp/` | HEAD | ✅ 存在 |
| `tests/` | HEAD | ✅ 存在 |
| test/format/lint/type commands | 上一节列出 | ✅ 通过 |
| 一个 import/CLI smoke test | `tests/test_smoke.py` | ✅ 通过 |

## 4. Acceptance

T001 acceptance 写明:
- "build and install from a clean environment"
- "all quality commands pass twice"
- "package metadata contains supported Python bounds"

### 4.1 包 metadata 包含 supported Python bounds

`pyproject.toml`:
```toml
requires-python = ">=3.12"
classifiers = [
  ...
  "Programming Language :: Python :: 3",
  "Programming Language :: Python :: 3.12",
  "Programming Language :: Python :: 3.13",
]
```

✅ 通过。

### 4.2 环境版本(Python 3.12)

使用 miniconda 用户级 Python 环境(`/home/lpdev/miniconda3/`)安装:

```
$ ~/miniconda3/bin/conda run -n robinhood-lp python -c "import sys; print(sys.version)"
Python 3.12.14 | packaged by Anaconda, Inc. | (main, Aug 27 2026, 14:46:43) [GCC 14.3.0]
```

环境名:`robinhood-lp`
解释器路径:`~/miniconda3/envs/robinhood-lp/bin/python`
包来源说明:本会话早期 `pip install -e ".[dev]"`(来自当前 git working tree 的 editable install)。

✅ 通过(满足 "Python 3.12" 边界)。

### 4.3 Python runtime 与 dev dependencies 的可复现 lockfile

`/tmp/robinhood-lp.lock.txt`(30 行)由 `pip freeze` 生成:

```
annotated-types==0.8.0
anyio==4.15.1
ast_serialize==0.11.1
certifi==2026.7.22
coverage==7.16.0
eth-hash==0.8.0
h11==0.16.0
httpcore==1.0.9
httpx==0.28.1
idna==3.19
iniconfig==2.3.0
librt==0.15.0
mypy==2.3.1
mypy_extensions==1.1.0
packaging==26.3
pathspec==1.1.1
pluggy==1.6.0
pycryptodome==3.23.0
pydantic==2.13.5
pydantic_core==2.46.5
Pygments==2.21.0
pytest==9.1.1
pytest-asyncio==1.4.0
pytest-cov==7.1.0
-e git+ssh://git@github.com/melodydeck04/robinhood-lp.git@6c3177883a676a06f8b571d0a598f524e319fa34#egg=robinhood_lp
ruff==0.16.7
setuptools==83.0.0
typing-inspection==0.4.4
typing_extensions==4.16.0
wheel==0.47.0
```

⚠️ 残余风险:`pyproject.toml` 写的是 `>=X,<Y` 范围,**不是** `==X.Y.Z` 精确版本。第三方复现时
`pip install -e .[dev]` 可能解析到不同小版本,因此 lockfile 是**会话时点**的快照,
不是项目 controlled artifact。该 lockfile 未 checked in 到仓库。

### 4.4 完整质量门连续运行两次

两次都执行相同命令序列,记录时间戳与结果。

#### Pass 1(2026-09-14 11:57:54 CST)

| 命令 | 结果 |
| --- | --- |
| `pytest` | **265 passed, 2 skipped** (0.45s) |
| `ruff check .` | `All checks passed!` |
| `ruff format --check .` | `60 files already formatted` |
| `mypy src tests` | `Success: no issues found in 36 source files` |

#### Pass 2(2026-09-14 11:58:02 CST)

| 命令 | 结果 |
| --- | --- |
| `pytest` | **265 passed, 2 skipped** (0.46s) |
| `ruff check .` | `All checks passed!` |
| `ruff format --check .` | `60 files already formatted` |
| `mypy src tests` | `Success: no issues found in 36 source files` |

跳过项:
- `tests/test_abi_artifacts.py::test_artifact_file_sha256_matches_when_recorded` —
  SHA-256 是 `manual-annotation` 不是 64-hex,所以测试 skip(预期)
- `tests/test_protocol_ids.py::test_pool_id_matches_pinned_solidity_vector[reordered_inputs]` —
  Python `PoolKey` 模型强制 currency0 < currency1,乱序输入构造即失败(预期)

✅ 通过(两次完全一致)。

### 4.5 `git diff` 与 `git diff --check`

```
$ git diff --stat HEAD      → 空
$ git diff --check HEAD     → 空
$ git status                → nothing to commit, working tree clean
```

working tree 与 HEAD 一致,**没有**无关改动。

### 4.6 clean install

⚠️ **未真正执行** "build and install from a clean environment"。

- 当前 conda env 是会话早期手装的 editable install,**不是** 从干净 wheel/sdist 装出。
- lockfile 包含 `-e git+ssh://...` 路径,这证明 env 来自 git working tree 而非已发布的 wheel。
- 没有在隔离容器中真清空再 `pip install .` 一次。

这违反 T001 acceptance 的第一条,详见 §6 未满足项。

## 5. Must not

T001 must not:
- "add blockchain/storage/dataframe dependencies before their first consumer task"

✅ 满足:`pyproject.toml` runtime dependencies 仅 `pydantic`,`eth-hash[pycryptodome]`(T010 用),
`httpx`(T020 用);没有 pyarrow / web3.py / sqlalchemy。

- "expose environment contents in diagnostics"

✅ 满足:`src/robinhood_lp/__main__.py` 仅 print 版本号;`src/robinhood_lp/config/` 配置错误信息已 redact
credentials(`tests/test_config.py::test_loader_rejects_credential_url_in_config` 通过)。

## 6. 未满足项 / 残余风险

按 `AGENTS.md` §2 "只有全部验收通过后才能把任务改为 `[x]`",以下两点不通过:

1. **(A) 没有 checked-in reproducible lockfile**。
   `/tmp/robinhood-lp.lock.txt` 是会话 evidence,不是仓库 controlled artifact。
   T001 acceptance "reproducible local development" 需要 lockfile **在仓库内**,以便
   第三方不需要先 git+ssh 拉到当前 commit 就能复现。

2. **(B) 没有真实的 "from a clean environment install"**。
   两次 pytest 都在已有 conda env 上跑;`pip freeze` 看到的是 editable install。
   需要在隔离容器/临时 venv 中 `pip install .[dev]` 一次确认。

其他残余风险(来自 `AGENTS.md` §5 "外部事实"):
- `pydantic>=2.8,<3` 等版本范围是会话早期手写,严格说违反"凭记忆"原则。但 T001 acceptance
  没要求版本固定化,故此处记为残余风险,不阻塞 T001 验收是否通过(由用户判断)。

## 7. 跳过的测试

T001 acceptance 本身没有要求集成 / 网络 / 端到端测试。本会话唯一"跳过"的两条是
**预期** skip:
- `test_artifact_file_sha256_matches_when_recorded` skip,因为 SHA-256 字段是
  `manual-annotation` 而非 64-hex(测试代码里的 if 分支显式 skip)
- `test_pool_id_matches_pinned_solidity_vector[reordered_inputs]` skip,因为 Python
  `PoolKey` 模型在构造时就拒绝乱序输入

## 8. T001 是否可勾 `[x]`

按 `AGENTS.md` §2 "代码存在不等于任务完成",按 §8 "不使用'应该没问题'代替证据",
**当前不勾 T001**:

- T001 acceptance 第一条 "build and install from a clean environment" 未真正执行。
- T001 acceptance "locked dependencies" 当前**没有** checked-in lockfile。
- 这两点都需要用户授权才能推进:写 `requirements.lock.txt` 到仓库并 commit,以及真清空环境装一次。

本会话按你的指令("不要自行 commit、push 或覆盖当前未提交改动")和 `AGENTS.md` §7,
**保留 T001 为 `[ ]`**。

## 9. 建议下一步

按 `STATUS.md` §9 正确的顺序:

1. 闭合 T001 (A) (B):写 `requirements.lock.txt` 到仓库,commit(需要用户授权)。
2. 在隔离容器或临时 venv 跑一次 clean install + 两次质量门,保留 evidence。
3. 复核 T002 (V1 single-chain / single-active-pool 严格模型)。
4. 补齐 T003 intentional-failure 表 + vulnerable-dep 证据。
5. 按 T010 → T011 → T012 → T013 顺序复核。其中 T012/T013 有实质缺口(浮点转换、缺少
   manual-review evidence)。
6. 继续 T020 → T021 → T024 → T022 → T023 → T025 → T030 → ... 链上任务。
