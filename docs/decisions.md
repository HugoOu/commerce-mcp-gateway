# 架构决策记录（ADR）

> 格式：决策 / 备选 / 为什么不选 / 后来证伪了什么。
> 分工：AI 可以帮你查既有实践，判断必须由你写。

## ADR-0001 策略语言边界

- 决策（上一轮已定）：YAML + 固定算子集 + pydantic 校验，编译成内存决策结构。
- 备选：TODO(你)
- 为什么不选：TODO(你)

## ADR-0002 预算语义

- 决策（上一轮已定）：reserve 然后 commit/release，SQLite 事务 + 幂等键 + TTL 回收。
- 备选：TODO(你)
- 为什么不选：TODO(你)

## ADR-0003 证据语义

- 决策（上一轮已定）：全局哈希链（每次调用一条，含 deny）+ 运行证据包（含 Merkle 根）。
- 备选：TODO(你)
- 为什么不选：TODO(你)

## ADR-0004 网关传输选择（Step 3 决定）

- 决策：TODO(你)
- 依据：TODO(你)

## ADR-0005 SDK 代理能力与转发实现方式（Step 3 决定）

- 决策：TODO(你)
- 依据：TODO(你)


## ADR-0006 审批闸门机制（候选，待决策）

- 背景：2026-07-28 起 `tools/call` 可用 `resultType: "input_required"` 走 mrtr 机制暂停等人；架构第 7 条目前写的是「审批是 durable wait，投递通道为 CLI 与 MCP elicitation」。
- 备选：TODO(你)（协议原生 input_required/mrtr；自造 CLI 通道；两者并用）
- 决策：TODO(你)
- 为什么不选：TODO(你)
- 注：会触及先验架构，按 AGENTS.md 需要证据 + User 认可。

## ADR-0007 策略拒绝的返回形态（候选，待决策）

- 背景：`Decision.deny` 落到协议层有两种形态——工具执行错误（`isError: true`，模型可自适应）与 JSON-RPC 协议错误（模型无法自适应）。
- 备选：TODO(你)
- 决策：TODO(你)
- 为什么不选：TODO(你)
- 提示：建议按「可否自适应 / 是否成为攻击预言机 / 会话是否需要存活」三类分别定；完整理由写进证据账本，线上响应只回最小化投影。

## ADR-0008 工具名冲突消歧（候选，待决策）

- 背景：工具名唯一性只在单个 server 内成立；网关聚合 merchant 与 PSP 会撞名；规格建议加 server 标识前缀，且不能依赖 serverInfo 的 name。
- 备选 / 决策 / 为什么不选：TODO(你)

## ADR-0009 是否采用 x-mcp-header（候选，待决策）

- 背景：该扩展把参数镜像进 HTTP 头供中间人使用；规格禁止镜像敏感参数。
- 备选 / 决策 / 为什么不选：TODO(你)

## ADR-0010 tools/list 的过滤与缓存语义（候选，待决策）

- 背景：tools/list 结果可因授权不同而不同（可做工具隐藏），并带 ttlMs / cacheScope；若上游声明 public 而网关的过滤结果依赖 agent 身份，直接沿用会越权泄露。
- 备选 / 决策 / 为什么不选：TODO(你)
