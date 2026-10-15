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

## 4. `src/gateway/evidence/` —— 证据链与证据包（待填）

## 5. `src/gateway/enforce.py` —— 执行点（待填，W3 Step 4）

## 6. `src/gateway/frontdoor.py` + `upstream.py` —— 前后门（待填）

> 每填一节，都要有「读什么 / 为什么这样切 / 自测」三块，与上面两节保持同一格式。
