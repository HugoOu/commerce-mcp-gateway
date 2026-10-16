# 读代码导览（AI 维护，供你事后学习）

> 用途：`AGENTS.md` 硬规矩 1 要求「简历上写的每个模块，必须能从小规模手写复现」。2026-10-15 起三处核心由 AI 实现（ADR-0026），达成路径改成「**读代码导览 + Step 5 盘问**」。
> 用法：按下面的顺序读；每节末尾的自测问题答不上来，就记进 `docs/interview-failures.md`。

## 0. 先建立地图

一条调用穿过的层（`docs/architecture.md` 第 4 节）：`frontdoor`（HTTP + 拦截点）→ `enforce`（唯一执行点）→ `policy`（判定）→ `budget`（预算）→ `upstream`（转发到 mock）→ `evidence`（记账）。
读代码的顺序建议：**词汇表 → 策略 → 预算 → 证据 → 执行点 → 前门**，因为后面的模块都依赖前面的概念。

## 1. `src/gateway/decision.py` —— 全项目的词汇表

- **读什么**：`InterceptedRequest`（一次被拦截的调用，含**未校验**的原始参数与 `now`）、`AuthorizationBasis`（谁、被授权做什么）、`Mandate`（用户意图，结构对齐 AP2 的 open / closed）、`Decision`（策略输出）。
- **为什么这样切**：这四样东西被 policy / budget / evidence / enforce 共同使用。放一处才能避免循环 import，也让「一个 Decision 由什么构成」只有一个定义（architecture 第 3 条）。
- **自测**：① 为什么 `tool_call_id` 必须由网关签发、且在重试之间保持不变？② `arguments` 为什么特别标注「未校验」？③ `now` 为什么放在请求上，而不是让策略自己读时钟？

## 2. `src/gateway/policy/` —— 策略求值

- **读什么**：先读 `engine.py` 顶部的模块 docstring（求值四步与各自的出处），再读 `_matches` / `_arg_matches`（命中与算子），最后读 `_check_constraints`（约束与 fail-closed）。`schema.py` 只在你想知道「策略文件能写什么」时读。
- **三处最容易追问的点**：① `segment_glob` 为什么 `merchant__*` **不**跨段（ADR-0001：语言止步于 glob）；② 为什么是「最严者胜」而不是「任何 allow 命中就放行」（非补偿性门，见 `docs/evaluation.md`）；③ 为什么策略约束与 mandate 约束是**取交集**（ADR-0020：策略只能收紧授权）。
- **自测**：① 参数里写 `"amount": "9.71"`（字符串）会怎样，为什么？② 策略文件里多写一个字段会怎样，为什么这样设计？③ `merchant__**` 与 `merchant__*` 的区别是什么？④ 没有传 `now` 时，过期判定会怎样？

## 3. `src/gateway/budget/` —— 预算原子扣减

- **读什么**：整个模块就在 `__init__.py` 里（类型与实现放一起，因为账本只做这一件事）。顺序：`SCHEMA`（表结构与那条 `UNIQUE`）→ `reserve`（条件更新与幂等）→ `commit` / `release`（终结与幂等）→ `balance` / `reclaim_expired` → 内部工具 `_transaction` / `_used_cents`。
- **为什么这样切**：
  - **金额以「分」入库**（整数）：浮点求和会在边界上让 `≤ limit` 假成立，而 I2 正是靠这个不等式成立的；
  - **`BEGIN IMMEDIATE` 把「读余额 → 判断 → 写 hold」关进同一个事务**：这就是 ADR-0002 点名的在途窗口（两笔 30 元并发对 100 元上限会都看到 100 可用）；
  - **幂等靠 `UNIQUE(session_id, tool_call_id)` 兜底**：并发重复提交也只会插入一条；
  - **上限只能收紧**（`_upsert_session` 取 min）：与 ADR-0020「策略只能收紧」是同一条原则；
  - **`now` 由调用方传入**：实现里不读时钟，重放才可复算（architecture 第 4 条）。
- **自测**：① 为什么 `reserve` 失败时必须「不留下半条 hold」？② 同一幂等键重试时返回的是哪一条 hold，为什么这样设计？③ 释放过的 hold 还占额度吗？④ 50 个线程抢 100 元上限、每笔 30 元，应该有几笔成功，为什么是「恰好」而不是「最多」？⑤ 为什么重复 `commit` 要幂等，而 `commit` 之后再 `release` 要拒绝？

## 4. `src/gateway/evidence/` —— 证据链与证据包

- **读什么**：`EvidenceEntry`（链上的单元）→ `_entry_hash` / `_merkle_root`（两个哈希怎么算）→ `ChainLedger.record_pending` / `record_done`（写入顺序）→ `verify`（能检出什么）→ `replay`（前置条件与决策序列）→ `record_inputs` / `reconstruct`（ADR-0028 的可重构性）。
- **为什么这样切**：
  - **`prev_hash` 参与自己的哈希**：改了任何一条，它后面所有条目的哈希都会变，链因此不可局部伪造；
  - **写入顺序是「链 → 包 → manifest」**：中途崩溃会让包比链少一条，`verify` 会报出来——宁可报错，也不要静默地让账本与证据对不上；
  - **链与包互为见证**：包内截断靠 manifest 的条数与 Merkle 根发现，链被截断靠「包的叶哈希必须都能在链上找到」发现；
  - **`replay` 的评估器是参数**：证据层不 import 策略（层次干净），Step 4 由执行点把策略闭包传进来；
  - **三件套，不是一个文件**（ADR-0028）：链（只留摘要、可对外出示）+ 运行证据包（manifest 封印）+ **可复算输入件**（`runs/<run_id>.inputs.jsonl`，金额与商户在这里，可单独访问控制 / 删除）。绑定靠摘要：重放时校验 `digest(arguments) == 链上的 args_digest`。**撤下输入件后链仍然可验、而重构能力归零**——这个落差是可报的结论，不是缺陷；
  - **两个数分开报**（`ReconstructReport`）：前置条件满足率（证据够不够）与决策重构率（重算对不对）。混成一个数就看不出是「没证据」还是「证据对不上」；
  - **诚实边界**：把链与全部 manifest 一起重写的对手检不出（那需要外部锚点或见证，威胁模型 R5 本版未采纳）。
- **自测**：① 为什么短摘要（16 位十六进制）不能拿来做链？② 删掉运行包的最后一条，`verify` 靠什么发现？③ 只删链的最后一条，为什么 `verify()` 也能发现？④ 重放前置条件里为什么需要 `policy_digest` 与预算前后？⑤ 「verify 通过」保证了什么、又没保证什么？⑥ 为什么可复算输入**不能**直接写进链上条目（两个理由，一个是安全的、一个是机制的）？⑦ 重构需要的三个输入（`arguments` / `now` / `basis`）分别从哪儿来？

## 5. `src/gateway/enforce.py` —— 执行点

- **读什么**：模块 docstring 里的链路（`authorize → decide → enforce → record_pending → forward → sanitize → settle → record_done`）→ `call_tool`（把链路按顺序读一遍，每一步都有注释）→ `_deny`（ADR-0007 的三层形态）→ `_reserve` / `_used`（预算）→ `_record`（证据字段从哪来）→ `read_resource`（ADR-0023 的同形拒绝）。
- **为什么这样切**：
  - **决策只在这一处**：前门与中间件一行决策逻辑都没有（ADR-0005 的分工），所以「这次调用为什么被放行/拒绝」永远只有一个答案来源；
  - **`record_pending` 在转发之前**（ADR-0022）：崩溃也一定留下「有一条 Decision 授权过它」；
  - **拒绝是「返回结果」不是「抛异常」**（ADR-0007 第 2 层）：抛异常会变成协议错误，客户端库可能直接掐掉整个回合——那是一次廉价的 DoS；
  - **`_reserve` 在有金额时才动手**，且没有 mandate 就直接拒（fail-closed），而不是「没约束就放行」；
  - **审批第一轮返回 `InputRequiredResult`**（MRTR），第二轮从 `input_responses` 读回答——重试会重跑整条链，但 `tool_call_id` 是内容摘要派生的，`reserve` 命中同一个 hold，不会重复占额度；
  - **审批的三条通道在 `_resolve_approval` 里分叉**（W4 Step 1.2）：带回答 → 直接判；老客户端 → `approval_unavailable`；第一轮 → 挑战（能 elicitation 走 form，否则只给 `request_state`）；兜底通道的第二轮 → **在网关侧等文件里的决定**。为什么等待不能交给客户端重试？因为 SDK 的 state-only 重试上限是 10 轮、退避 50ms→250ms（≈1.3s），人来不及敲一条 CLI 命令。
- **自测**：① 为什么未知工具是**协议错误**而策略拒绝不是？② `decision=allow` 与 `outcome=tool_error` 为什么能同时出现？③ 被拒的调用为什么要在链上留痕？④ 审批重试那一轮为什么不会重复扣款？⑤ `read_resource` 为什么对「越界」和「不存在」回同一个答案？⑥ 审批的等待预算为什么从**登记时刻**算起，而不是从每一轮请求算起？

## 6. `src/gateway/frontdoor.py` + `upstream.py` —— 前后门

- **读什么**：`GatewayMiddleware.__call__` 的**三类分流**（拦截 / 放行 / 结构化错误）→ `_bearer_token`（下游凭证只用于识别调用方）→ `_meta`（协议事实）→ `channel_for` 把协议事实翻成审批通道名；`upstream.py` 的 `UpstreamRegistry.start`（启动时聚合工具清单、冲突 fail fast）→ `resolve`（按前缀路由）。
- **为什么这样切**：
  - **前门不做决策**：它只把「谁在调用」「客户端能接受什么」翻成执行点认识的输入；放行与拒绝永远只有执行点一个来源（ADR-0005 的分工）；
  - **拦截面是白名单，不是黑名单**：`tools/*` + `resources/read` 走执行点；握手与心跳（`initialize` / `server/discover` / `ping`）与**所有通知**原样放行；其余一律 `-32602`。为什么不能反过来（放行一切、只拦已知的）？那样 I1 会随 SDK 新增方法**悄悄**失效——这条在 W4 Step 1 的验收里真的踩到了：`initialize` 没放行，老客户端连都连不上，而 ADR-0024 要的是双代并存；
  - **通知的判别式是 `ctx.request_id is None`**（SDK 文档给的），不是方法名前缀——SDK 明说 `notifications/initialized` 也要在 `call_next` 里跑，不放行就等于把握手掐断；
  - **审批通道的判断放在前门**：`_meta` 是协议层的东西，执行点不该解析它；前门只交出一个通道名，执行点按通道办事——所以测试可以直接注入通道，不必伪造 `_meta`；
  - **`upstream.py` 是唯一持有 `mcp.client` 的地方**（`tests/test_layering.py` 用 AST 钉住）：这就是 I1 在代码结构上的那一半——别处拿不到上游客户端，旁路不是「不允许」而是「不可能」；
  - **工具名在启动时就定好**（`<alias>__<tool>`），运行期只做前缀路由，冲突在启动时 fail fast（ADR-0008）。
- **自测**：① 为什么 `initialize` 必须放行，而 `resources/list` 必须报错？② 通知为什么不走身份检查？③ 「旁路不可能」与「旁路被禁止」的区别是什么，代码里靠什么成立？④ 审批通道的判断为什么放在前门而不是执行点？⑤ 上游工具清单为什么要在**启动时**聚合而不是每次 `tools/list` 现拉？

## 7. `src/gateway/approval.py` + `cli.py` —— 审批闸门与命令行

- **读什么**：`channel_for`（协议事实 → 通道名，两处 fail-closed）→ `register` / `resume`（幂等与绑定校验）→ `challenge` / `state_only`（两种挑战形状）→ `wait`（durable wait 的那一句）→ `cli.py` 的 `serve` / `chain` / `verify` / `replay` / `approvals` / `approve` / `deny`。
- **为什么这样切**：
  - **三条通道写同一个 JSONL**：CLI 看到的和客户端批准的一定是同一件事——形状只有一处定义（`_append`）；
  - **`request_state` 是客户端给的，必须校验**：规格只说「原样回传」，没说不可伪造；对不上动作指纹或主体就当作没这回事（ADR-0021 的绑定要求）；
  - **幂等登记**：同一（动作指纹, 主体）在未决定期间只有一条，客户端重试不会把 CLI 刷屏；已决定的不复用（单次使用）；
  - **「过期」是显式状态**：`pending()` 只返回未决定**且未过期**的，所以 `register()` 不会复用一条 1.5 小时前的待审批；所有通道的收尾都写 `decision`（`by=human/client/timeout`），否则它会永远挂在待办里。这条是手工验收踩出来的，见 `docs/w4-plan.md`；
  - **调试客户端不放 `src/gateway/`**：`tools` / `call` / `read` 要当 MCP 客户端，而 `src/gateway/` 里只允许 `upstream.py` 持有 `mcp.client`（I1 的结构契约）——所以它们留在 `scripts/play.py`；
  - **argparse 的经典坑**：子解析器会把自己那份默认值写回 namespace，所以 `shared_flags()` 用 `SUPPRESS`——否则 `--psp-mode decline serve` 会被静默改回默认值。
- **自测**：① 为什么「老客户端 + elicitation 广告」也只能拒？② `state_only` 挑战为什么是合法形状？③ 伪造 `request_state` 能造成什么？④ 为什么审计入口（`verify` / `replay`）该是产品的一部分，而调试客户端不是？⑤ 等待预算为什么从**登记时刻**算起？把「过期」当成「还没决定」会同时坏掉哪三件事？⑥ 为什么超时也要落一条 `decision`？
