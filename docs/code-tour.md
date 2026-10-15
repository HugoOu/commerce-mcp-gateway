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

## 3. `src/gateway/budget/` —— 预算原子扣减（待填）

- **读什么**：整个模块就在 `__init__.py` 里（类型与实现放一起，因为账本只做这一件事）。顺序：`SCHEMA`（表结构与那条 `UNIQUE`）→ `reserve`（条件更新与幂等）→ `commit` / `release`（终结与幂等）→ `balance` / `reclaim_expired` → 内部工具 `_transaction` / `_used_cents`。
- **为什么这样切**：
  - **金额以「分」入库**（整数）：浮点求和会在边界上让 `≤ limit` 假成立，而 I2 正是靠这个不等式成立的；
  - **`BEGIN IMMEDIATE` 把「读余额 → 判断 → 写 hold」关进同一个事务**：这就是 ADR-0002 点名的在途窗口（两笔 30 元并发对 100 元上限会都看到 100 可用）；
  - **幂等靠 `UNIQUE(session_id, tool_call_id)` 兜底**：并发重复提交也只会插入一条；
  - **上限只能收紧**（`_upsert_session` 取 min）：与 ADR-0020「策略只能收紧」是同一条原则；
  - **`now` 由调用方传入**：实现里不读时钟，重放才可复算（architecture 第 4 条）。
- **自测**：① 为什么 `reserve` 失败时必须「不留下半条 hold」？② 同一幂等键重试时返回的是哪一条 hold，为什么这样设计？③ 释放过的 hold 还占额度吗？④ 50 个线程抢 100 元上限、每笔 30 元，应该有几笔成功，为什么是「恰好」而不是「最多」？⑤ 为什么重复 `commit` 要幂等，而 `commit` 之后再 `release` 要拒绝？

## 4. `src/gateway/evidence/` —— 证据链与证据包（待填）

- **读什么**：`EvidenceEntry`（链上的单元）→ `_entry_hash` / `_merkle_root`（两个哈希怎么算）→ `ChainLedger.record_pending` / `record_done`（写入顺序）→ `verify`（能检出什么）→ `replay`（前置条件与决策重构）。
- **为什么这样切**：
  - **`prev_hash` 参与自己的哈希**：改了任何一条，它后面所有条目的哈希都会变，链因此不可局部伪造；
  - **写入顺序是「链 → 包 → manifest」**：中途崩溃会让包比链少一条，`verify` 会报出来——宁可报错，也不要静默地让账本与证据对不上；
  - **链与包互为见证**：包内截断靠 manifest 的条数与 Merkle 根发现，链被截断靠「包的叶哈希必须都能在链上找到」发现；
  - **`replay` 的评估器是参数**：证据层不 import 策略（层次干净），Step 4 由执行点把策略闭包传进来；
  - **诚实边界**：把链与全部 manifest 一起重写的对手检不出（那需要外部锚点或见证，威胁模型 R5 本版未采纳）。
- **自测**：① 为什么短摘要（16 位十六进制）不能拿来做链？② 删掉运行包的最后一条，`verify` 靠什么发现？③ 只删链的最后一条，为什么 `verify()` 也能发现？④ 重放前置条件里为什么需要 `policy_digest` 与预算前后？⑤ 「verify 通过」保证了什么、又没保证什么？

## 5. `src/gateway/enforce.py` —— 执行点（待填，W3 Step 4）

- **读什么**：模块 docstring 里的链路（`authorize → decide → enforce → record_pending → forward → sanitize → settle → record_done`）→ `call_tool`（把链路按顺序读一遍，每一步都有注释）→ `_deny`（ADR-0007 的三层形态）→ `_reserve` / `_used`（预算）→ `_record`（证据字段从哪来）→ `read_resource`（ADR-0023 的同形拒绝）。
- **为什么这样切**：
  - **决策只在这一处**：前门与中间件一行决策逻辑都没有（ADR-0005 的分工），所以「这次调用为什么被放行/拒绝」永远只有一个答案来源；
  - **`record_pending` 在转发之前**（ADR-0022）：崩溃也一定留下「有一条 Decision 授权过它」；
  - **拒绝是「返回结果」不是「抛异常」**（ADR-0007 第 2 层）：抛异常会变成协议错误，客户端库可能直接掐掉整个回合——那是一次廉价的 DoS；
  - **`_reserve` 在有金额时才动手**，且没有 mandate 就直接拒（fail-closed），而不是「没约束就放行」；
  - **审批第一轮返回 `InputRequiredResult`**（MRTR），第二轮从 `input_responses` 读回答——重试会重跑整条链，但 `tool_call_id` 是内容摘要派生的，`reserve` 命中同一个 hold，不会重复占额度。
- **自测**：① 为什么未知工具是**协议错误**而策略拒绝不是？② `decision=allow` 与 `outcome=tool_error` 为什么能同时出现？③ 被拒的调用为什么要在链上留痕？④ 审批重试那一轮为什么不会重复扣款？⑤ `read_resource` 为什么对「越界」和「不存在」回同一个答案？

## 6. `src/gateway/frontdoor.py` + `upstream.py` —— 前后门（待填）

> 每填一节，都要有「读什么 / 为什么这样切 / 自测」三块，与上面两节保持同一格式。
