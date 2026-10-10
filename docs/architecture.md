# 架构（先验架构全文）

> 本文件由 AI 从既有的架构讨论转写（2026-10-05），**内容与讨论结论一致，未改动架构本身**。
> 架构变更流程见 `AGENTS.md`：需充分证据、User 明确认可，并记入 `docs/decisions.md`。

## 1. 定位

一个位于 agent 与 commerce/pay MCP server 之间的策略执行与审计网关：让「agent 能不能调用这个工具、能不能花这笔钱」成为内核里可执行、可留痕、可复算的边界。

## 2. 八条切分理由

1. **网关做成独立 MCP server 进程，不做成库。** 协议边界强制显式处理传输、会话与能力协商；可独立观测与重启；演示直观。代价是转发要自己写（已核实：Python SDK v2 无内置代理，服务端类为 `MCPServer`）。
2. **单一执行点。** 所有被拦截的方法调用（`tools/*`、`resources/*`，含 `prompts/get`；见 ADR-0011）必经 authorize → decide → enforce → forward → sanitize → settle → record，无旁路。这构成可测试不变量 I1：「不存在未经 Decision 就放行的方法调用」。
3. **决策是一等公民。** `Decision = allow | deny(reason_code) | require_approval(approval_id) | transform(patch)`。策略输出它、证据记录它、重放比对它。
4. **确定性核心 + 副作用边缘。** 策略求值、预算算术、脱敏、证据哈希为纯函数；时间、随机、上游响应、用户审批作为被记录的输入。这是可复算重放的前提。
5. **两阶段预算。** reserve → commit / release，配单文件 SQLite（WAL）+ 进程内单写者，DB 约束兜底。
6. **证据双形态。** 全局哈希链（每次调用一条，含 deny）+ 运行证据包（JSONL + manifest + Merkle 根）。
7. **审批是 durable wait。** 待审批记录落库，重启后仍有效；投递通道为 CLI 与 MCP elicitation。
8. **可观测性收敛到一个模块。** OTel MCP 语义约定仍在演进（semconv 已有 deprecated 条目），属性映射集中在 `telemetry/mapping.py`。

## 3. 架构示意

```mermaid
flowchart TB
  D["agent driver<br/>薄循环 · MCP client"]
  subgraph GW["Gateway 进程（控制面）"]
    F["frontdoor<br/>Streamable HTTP + stdio<br/>会话 · 能力协商"]
    E["enforce<br/>唯一执行点"]
    P["policy<br/>YAML→决策"]
    B["budget<br/>reserve/commit/release"]
    S["sanitize<br/>注入筛查 · PII 脱敏 · audience 过滤"]
    A["approval<br/>durable 待审批"]
    V["evidence<br/>哈希链 + 证据包"]
    T["telemetry<br/>OTel 映射"]
  end
  M["mock merchant MCP"]
  PP["mock PSP MCP + 故障注入"]
  DB["SQLite<br/>ledger · budget · approvals"]
  C["CLI<br/>run / verify / replay / approve"]

  D -->|"tools/call"| F --> E
  E --> P
  E --> B
  E --> A
  E -->|"放行"| M
  E -->|"放行"| PP
  M --> S
  PP --> S
  S -->|"净化后结果"| D
  B --> DB
  A --> DB
  V --> DB
  E --> V
  E --> T
  C <--> A
  C --> V
```

## 4. 分层职责

| 层 | 组件 | 职责 |
| --- | --- | --- |
| 客户端 | agent driver（薄循环） | 以 MCP client 身份连网关，产出 tools/call |
| 网关前门 | `frontdoor/` | MCP server 端点（Streamable HTTP + stdio）、会话管理（Mcp-Session-Id）、能力协商透传 |
| 判定 | `policy/` | 策略加载（YAML→pydantic→内存决策结构）、求值、Decision 构造 |
| 执行 | `enforce/` | 单一执行点（覆盖 tools/* 与 resources/*），落实 Decision；deny-by-default；唯一持有上游客户端 |
| 预算 | `budget/` | reserve/commit/release、幂等键、TTL 回收、重启对账 |
| 结果处理 | `sanitize/` | 注入筛查、PII 脱敏、audience 过滤 |
| 审批 | `approval/` | durable 待审批队列、CLI 与 elicitation 投递 |
| 证据 | `evidence/` | 哈希链账本、证据包、verify 与 replay |
| 观测 | `telemetry/` | OTel span/指标 + 属性映射 |
| 上游 | `mcp_servers/` | mock merchant、mock PSP、故障注入 |
| 存储 | `store/` | SQLite（ledger、budget、approvals） |

## 5. 一次被拦截请求的时序（以 tools/call 为例）

1. 前门收到 JSON-RPC，解析出 agent 身份与会话
2. `policy.evaluate(agent, tool, args)` → Decision
3. deny：写证据（含 reason_code）→ 返回结构化错误（不转发）
4. require_approval：落库待审批 → 通知 → 有界等待 → 转为 allow 或 deny
5. allow：`budget.reserve` → 转发上游 → 取结果
6. `sanitize(结果)`：注入筛查 / PII 脱敏 / audience 过滤
7. `budget.commit`（或失败时 release）
8. 写证据（decision、policy 版本与摘要、预算前后、上游请求与响应摘要、脱敏标记）+ 发 span
9. 返回结果给 agent

> `resources/read`、`resources/list`、`resources/subscribe` 走同一条链；差异在步骤 6 与 7——`resources/list` 需要按 URI 过滤并**重签游标**（ADR-0013），订阅通知需要在流上做同等检查。

## 6. 四条可测试不变量（简历上的数字来源）

| 编号 | 不变量 | 测法 |
| --- | --- | --- |
| I1 | 不存在未经 Decision 就放行的方法调用（覆盖 tools/* 与 resources/*） | 执行点是唯一入口；插桩测试 + import-linter 分层契约证明无旁路 |
| I2 | 并发安全：`sum(committed) + sum(active holds) ≤ limit` | N=50 并发压测 |
| I3 | 防篡改：篡改任一记录 verify 失败 | 翻转一个字节必须被检出 |
| I4 | 可复算：同一证据包重放决策序列逐条一致 | 重放确定性率 |

## 7. 与其它文档的关系

- 三个设计决定（策略语言边界、预算语义、证据语义）的取舍与备选写在 `docs/decisions.md`。
- 威胁与挂点的对应写在 `docs/threat-model.md`。
- 分阶段计划写在 `docs/w1-plan.md`。
