# 项目执行规则

这些规则适用于本仓库中的所有编码代理。

## 1. 目标和依据

- 只交付 V1。不要自行增加 V2、多链、多活动池、多用户或策略市场。
- V1 最终目标包括 Robinhood Chain mainnet 自动执行。回测、testnet 和 paper 是
  证据门槛，不是最终交付的替代品。
- 文档分为 Intent → Spec → Implement 三层，定义见 `docs/README.md`。
- 文档冲突时按以下顺序处理：
  1. `docs/intent/PROJECT_GOALS.md`
  2. `docs/spec/product/`、`docs/spec/strategy/`、`docs/spec/operations/`、
     `docs/spec/security/`
  3. `todo/` 中当前任务合同
  4. `docs/spec/architecture/` 和 `docs/spec/protocol/`
  5. `docs/implement/`、代码和测试
- 低优先级内容与高优先级内容冲突时，以高优先级内容为准并报告冲突。不要悄悄
  选择对实现更方便的版本。
- 不要把尚未确认的想法写成需求。产品选择不明确且会改变行为时，停止并提问。

## 2. 执行 TODO

- 一次只执行一个编号任务。
- `todo/config.yaml` 是任务进度的唯一机器可读来源；任务 Markdown 不保存 checkbox。
- 开始前阅读本文件、`CLAUDE.md`、项目目标、`todo/README.md`、该任务全文、任务引用
  和所有将修改的文件。
- 开始前确认所有依赖任务已经完成，且阶段 Entry 条件满足。没有满足就不要编码。
- 开始前用简短文字列出：Outcome、Dependencies、Deliverables、Acceptance、Must not。
- 只实现当前任务。不要顺手实现后续任务，不要做无关重构。
- 当前任务中的 Outcome、Deliverables、Acceptance 和 Must not 都是任务合同，
  不能只完成其中一部分。
- 只有独立审查全部通过后，工作流控制器才能把任务改为 `APPROVED`。缺少凭据而
  跳过的集成测试不算通过。
- 不要因为代码已经提交、测试数量增加或主要路径能运行就宣称任务完成。

### 2.1 角色和交接边界

- Planner 只澄清 Intent、Spec 和任务合同，不实现业务代码，也不伪造验收证据。
- Issue Triager 只读核对异常证据并区分实现缺陷、任务合同偏差、Spec 缺陷、Owner
  决策和外部阻塞；发现者提出的分类不是最终分类。
- Developer 每次以全新上下文在独立 worktree 中只实现一个 `READY` 任务。不得修改
  Intent、Spec、任务合同、审查记录、审查 Agent 或批准状态；规格不足时返回
  `TRIAGE_REQUIRED` 和证据，不自行决定分类。
- Developer 单会话预算不足但任务本身未阻塞时，可以返回
  `CONTINUATION_REQUIRED`。控制器保持同一 task、attempt、branch、worktree 和
  `IN_DEVELOPMENT` 状态，再启动一个全新上下文续跑；这不是重试、triage 或审批。
- Reviewer 每次以全新上下文在 candidate commit 的 detached worktree 中审查。它可以
  运行验证命令，但不得修改、修复、提交或推送任何内容。
- Plan Reviewer 在 exact planning base/candidate 上独立检查合同或 Spec 修正；允许有证据
  的 `NO_CHANGE_REQUIRED`，不得为了制造 diff 要求无意义改文档。
- Manager 可以解释状态、选择合法的下一步和处理歧义，但不能替代独立 Reviewer
  批准任务。
- Agent 之间只以任务合同、base commit、candidate commit 和结构化报告交接。未提交
  工作区、聊天结论和“已经完成”的自述不是交接证据。
- `tools.workflow` 只执行 worktree、SHA、路径保护、结构化输出和状态转换等机械门禁；
  它不判断产品需求或代码语义，也不得启动 Claude。Manager 使用 Claude Code 原生
  Agent 工具启动、展示和恢复角色，再调用对应的 `prepare-*`/`finish-*` 门禁。
- 审查失败后必须启动新的 Developer；修复产生新的 candidate commit 后，再启动新的
  Reviewer。不得复用导致结论偏置的旧上下文。
- 正常任务只走开发和审查。只有结构化报告明确要求时才进入 triage；不要把普通实现
  问题升级成规划阻塞。涉及 Intent 的选择必须停下等待 Owner 决定。
- 已稳定复现、不会改变 Intent/Spec/公开接口/依赖/安全或交易行为的局部实现缺陷，
  可以走 `Mxxxx` 维护通道。维护记录不加入产品任务图，但仍必须使用隔离 Developer、
  exact candidate commit 和独立 Reviewer。维护需要扩大路径或改变行为时必须停止并转入
  triage 或正式编号任务，不能借“小修”绕过合同。

## 3. V1 固定边界

- 只支持 Robinhood Chain，并校验实际 chain ID、部署地址和 runtime code hash。
- 用户用 contract address 人工选择一个目标 token。系统不得自动选择或切换。
- 可以发现一个目标 token 的多个候选 PoolKey，但同一时间只能有一个人工选择的
  活动 PoolKey。
- Token、配对 Token、PoolKey 和 Hook 分别审批。symbol/name 只用于显示。
- 不理解 Token 或 Hook 的实际结算行为时，禁止把它用于策略、paper 或 live。
- 策略只处理正常市场和仓位生命周期；中央风险层执行不可绕过的资金、权限、
  估值、Gas、损失和回撤限制。
- 价格、交易量和流动性分布是 V1 策略市场信号边界。涉及 USDG 的价格特征和 5 分钟
  极端涨跌规则必须使用 T053 合格、按时间点可复现的 USDG 换算；不得用活动池原始
  相对价格冒充。
- `NO_NEW_RISK` 是带作用域和 reason code 的风险结果，不是全局运行状态，也不
  自动退出已有仓位。
- 价格离开 Range 不等于退出。策略必须明确选择等待、重建、减仓或退出并计算成本。
- 所有主要资金、风险和绩效以 USDG 等值表达，同时保留原始 token/ETH 整数数量。
- 除已确认的 5 分钟规则外，不要在进入 paper trading 前自行确定其他经济或风险数值。
  当前任务实现可配置 schema、版本、校验、测试、证据和审批绑定；临时值标记为
  `EXPERIMENTAL_NOT_LIVE_APPROVED`。
- testnet 前可运行 preliminary paper 验证实现，但它不是 live 证据。正式顺序是
  backtest → testnet → post-testnet paper/shadow → 安全复核 → 人工晋级。

## 4. Live 和密钥

- Phase 0–8 不得加入签名、广播、私钥或明文 Keystore 解密能力。
- Phase 9 中能够签名或广播的任务仍需要该任务写明的用户授权。完成前序任务不等于
  获得 live 权限。
- mainnet 交易只能在 T094 的明确、带范围人工晋级之后执行。
- 主应用、Web、策略和风险进程不得读取私钥或 Keystore 密码。
- 私钥只保存在标准加密 Web3 Keystore 中。密码只通过不回显的交互式终端输入，
  不得来自环境变量、配置、CLI 参数、Web、文件、日志或数据库。
- 明文私钥只允许存在于隔离 signer 的内存中。进程重启后 signer 必须保持锁定。
- 不得连接或控制用户主钱包，不得自动把退出资产转到外部地址。
- 不得打印环境变量、凭据 URL、Webhook、私钥、密码、签名原文或授权头。

## 5. 外部事实

- Robinhood Chain、Uniswap 部署、ABI、Hook、Token 和 RPC 能力都会变化。不要凭
  记忆或从其他链复制。
- 优先使用官方源码、官方文档和链上读取。第三方页面只能作为交叉验证。
- 对可变事实记录来源 URL、获取时间、chain ID、区块号/区块哈希和 code hash，
  适用什么就记录什么。
- 无法验证时返回 UNKNOWN、暂停或阻止晋级。不要猜默认值。

## 6. 代码边界

- 保持 protocol、RPC、storage、reconstruction、features、strategy、risk、execution、
  presentation 分层。依赖方向以 `docs/spec/architecture/ARCHITECTURE.md` 为准。
- strategy 不访问 RPC、数据库、signer 或执行器，不修改账本，也不批准自己的风险。
- execution 不决定策略，不能绕过中央风险检查。
- 链上数量、tick、价格编码、liquidity 和会计路径使用整数。只在明确的展示或统计
  边界转换为 Decimal；协议和会计路径禁止 float。
- replay、feature、backtest 使用事件时间和当时已可获得的数据。禁止 future data、
  wall clock 和未固定随机数。
- 原始数据和审计事件追加写入。不得覆盖、删除或用插值填补缺口。
- 错误必须带足以定位的 chain、PoolKey、block/range、endpoint、attempt 和原因，
  同时完成秘密脱敏。

## 7. 修改和验证

- 先读现有实现，再修改。不要猜代码行为。
- 使用最小改动完成任务。保留用户已有和无关的工作区改动。
- 新行为必须有正常、边界、无效输入和失败路径测试。需要两个异质 fixture 时不要
  用同一个 fixture 改名代替。
- 完成前运行任务要求的测试，以及 `pytest`、Ruff format/check 和严格 mypy。普通测试
  输出只要求结果和退出码稳定；只有 fixture、artifact、序列化等确定性产物才要求
  byte-for-byte 一致。Developer 与独立 Reviewer 各运行一次已经是两次独立验证，不得
  无理由要求每个普通门禁在同一角色内重复两遍。
- 测试失败时查明是本次引入还是已有问题。不要删除测试、放宽容差、增加无理由
  ignore 或降低风险门槛来让 CI 通过。
- 检查 `git diff` 和 `git diff --check`。不要提交 `.env`、Keystore、密钥、凭据、
  运行数据或包含秘密的截图。
- 不要自行 commit、push、merge、部署或发送外部消息。只有用户明确要求时才执行。

## 8. 交付报告

- 说明完成了什么、修改了哪些文件、运行了哪些命令及结果。
- 列出未满足的验收、跳过的测试、假设、外部事实版本和残余风险。
- 如果任务没有完成，保持非 `APPROVED` 状态，并准确说明阻塞项。
- 不使用“应该没问题”“基本完成”或“理论上通过”代替证据。
