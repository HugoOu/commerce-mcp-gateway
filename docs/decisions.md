# 架构决策记录（ADR）

> English index: [decisions.en.md](decisions.en.md)


> 格式：决策 / 备选 / 为什么不选 / 后来证伪了什么。
> 本文件记录关键设计决策、备选与依据；部分内部过程文档未随公开仓库发布，文中指向内部计划的链接仅作历史注记。

## ADR-0001 策略语言边界（已采纳 B + 段感知规则，2026-10-10）

- 决策（已定，2026-10-10）：采纳 **B 方案**——新增 URI 专用算子（前缀 + 通配，glob 语义），并采纳段感知规则：`*` 默认不跨 `/`，需要跨段时显式写 `**`。策略 schema 增加与 `tools:` 并列的 `resources:` 段。
- 备选：A 复用 glob 不新增算子；C 引入正则；D 策略改为 Python 函数。
- 为什么不选 A：与 B 表达力等价，但让 URI 与工具名共用同一算子，语义会混（工具按名字寻址、资源按 URI 寻址）。
- 为什么不选 C：正则难静态分析、易灾难性回溯，与「不做通用 DSL」冲突；Cedar / AWS IAM / K8s 三处先例都止步于 glob。
- 为什么不选 D：不可审计、不可哈希、不可重放，原决策已否掉。
- 待澄清（2026-10-09）：现有算子集是为「工具名 + 参数」设计的，而 resources 用 URI 标识。若要表达「允许读 `merchant://catalog/*` 但禁止 `merchant://orders/*`」，需要扩算子（URI 前缀或模式匹配）或换表达方式——这会触及「绝不做通用 DSL」的边界，需在此记录取舍。

- 候选备选（AI 提议，2026-10-10，待你选）：
  - A 复用 glob 做 URI 匹配（`match_uri: "merchant://catalog/*"`），不新增算子
  - B 新增 URI 专用算子（前缀 + 通配，仍是 glob 语义），命名更清晰
  - C 引入正则（表达力强，但难静态分析、易灾难性回溯，与「不做通用 DSL」冲突）
  - D 策略改为 Python 函数（表达力无限，但不可审计、不可哈希、不可重放，原决策已否掉）
  - 建议：A 或 B，并把边界写成「URI 匹配只支持前缀与 glob，不支持正则」；策略 schema 增加与 `tools:` 并列的 `resources:` 段，而不是把 URI 塞进工具名匹配
  - 调研证据（2026-10-10，供决策）：
    - Cedar（现代授权语言）：`like` 通配算子只支持 `*`（匹配 0 个或多个任意字符），字面星号用反斜杠转义；示例 `location like "s3:*"`。没有正则。
    - AWS IAM：资源 ARN 支持 `*` 与 `?`，建议在冒号分隔的段内使用；段末的 `*` 可跨段边界；服务段不允许通配 → 段感知的 glob。
    - Kubernetes RBAC：资源名用精确列表或 `*`；对 nonResourceURLs 只支持后缀 glob（`*` 作后缀）→ 对 URL 类标识，业界标准是前缀 / 后缀 glob，不是任意正则。
    - 结论：B 方案与三处先例一致。建议再加一条段感知规则：`*` 默认不跨 `/`，需要跨段时显式写 `**`——否则 `merchant://*` 会意外匹配 `merchant://orders/1`。

## ADR-0002 预算语义（已采纳，2026-10-10）

- 决策（上一轮已定）：reserve 然后 commit/release，SQLite 事务 + 幂等键 + TTL 回收。
- 证据（2026-10-10 调研）：
  - Stripe 的 manual capture：authorization 先占用资金、之后 capture 才真正扣款（Authorizing a payment guarantees the amount by holding it on the customer payment method）；charge 上的 `capture_before` 标明授权何时过期——与 reserve → commit/release + TTL 回收逐条对应。
  - Stripe 的幂等键：safely retrying requests without accidentally performing the same operation twice；执行开始后保存首次请求的状态码与响应体，同一 key 的重试返回已保存的响应（包括 500）——与幂等键 (session_id, tool_call_id) 对应，并提示重放要忠实，失败也要原样重放。
  - 对照：纯内存计数器重启即丢；仅由账本推导余额会在在途窗口超支（两笔 30 元并发对 100 元上限，会都看到 100 可用）。
- 推荐：保持 reserve → commit/release，落 SQLite（WAL）+ 进程内单写者、DB 约束兜底；reserve 用 IMMEDIATE 事务与条件更新；幂等键取 (session_id, tool_call_id)；上游失败或超时 release；启动时回收超过 TTL 的 hold 并对账；记录 hold 生命周期以便复算。
- 备选：a) 纯内存计数器；b) 仅由账本推导余额；c) 外部预算服务。
- 决策（已定，2026-10-10）：采纳推荐方案——reserve → commit/release，SQLite（WAL）+ 进程内单写者 + DB 约束兜底；幂等键取 (session_id, tool_call_id)；TTL 回收与启动对账。
- 为什么不选 a：纯内存计数器重启即丢，并发下也不安全。
- 为什么不选 b：仅由账本推导余额会在在途窗口超支——两笔 30 元并发对 100 元上限会都看到 100 可用，这正是重复花费窗口，三类里最危险。
- 为什么不选 c：外部预算服务对 demo 是过度设计，引入额外运维成本。
- 补充（2026-10-16，**一次「查错并回退」的记录**，比结论本身更值得留）：我曾判断「绑定声明了金额字段、参数里却没给」是 `_reserve` 里的 fail-open 洞（省略 `amount` 的 capture 能绕开预算），并加了一个 `amount_missing` 的拒绝分支。**测试当场证明判断是错的**：策略引擎的 `_check_constraints` 早就在「绑定声明了金额字段、参数里没有金额」时返回 `amount_unreadable`（fail-closed），执行点的 `_reserve` 根本看不到这种输入——新加的分支是**死代码**。已回退该改动，改为在 `tests/test_adversarial.py` 里钉一条用例记录**真实**的拦截点（引擎），并把 `_reserve` 那一行标注成「走不到，只为局部不变量自明」。
  这条留档的价值：**判定只有一处**（ADR-0005 的分工）不是口号——同一个问题在两处各写一遍，就会产出「两处判定、一处不可达」这种噪音。更重要的提醒：**安全结论要以跑出来的行为为准，不能以读一段代码为准**。
  **哪些调用该占额度**是另一个问题，见 ADR-0027（候选）。

## ADR-0003 证据语义（已采纳，2026-10-10）

- 决策（上一轮已定）：全局哈希链（每次调用一条，含 deny）+ 运行证据包（含 Merkle 根）。
- 证据（2026-10-10 调研）：
  - AWS CloudTrail 日志完整性校验：SHA-256 哈希 + SHA-256/RSA 签名；能判断日志文件是否被删除或修改；为每个日志文件算一个哈希，并每小时生成一个 digest file 引用这些文件与其哈希——与「每次调用一条记录 + 每次运行的证据包」逐条对应。
  - RFC 6962（Certificate Transparency）：日志是 append-only 且公开可审计，append-only 性质用 Merkle Tree 实现，可证明某个版本与之前版本一致；提供 Merkle Audit Path（包含证明）。
  - Sigstore Rekor：把签名元数据记入不可变记录，提供 inclusion proof 与日志完整性校验。
- 推荐：保持双形态——全局哈希链（每条记录含 prev_hash，防删除与改序）+ 运行证据包（JSONL + manifest，manifest 带该次运行记录的 Merkle 根，可做包含证明）；规范化用键排序（JCS 思路）；非确定性字段作为输入记录而非重算；提供 verify（重算链与根）与 replay（重放判定）两条命令。
- 备选：a) 只做每次调用的链；b) 只做每次运行的包；c) 用数据库事务 / WAL 替代。
- 决策（已定，2026-10-10）：采纳推荐方案——全局哈希链（含 prev_hash）+ 运行证据包（manifest 带 Merkle 根），并提供 verify 与 replay 两条命令。
- 为什么不选 a：只做链不便携带与局部验证。
- 为什么不选 b：只做包缺少跨运行单调性，整段删除不可检出。
- 为什么不选 c：数据库事务 / WAL 只保证一致性、不保证防篡改，也不能对外携带——审计需要可交付的证据形态（CloudTrail / RFC 6962 的形态）。

## ADR-0004 网关传输选择（已采纳，2026-10-14）

- 背景：网关终结两条连接——对 agent 是 server，对 mock 是 client；两条边可以选不同传输。
- 证据（2026-07-28 原文核实，2026-10-14）：
  - 每个 JSON-RPC 请求或通知**各自是一次 HTTP POST**；客户端 MUST 带 `Accept`（同时列 `application/json` 与 `text/event-stream`）与**请求元数据头**；服务端对每个请求选择回单个 JSON 或一条**限定于该请求的 SSE 流**；**最终 JSON-RPC 响应会终止该流**。
  - 服务端主动推送走 `subscriptions/listen` 建立的**长连接**（不再是 GET 独立流）。
  - 旧形态（`Mcp-Session-Id` 会话、GET 独立流、`Last-Event-ID` 恢复）属于 2025-03-26～2025-11-25，**本版明确不含**；规格给了收到旧客户端 GET / DELETE 时的 SHOULD 处理方式。
  - 规格只对**客户端**给了倾向（能支持 stdio 就支持），对 server 侧没有倾向——所以网关选型要按架构决定。
  - 架构切分理由 1 要求网关是独立进程（可观测、可重启）——stdio 的 server 通常由 client 拉起为子进程，生命周期绑在 client 上，与此冲突。
  - 既有实现（2026-10-14 调研）：`open-webui/mcpo`（MCP-to-OpenAPI proxy，把 stdio 的 MCP server 暴露成 HTTP 端点）、`IBM/mcp-context-forge`（在任意 MCP / A2A / REST 前面暴露统一端点）、`agentgateway/agentgateway`（面向 AI agent 与 MCP 的代理）、`openziti/mcp-gateway`（上游支持 stdio 或 HTTP）、`asashiki/mcp-switch`（把本地 stdio 与远程 HTTP 的 MCP server 聚合在一个端点后面）。共同模式：**上游混合传输、下游统一端点**——与本条推荐一致。
- 推荐：**两条边用不同传输**——对 agent 暴露 **Streamable HTTP**（独立进程、多客户端、可观测、可重启）；对 mock 用 **stdio**（作为子进程拉起，零端口、零网络配置、最容易复现）。
- 备选：a) 两侧都用 HTTP；b) 两侧都用 stdio；c) 对 agent 也用 stdio。
- 为什么不选 c：与「独立进程」冲突，且难观测、难重启。
- 为什么不选 a：mock 走 HTTP 需要额外端口与进程管理，对 demo 是纯成本。
- 为什么不选 b：stdio 不便于多客户端与独立观测。
- 决策（已定，2026-10-14）：采纳推荐方案——对 agent 暴露 Streamable HTTP，对 mock 用 stdio。

## ADR-0005 SDK 代理能力与转发实现方式（已采纳，2026-10-15）

- 背景：网关需要把请求转发到上游 MCP server，同时在下游做拦截与合成。SDK 没有内置代理（2026-10-04 已核实），所以要分别决定「转发」与「拦截」用什么机制。

- 证据一：SDK 公开 API 面（2026-10-15 实测，SDK 2.3.0）

| 面 | 可用件 | 关键点 |
| --- | --- | --- |
| 下游（server） | `MCPServer`：`tool` / `add_tool` / `resource` / `prompt` / `run(transport=stdio, sse, streamable-http)` | 一个类同时支持三种传输 |
| 下游挂载 | `streamable_http_app` / `sse_app` / `session_manager` | 可直接挂进自己的 ASGI 应用（加自己的路由与生命周期） |
| 下游拦截 | `MCPServer.middleware`（`list[ServerMiddleware]`） | `(ctx, call_next) -> result`，在**每个入站请求/通知的最早处**执行（早于校验、查找、握手）；可读 `ctx.method` / `ctx.params`（未校验的原始参数）；可 `call_next(replace(ctx, params=...))` 改写；可**在 `call_next` 之前抛错来否决**（含握手）；通知的返回值被丢弃；`ctx.request_id is None` 可区分通知 |
| 下游鉴权 | `token_verifier` / `auth_server_provider` 参数 + `AuthenticationMiddleware` / `RequireAuthMiddleware` / `BearerAuthBackend` | SDK 自带 resource server 侧的 bearer 校验中间件 |
| 观测 | `OpenTelemetryMiddleware`（`server/_otel.py`），**默认已注册** | SDK 已在发 OTel span——我们的 `telemetry/` 应是配置与属性映射，不是从零造 |
| 上游（client） | `ClientSession`：`initialize` / `list_tools` / `call_tool` / `read_resource` / `subscribe_resource` / `dispatch_input_request` / `discover` / `validate_tool_result` | `dispatch_input_request` 是 MRTR 客户端钩子；`validate_tool_result` 说明输出校验归消费者 |
| 上游传输 | `stdio_client(StdioServerParameters)`；`streamable_http_client(url)`（**返回两个值**） | 与 ADR-0004「对 mock 用 stdio」一致 |
| 版本 | `HANDSHAKE_PROTOCOL_VERSIONS` 到 `2025-11-25`；`MODERN_PROTOCOL_VERSIONS` 只有 `2026-07-28` | 默认协商到握手那代；现代版走另一条路径（W2 验证项） |
| 错误形状 | 工具抛异常 → `isError=True` + `resultType=complete`；异常消息**不进**载荷 | 验证 ADR-0007；可操作的拒绝理由必须自己构造 |
| 结构化输出 | 默认不填 `structuredContent`（结果是文本 JSON）；需 `@mcp.tool(structured_output=True)` | `sanitize/` 要同时覆盖两种形态 |
| 字段命名 | SDK 用 snake_case（`is_error` / `structured_content` / `result_type`），线上别名 camelCase | 打印与比较用 `model_dump(by_alias=True)` |
| 中间件短路（2026-10-15） | `ServerMiddleware` 不调 `call_next` 直接返回 `BaseModel`/`dict` → **自己拥有结果**（`_dump_result` 只做 `by_alias` 序列化，管线不再修补）；抛 `MCPError`（或返回 `ErrorData`）→ 协议错误 | 拒绝要**返回** `CallToolResult(is_error=True)`，不能靠抛异常 |
| 工具层错误分层（2026-10-15） | `ToolError`（可预期，含未知工具名与参数校验失败）→ `isError=True` + 原文进 `content`；`UnexpectedToolError`（崩溃）→ `isError=True` 但只给 `Error executing tool <name>`；`MCPError` → 协议错误 | SDK 自己的分层与 ADR-0007 同构；我们的 deny 走第一条 |
| resources 错误面（2026-10-15） | 没有 `isError` 通道：`ResourceError` → `-32603` 带原文（`ResourceNotFoundError` → `-32602`）；其他异常 → 只给通用消息 | resources 的拒绝形态是 ADR-0007 未覆盖的缺口 → ADR-0023（候选） |

- 证据二：既有实现的通行做法（2026-10-15 调研）
  - `TBXark/mcp-proxy`（Go）：**单个 HTTP 入口聚合多个 MCP server**；上游支持 `stdio` / `sse` / `streamable-http` 三种客户端类型；并且**由代理持有并刷新下游 OAuth 令牌**（authorize once, hold and refresh for every caller）。
  - `open-webui/mcpo`（Python）：把 stdio 的 MCP server 暴露成 HTTP 端点——Python 生态里形态最接近的一个。
  - `IBM/mcp-context-forge`、`agentgateway/agentgateway`、`openziti/mcp-gateway` 等聚合器：都是「上游混合传输 + 下游统一端点」。
  - 共同结论：**没有现成的 Python 代理封装；所有实现都自己写转发，并且自己持有上游凭证**——与 ADR-0019 的推荐一致。

- 推荐：
  1. **下游**用 `MCPServer`，并以 `streamable_http_app` 挂进我们自己的 ASGI 应用；**拦截点用 `ServerMiddleware`**，但决策逻辑仍在 `enforce/`——中间件只负责把请求交给执行点、落实 Decision、把结果交回。
  2. **上游**用 `ClientSession` + `stdio_client`（ADR-0004），**转发逻辑自己写**（在工具的 handler 里调用上游 session）；不引入 Go 生态的代理。
  3. **凭证**由网关自己持有（ADR-0019）；`TBXark/mcp-proxy` 的「持有并刷新」是可直接借鉴的先例。
  4. **鉴权**优先复用 SDK 的 `token_verifier` 与 `AuthenticationMiddleware`，`identity/` 在其上产出 `AuthorizationBasis` 与 `authz_basis`。
  5. **观测**复用默认注册的 `OpenTelemetryMiddleware`，`telemetry/` 只做属性映射与补充。
  6. **W2 第一个技术验证项**：确认现代协议版（`2026-07-28`）能否协商成功——它决定 ADR-0006 的 `input_required` 合成能否照设计落地；若不可达，按 AGENTS.md 的「实现期规则」停下来提 ADR。
  7. **拒绝的落地方式**：在 `ServerMiddleware` 里短路**返回** `CallToolResult(is_error=True)`（可自适应层与硬安全层，见 ADR-0007）或抛 `MCPError`（结构性层）；崩溃类失败照 SDK 的 `UnexpectedToolError` 语义只给通用消息，原文进日志与证据账本。

- 备选：a) 只用 SDK 的 server、不做拦截；b) 纯中间件实现（不自己写转发）；c) 引入现成代理（Go 的 mcp-proxy 或 Python 的 mcpo）。
- 为什么不选 a：没有拦截点就没有这个项目。
- 为什么不选 b：中间件只覆盖下游一个方向；上游凭证、净化、证据都需要自己的转发路径。
- 为什么不选 c：跨语言构建链（Go）或功能不匹配（mcpo 面向 OpenAPI 暴露），且我们要学的是控制面本身。
- 决策（已定，2026-10-15）：采纳推荐方案 1–7——下游用 `MCPServer` + `streamable_http_app` 挂进自己的 ASGI 应用，**拦截点用 `ServerMiddleware`**（决策逻辑仍在 `enforce/`，中间件只负责把请求交给执行点、落实 Decision、把结果交回）；上游转发自己写（`ClientSession` + `stdio_client`，ADR-0004）；凭证自持（ADR-0019）；鉴权复用 SDK 的 `token_verifier` / `AuthenticationMiddleware`；观测复用默认注册的 `OpenTelemetryMiddleware`；W2 第一个技术验证项是现代协议版（`2026-07-28`）的协商；拒绝靠中间件短路**返回** `CallToolResult(is_error=True)`，不靠抛异常。
- 依据：本页证据一（SDK 2.3.0 公开 API 面，2026-10-15 实测）与证据二（既有实现调研）；实测细节见 `docs/w1-plan.md` Step 3 的「实测事实」表。

- 实现注记（2026-10-18）：证据一里「`sanitize/` 要同时覆盖两种形态」曾只做到 `content`，漏了 `structuredContent`；F8 的证据里 `suspected_adversarial=0` 把它照了出来。现已递归扫描 `structuredContent` 的字符串叶子并补 PII 脱敏与回归测试（`tests/test_sanitize.py`）。这是**补实现**，不是改决策。


## ADR-0006 审批闸门机制（已采纳，2026-10-15 细化）

- 背景：2026-07-28 起 `tools/call` 可用 `resultType: "input_required"` 走 mrtr 机制暂停等人；架构第 7 条目前写的是「审批是 durable wait，投递通道为 CLI 与 MCP elicitation」。
- 证据：
  - 规格给了两条 in-band 通道：`elicitation`（客户端能力）与 `input_required` + mrtr（工具调用可暂停等人）；且 Servers MAY respond to tools/call with an InputRequiredResult——网关作为 agent 的 server，可以**自己合成** input_required 来做审批，不必等上游发起。
  - 既有实现（2026-10-10 调研）：
    - node9-proxy：危险动作 held for review，审批就在对话里（inline review），也可走团队审批人（dashboard / Slack）；明确 held action 在你回答前绝不执行（fail-closed）。
    - cordum：策略判定里有 `REQUIRE_APPROVAL` 这一类；且**不信任审批记录本身**——要求同一租户下已 resolved 的审批审计事件、`approval_ref` 与 `action_hash` 同时匹配，光有审批请求事件不算。
  - 规格（2026-07-28 basic/patterns/mrtr，2026-10-15 核实）：**MRTR 是本版新引入的模式，取代旧的 server-initiated requests，是破坏性变更**；`roots/list`、`sampling/createMessage`、`elicitation/create` 这类 server→client 请求 **MUST** 走 MRTR（即出现在 `input_required` 的 `inputRequests` 里）；`requestState` 是**不透明字符串**，client **MUST NOT** inspect / parse / modify 或对其内容做任何假设；MRTR 的设计目标之一是**不需要跨实例共享存储、也不需要带状态的负载均衡**。
  - 架构影响：网关作为 server 必须支持 MRTR 才能转发/合成 server→client 请求，作为 client 必须处理上游返回的 `input_required`；`inputRequests` 是 **map**，一次响应可携带多个请求（如同时要 elicitation 与 sampling），所以 frontdoor 需要扇出/扇入。
- 推荐：默认走**协议原生**——网关自己合成 `input_required`，**审批的暂停状态优先放进 `requestState`**（协议刻意把状态推给客户端持有），只有需要跨重启恢复的审批才落库（SQLite）并**安全绑定到会话/用户**；CLI 作为兜底通道；审批必须**绑定动作指纹**（参数摘要 + 策略版本）且单次使用；等待期间动作绝不执行（fail-closed）；超时默认拒绝。
- 备选：a) 纯协议原生（input_required / elicitation）；b) 纯自造 CLI 通道；c) 两者并用。
- 决策（已定，2026-10-15 细化）：采纳推荐方案——协议原生 `input_required` + CLI 兜底；**暂停状态优先放进 `requestState`**（MRTR 刻意把状态推给客户端），只有需要跨重启恢复的审批才落库并安全绑定会话/用户；审批绑定动作指纹且单次使用；等待期间动作绝不执行（fail-closed）；超时默认拒绝。
- 为什么不选 a：纯协议原生依赖客户端支持 elicitation / mrtr，通用性差。
- 为什么不选 b：纯自造通道不可被通用 MCP 客户端使用，且无法复用协议的状态语义。
- 注：会触及先验架构，按 AGENTS.md 需要证据 + User 认可。
- 补充（2026-10-16，W4 Step 1.2 落地）：**通道选择由入站 `_meta` 判**，三档且两处 fail-closed——
  - 老客户端（非现代版，或没有版本号）→ `approval_unavailable`：它读不懂 `input_required`，问它等于发一个它解析不了的结果；
  - 现代版 + 广告了 `elicitation` → form 模式 challenge（主通道）；
  - 现代版 + 没广告 → 返回**只带 `request_state`** 的 `InputRequiredResult`，客户端退避重试，网关在**第二轮里**等文件里的决定。
  两条实测依据：① SDK **只在真的注册了回调时**才广告 elicitation 能力（`ClientSession._build_capabilities`），所以能力广告可信；② state-only 重试上限是 10 轮、退避 50ms→250ms（≈1.3s），人来不及敲 CLI 命令——所以**等待必须放在网关侧**，等待预算从登记时刻算起。
  另外收紧两处：`request_state` 由客户端回传，必须核对**动作指纹 + 主体**（ADR-0021 的绑定要求）；`register()` 幂等、已决定的不复用（单次使用）。

## ADR-0007 策略拒绝的返回形态（已采纳，2026-10-10）

- 背景：`Decision.deny` 落到协议层有两种形态——工具执行错误（`isError: true`，模型可自适应）与 JSON-RPC 协议错误（模型无法自适应）。
- 证据：
  - 规格：工具执行错误 = `isError: true` + content 载荷（模型可读）；协议错误 = JSON-RPC error（通常由客户端库处理）。
  - 既有实现（2026-10-10 调研）：JanuScope 走**事前告知**——把策略信息体现在工具元数据里，助手在调用前自我审查（README 记录 Copilot 主动拒绝 UPDATE、拒绝取邮箱），而不是被拒之后才纠正。
  - 对照：HTTP 世界对授权拒绝用 403 硬错误，但那是给人写的客户端；agent 场景需要模型可读的反馈才能继续。
- 推荐：三层并用（事前策略上下文 / 事中可自适应拒绝 / 硬拒绝最小化 + 结构性错误用协议错误），详见下方「建议方案」表。
- 事前告知的范围（2026-10-10 收紧，回应「会不会泄露决策边界被反编译」）：只暴露**能力与类别**，绝不暴露**参数与阈值**——可以说「这个工具需要人工审批」「这一列是 PII」「这个面是只读的」，不可说「上限是 50 美元」「白名单含 m1/m2」。
  - 判断依据：能收紧的边界即使被知道也无法被绕过（知道要审批并不能避免审批）；可搜索的阈值一旦泄露就等于把策略交给了对手（知道上限 50 就会提交 49.99）。
  - 更稳妥的替代：能**摘掉能力**（工具隐藏）就不要**描述约束**——摘掉会消灭动作空间，描述只是留下可探测的空间。
  - 事前告知是**可用性优化**（减少无效步数），不是安全控制；安全控制始终是执行点。
- 决策（已定，2026-10-10）：采纳「三层 + 事前告知只给类别」——事前只暴露能力与类别（是否需审批、是否只读、是否含 PII），不暴露阈值与白名单；事中可自适应拒绝用工具执行错误 + 稳定 `reason_code`；硬拒绝只给通用码；结构性错误用协议错误；硬拒绝连续 N 次终止运行。
- 备选：去掉事前告知层（更保守，代价是模型多撞几次墙）；策略拒绝一律用协议错误。
- 为什么不选「一律协议错误」：协议错误常被客户端库拦截、模型看不到；且任何能影响工具调用的东西都能靠触发一次拒绝终止用户会话，等于一次廉价的拒绝服务。
- 提示：建议按「可否自适应 / 是否成为攻击预言机 / 会话是否需要存活」三类分别定；完整理由写进证据账本，线上响应只回最小化投影。

### ADR-0007 三层方案（已采纳，2026-10-10）

按「可否自适应 / 是否会变成攻击预言机 / 会话是否需要存活」三类分别定，而不是二选一：

| 拒绝类别 | 例子 | 线上形态 | 线上理由 | 证据账本 |
| --- | --- | --- | --- | --- |
| 可自适应 | 金额超上限、商户不在白名单、缺必填参数、品类不允许 | 工具执行错误 `isError: true` | 稳定 `reason_code` + 一句可操作提示 | 完整：命中规则、阈值、policy 版本 |
| 硬安全 | 令牌 audience 不符、重放双付、参数被篡改、注入驱动的调用、绝对禁止项 | 工具执行错误 `isError: true` | 只给通用码（如 `policy_denied`），不给阈值与规则名 | 完整 + 标记 suspected_adversarial |
| 结构性 | JSON-RPC 非法、未知方法、params 不符 inputSchema | JSON-RPC 协议错误 | 标准错误码 | 完整 |
| 需要人 | 高风险动作 | `resultType: "input_required"`（见 ADR-0006） | 审批请求 | 完整 + 审批结果 |

- 安全性的实现方式是**最小化线上理由**，而不是换成协议错误：阈值、白名单内容、内部规则名绝不回显，否则注入指令可以二分搜索策略边界。
- 硬安全类拒绝要能升级：连续 N 次硬拒绝终止本次运行（类比 mini-swe-agent 的 `LimitsExceeded`），避免无限重试。
- 已知语义借用：规格里 `isError` 意为「工具执行失败」，用它表达「策略拒绝」是借用，需在此显式记录理由。

## ADR-0008 工具名冲突消歧（已采纳，2026-10-10）

- 背景：工具名唯一性只在单个 server 内成立；网关聚合 merchant 与 PSP 会撞名；规格建议加 server 标识前缀，且不能依赖 serverInfo 的 name。
- 证据：
  - 规格：工具名 SHOULD 只允许 `A-Za-z0-9`、`_`、`-`、`.`；唯一性只在单个 server 内成立；聚合方 SHOULD 用 server 标识前缀消歧。
  - 既有实现（2026-10-10 调研）：openziti/mcp-gateway 自动加前缀，用 `fs:read_file` 形式——注意它用了 `:`，而 `:` 不在规格建议的字符集内；metamcp 用 namespace 分组并支持按 namespace 覆盖工具名。
- 推荐：由网关用**配置里的 server 别名**生成前缀，格式 `<alias>__<tool>`（双下划线，落在规格允许字符集内）；别名重复或加前缀后仍冲突则**启动即失败**（fail fast）；映射关系写入证据，便于从一次调用反查上游 server。
- 决策（已定，2026-10-10）：采纳推荐方案。
- 备选：不加前缀（撞名即拒绝）；用 `:` 作分隔符（对齐 openziti 的做法）。
- 为什么不选：不加前缀无法同时聚合 merchant 与 PSP；`:` 不在规格建议的字符集内（规格只允许字母、数字、`_`、`-`、`.`）。

## ADR-0009 是否采用 x-mcp-header（已采纳，2026-10-10）

- 背景：该扩展把参数镜像进 HTTP 头供中间人使用；规格禁止镜像敏感参数。
- 证据：
  - 规格：把工具参数镜像成 `Mcp-Param-{name}` HTTP 头，供负载均衡 / 代理 / WAF 在不解析消息体的情况下路由与检查；约束含仅限原始类型（integer / string / boolean）、必须静态可达、整数须在 IEEE754 安全范围内、大小写不敏感唯一、不得含控制字符；并明确警告不要把密码 / 密钥 / 令牌 / PII 标成 header。
  - 采用情况（2026-10-10 调研）：按 `x-mcp-header` / `Mcp-Param` 在 GitHub 检索几乎没有真实采用，属全新扩展。
- 推荐：**不主动使用**（网关本来就要解析 body，用它没有收益），但实现两条防御——转发时保留上游声明的 `Mcp-Param-*` 头（否则破坏上游的中间人语义）；策略层检查敏感参数是否被镜像成 header，命中则拒绝或告警。
- 决策（已定，2026-10-10）：采纳推荐方案。
- 备选：主动使用它做参数级路由与检查。
- 为什么不选：网关本来就要解析 body，用它没有收益；而且会把参数暴露给链路上所有中间人（规格明确警告不要把密码 / 密钥 / PII 标成 header）。

## ADR-0010 tools/list 的过滤与缓存语义（已采纳，2026-10-10）

- 背景：tools/list 结果可因授权不同而不同（可做工具隐藏），并带 ttlMs / cacheScope；若上游声明 public 而网关的过滤结果依赖 agent 身份，直接沿用会越权泄露。
- 证据：
  - 规格：列表操作带 `ttlMs` 与 `cacheScope`，且结果可因授权不同而不同。
  - 先例（2026-10-10 调研）：HTTP 的经典解法是 `Vary`——在响应里声明除方法与 URL 外还有哪些请求头影响了内容，从而让缓存按这些维度分键（MDN：Vary 用来为内容协商创建 cache key）；metamcp 则用按 endpoint / namespace 划分视图的方式规避跨客户端复用。
- 推荐：网关缓存上游列表时按（上游标识, 上游游标）分键；对外返回的列表按**授权指纹**分键，绝不跨授权复用；若上游声明 `cacheScope: public` 而你的过滤依赖 agent 身份，出口必须收紧为 `private`（或按指纹分区），等价于给列表加上 `Vary: Authorization`。
  - 补充（2026-10-15，server/utilities/caching 原文）：
    - 规格要求服务端对以下操作的 `resultType: complete` 结果**必须**带缓存提示：`server/discover`、`tools/list`、`prompts/list`、`resources/list`、`resources/templates/list`、`resources/read`。→ 网关对这六类操作是**服务端**，所以它**构造**的结果（例如过滤后的 `tools/list`）**必须带 `ttlMs` / `cacheScope`**。
    - cache key 的定义是「请求方法 + 影响结果的请求参数」（如 `resources/read` 的 `uri`、分页列表的 `cursor`），且 **Clients MUST NOT serve a cached response for a request whose method or parameters differ**。→ 授权属于「影响结果的参数」，必须进 key（本条推荐的规格依据）。
    - **通过 MRTR 重试的请求（带 `inputResponses` 或 `requestState`）结果 MUST NOT 被缓存**，因为它们依赖的输入不在 cache key 里。
- 决策（已定，2026-10-10）：采纳推荐方案。
- 备选：原样透传上游的 `cacheScope`。
- 为什么不选：过滤结果依赖 agent 身份，而 public 缓存不区分身份，透传会导致越权泄露（把 A 的工具视图喂给 B）。

## ADR-0011 拦截面覆盖范围（已采纳，2026-10-10）

- 背景：当前架构的 I1 与 `Decision` 只绑定在 `tools/call` 上；而 resources 是 server 可暴露的一等能力面（`resources/list`、`resources/read`、订阅通知），不拦即构成策略与审计的双重旁路。
- 证据：
  - 规格：resources 能力含 `listChanged` / `subscribe` 两个可选特性；`resources/read` 返回 `contents`；`resources/list` 结果可因授权不同而不同。
  - 威胁：经 `resources/read` 读取受限数据不产生 `tools/call` 记录，既绕过策略也绕过审计；旁路有三种形态（一次性读取、枚举、持续推送）。
  - 架构：现有 I1 的表述是「不存在未判定就放行的调用」，作用域实际只有 tools/call。
- 必要性：不扩则 I1 的作用域与风险不匹配；且属低成本扩展（机制已存在，只扩作用域）。
- 可行性：`Decision` 从「工具调用」抽象为「被拦截的 MCP 请求」；policy 增加 URI 维度（见 ADR-0001 候选备选）；enforce 增加 resources 分支；主要新增工程量在 `resources/list` 的分页过滤与通知过滤。
- 安全性：不削弱现有保护，只扩大同一机制的作用域；需注意只拦 read 不拦 list 会留下枚举旁路。
- 能力暴露建议：
  - 默认保留 resources 能力并按 URI 用策略过滤；多上游取并集声明（任一上游有则声明）。
  - `subscribe: true` 只在网关自己实现了通知过滤时才声明（不能承诺做不到的事）。
  - 提供配置开关 `expose_resources: false` 作为粗粒度模式（从能力声明摘掉）；此时客户端仍可能直接调用，按 ADR-0007 归为结构性错误返回。
- 决策：**采纳**（User 认可，2026-10-10）。拦截面扩展为「所有能读数据或改变状态的方法」，覆盖 `tools/*` 与 `resources/*`（含 `prompts/get`），排除 `ping` / `logging` 等工具方法；I1 改写为「不存在未经 Decision 就放行的方法调用」。已同步到 `architecture.md`。
- 备选：a) 维持只拦 `tools/*`；b) 只拦 `resources/read` 不拦 `resources/list`；c) 扩到全协议（含 utilities）。
- 为什么不选：a) 留下策略与审计的双重旁路，且 I1 的作用域与风险不匹配；b) 留下枚举旁路——list 会泄露 URI，agent 仍可逐个尝试 read；c) `ping` / `logging` 不读写业务数据，纳入只增加噪音与开销。
- 注：会触及先验架构（Decision 适用范围与 I1 表述），按 AGENTS.md 需 User 明确认可后落地——已于 2026-10-10 认可。

## ADR-0012 能力暴露与聚合（已采纳，2026-10-10）

- 背景：网关同时面对 merchant 与 PSP；能力声明在 `initialize` 协商；若声明 `subscribe: true` 等于承诺投递经过策略过滤的通知。
- 证据：
  - 规格：resources 能力含 `listChanged` / `subscribe`；`tools/list` 结果 MAY vary by the authorization presented on the request（按授权过滤是规格承认的语义）；聚合方 SHOULD 用 server 标识前缀消歧。
  - 既有实现（2026-10-10 调研）：
    - openziti/mcp-gateway：把多个后端聚合成一个端点，工具自动加命名空间前缀（`fs:read_file`、`github:create_issue`）。
    - metamcp：把多个 MCP server 聚合成一个；用 namespace 分组，支持 server 级与工具级启停、按 namespace 覆盖工具名 / 描述 / 注解、按 endpoint 分配 namespace 并带 auth。
  - 结论：业界做法是「聚合方暴露按客户端过滤后的视图」，而不是全局摘掉能力。
- 备选：A 默认摘掉 resources 能力；B 保留能力 + 按 URI 策略过滤；C 保留能力但不实现过滤。
- 推荐方案（B）：
  - 能力声明取并集（任一上游有则声明），具体 URI 能否读由策略决定。
  - `subscribe: true` 仅在网关实现了通知过滤后声明。
  - 提供配置开关 `expose_resources: false` 作为粗粒度模式；此时客户端直接调用按 ADR-0007 归为结构性错误。
  - 工具与资源名统一加 namespace 前缀（对齐 openziti/metamcp 与规格建议），同时解决 ADR-0008 的消歧。
  - 进阶：像 metamcp 那样支持用网关自己的注解覆盖上游 hint（把不可信声明替换成你的判定），比「忽略注解」更强。
- 为什么不选 A：全有全无的控制，会让「策略拒绝」与「功能不存在」无法区分，并砍掉上游的合法用途。
- 为什么不选 C：保留能力却不拦，等于保留一个旁路。
- 决策（已定，2026-10-10）：采纳 **B 方案**——保留 resources 能力并按 URI 用策略过滤；能力声明取并集；`subscribe: true` 仅在实现通知过滤后声明；提供 `expose_resources: false` 开关；工具与资源名统一加 namespace 前缀。
- 注解子决策（2026-10-10）：不采用「用网关判定覆盖上游 hint」（留作后续扩展），但采用两条硬规则——
  1. 不可信 hint 只能用于**收紧**（例如 `destructiveHint: true` 触发人工审批），绝不用于**放行**；
  2. 出口不原样透传上游 hint：剥离或标记为不可信，避免客户端基于谎言自动放行。
- 后续扩展（future work）：用网关自己的判定覆盖上游注解（对齐 metamcp 的 namespace 注解覆盖），以及注解与策略的显式映射表。
- 补充（2026-10-15）：**sampling 不声明、不代理**——它已在 2026-07-28 弃用（规格建议改为直接集成 LLM provider API），且会把模型调用推到客户端侧使内容无法被策略约束。若上游仍在 `inputRequests` 里发 `sampling/createMessage`，网关**拒绝该子请求**并给 `reason_code`，而不是让整批失败（同批的 elicitation 仍可完成）。

## ADR-0013 分页与过滤语义（已采纳，2026-10-10）

- 背景：`resources/list`、`tools/list` 都支持分页与缓存（`nextCursor` / `ttlMs` / `cacheScope`）；网关按授权过滤条目后，上游游标的语义不再成立。
- 证据（Google AIP-158，2026-10-10 调研）：
  - Opacity：page token 必须是 opaque 且不可被用户解析，Base64 不算充分混淆；理由是一旦可解析，分页实现细节就变成 API 表面的一部分。
  - 参数绑定：分页时其他参数必须与产生 token 的调用一致。
  - Degraded skip：无法满足的 skip 必须返回 200 + 空结果集；已知超出集合范围时不得返回 next_page_token；允许「空页 + next_page_token」，靠 next_page_token 缺省来信号结束。
- 推论：网关必须自己签发游标（不能透传上游游标）——透传会泄露上游实现细节、违反 opacity；而且你的过滤条件本身就是「其他参数」的一部分，上游 token 与它并不绑定。
- 推荐方案：
  - fetch-through 填页：内部按上游游标连续拉取，直到凑满一页允许项或上游耗尽；对外发放自己的 opaque 游标。
  - 采用 AIP-158 的 degraded skip 语义：允许返回「空页 + 非空游标」，由游标缺省表示结束——这正好回应「空页会不会误导客户端」。
  - 设上限：单次客户端请求最多连续拉 N 页（建议 5），超限返回当前内容与「未耗尽」游标；上游调用数计入成本治理。
  - 缓存：按（授权指纹, 上游游标）缓存原始页，避免重复拉取；不得跨授权复用（呼应 ADR-0010）。
  - 恶意上游用永不结束的游标造成放大：以 N 页上限 + 每 agent 速率限制兜底。
- 决策（已定，2026-10-10）：采纳推荐方案——fetch-through 填页 + 自签 opaque 游标 + AIP-158 的 degraded skip 语义 + 单次客户端请求最多连拉 5 页 + 按（授权指纹, 上游游标）缓存且不跨授权复用。
- 备选：透传上游游标，并对外声明「每页数量可能少于声明值、空页不代表结束」。
- 为什么不选：透传会泄露上游分页实现（违反 opacity），且上游 token 与你的过滤参数并不绑定；把「空页可能是中间态」的理解负担推给模型，会直接损害 agent 的决策质量。
## ADR-0014 显式维护类型分类清单（已采纳，2026-10-15）

- 背景：网关对协议类型分「构造 / 校验 / 透传」三类（见 `architecture.md` 第 7 节）。ADR 变更可能把某个类型从透传升级为构造（已发生一例：`resources/list`）。
- 证据（2026-10-11 调研）：2026-07-28 通过 **SEP-2577** 批量弃用了一批接口（logging 的 RPC、roots、sampling 的 `ModelHint` / `ToolChoice`、`ToolResultContent` / `ToolUseContent`），并设了 deprecated features registry——说明协议面本身会变。若分类只存在脑子里，升级或弃用时不会触发对应的测试补齐。
  - 行业做法（2026-10-14 调研）：这套东西在软件架构里叫 **architecture fitness function**（架构适应度函数）——`archguard/archguard`（677★，架构治理工作台，可创建 fitness function 并分析依赖）是代表实现；Python 侧的落地工具是 `seddonym/import-linter`（1218★，Lint your Python architecture），Go 侧有 `fe3dback/go-arch-lint`。共同点：**把架构规则写成 CI 可执行的门禁**，而不是文档里的约定。
- 推荐：在代码里维护三张显式清单（`PASSTHROUGH` / `CONSTRUCTED` / `VALIDATED`），并让 CI 检查「每个构造类类型都有 golden 测试、每个校验类类型都有 negative 测试」。附带收益：可以拿清单快速核对规格的弃用清单，避免把弃用接口当推荐路径。
- 备选：只在文档里维护分类（成本更低，但不会在 diff 里可见）。
- 决策（已定，2026-10-15）：采纳推荐方案——代码里维护 `PASSTHROUGH` / `CONSTRUCTED` / `VALIDATED` 三张清单，CI 检查构造类有 golden、校验类有 negative。
- 为什么不选「只在文档里维护」：不会在 diff 里可见，升级或弃用时不会触发测试补齐。
## ADR-0015 评测范围与方法（已采纳，2026-10-14）

- 背景：作者是经济统计学本科 + 应用统计硕士，评测是最能差异化的部分；同时必须控制总工时。
- 证据：
  - 治理侧：EU AI Act 第 26 条对部署方要求 human oversight、audit logs、continuous monitoring（2026-08-02 生效）——评测指标应能逐项对应这三项义务。
  - 工程侧：项目已有四条可测试不变量（I1 无旁路、I2 预算并发安全、I3 防篡改、I4 可复算），它们本身就是评测的被测对象。
  - 行业侧：安全评测的通行做法是「违规率 + 随攻击强度的鲁棒性曲线 + 成本 / 延迟开销」，并要求可复现（固定种子、多次重复、报告方差）。
  - 文献（2026-10-12 调研，四篇最相关）：
    - **Auditable Claims about AI Agents**（2026-10-05）：EU AI Act 第 12 条只要求「自动记录事件」，**没说哪些记录能支撑某条声称**；结论是声称必须事先写明 policy、scope、能结算它的记录、以及谁写这些记录，并附一条判定规则。对 agent 追加三个条件：由**独立记录**覆盖、授权**绑定到每个动作的参数**、以及「完整性要超越防篡改」（不仅要防改，还要保证记录覆盖了所有该记的事）。
    - **Agent-Safety Evaluations as Load-Bearing Evidence**（2026-07-14）：提出以 **reconstructability**（证据能否重构声称所依赖的那个决策）作为评测有效性指标，含「重放前置条件探针」与「claim-evidence overclaim gap」；实测发现**多数 trace 的重放前置条件根本不满足**。
    - **Trust Is Not a Score / Runtime Assurance Contracts**（2026-09-30）：提出「assurance-transition gap」，主张用**非补偿性门（non-compensatory gates）**在运行时改变 agent 权限；其实验显示纯分数规则会放行 80/100 个本该拦截的注入，而合取门全拦。
    - **From Agent Failure Paths to Quantified Residual Risk**（2026-04-28）：把失效路径映射为**残余风险量化**，可复用你的统计学背景。
- 推荐：把评测做成第一等交付物（`eval/` + `docs/evaluation.md` + 一张结果表 + 一条曲线），指标分五组：
  1. 安全不变量：分类违规率（超额、双付、越权商户、未知内容类型、通知旁路）；
  2. 判定质量：误拦率 / 误放率（需要带标签的场景集；场景参数化生成并留 held-out 集防记忆）；
  3. 鲁棒性曲线：拦截率随注入强度的变化；
  4. 成本与延迟：单次调用开销、每场景上游调用数（含 fetch-through 的放大系数）；
  5. 审计质量：审计完整率、`verify` 通过率、`replay` 决策一致率、hold 泄漏率。
  组织方式改为「**声称 → 需要的记录 → 指标 → 判定口径**」，而不是按指标堆砌（这正是文献里的 claim-check 做法）：

  | 声称 | 需要的记录 | 指标 |
  | --- | --- | --- |
  | I1 不存在未判定放行 | 每次被拦截调用一条 Decision | 无 Decision 的调用数 = 0（插桩 + import-linter 分层契约） |
  | I2 预算不超支 | hold 与 commit / release 记录 | 并发压测下超支事件 = 0；hold 泄漏率 |
  | I3 证据防篡改 | 哈希链 + Merkle 根 | 篡改任一字节后 `verify` 失败率 = 100% |
  | I4 可复算 | 规范化输入 + `policy_digest` | 重放**前置条件满足率** + 决策重构率 |
  | 授权绑定参数 | 每条记录的 `args_digest` + 策略版本 | 分类违规率（超额 / 双付 / 越权商户） |
  | 审计完整性 | 记录覆盖所有被拦截方法 | 审计完整率（记录数 / 被拦截调用数） |
  | 审批确实发生 | 审批记录绑定 `action_hash` | 未审批执行数 = 0；审批-动作绑定校验通过率 |

  三项方法论升级（借文献，直接对应你的统计背景）：把 I4 升级为 **reconstructability**（含「重放前置条件探针」）；增加 **claim-evidence overclaim gap**（自称能力与证据能支撑的能力之差）；做一个 **非补偿性门 vs 纯分数规则** 的对照实验（文献显示分数规则会放行大量本该拦截的调用）。
  方法论要求：固定种子、每配置 N≥5 次重复、报告均值与置信区间、预先声明每个指标的判定口径、公开场景生成器以便复现。
- 明确不做（避免为评测而评测）：不做模型能力横评（那是 model evaluation，不是 governance evaluation；若做，只为证明控制措施与模型无关）；不复现通用 agent 安全基准的全量；不训练、不做 RL；不追求「一个分数」——文献标题就是 Trust Is Not a Score。
- 范围上限（防蔓延）：只做一个业务族（commerce）、12–20 个对抗场景；不接真实支付、不做多租户横评。
- 备选：a) 只做功能演示、评测留白；b) 全量模型横评（更亮眼但工时不可控）。
- 决策（已定，2026-10-14）：采纳推荐方案——按「声称 → 记录 → 指标」组织，含三项方法论升级（reconstructability / overclaim gap / 非补偿门对照）。
- 为什么不选 a（只做功能演示、评测留白）：没有数字就没有可检验的声称，简历上也只剩形容词。
- 为什么不选 b（全量模型横评）：那是 model evaluation，不是 governance evaluation，且工时不可控。

## ADR-0016 驱动（agent driver）的实现方式（已采纳，2026-10-15）

- 背景：架构里的 `driver/` 是「薄循环 + MCP client」，用来把网关放进一个真实的 agent 回路；需要决定自写还是用现成框架（提议过 Pi Harness）。
- 证据：
  - 驱动不是学习目标，但**必须可复现**：ADR-0003 的 `replay` 与 I4 要求同一输入得到同一决策序列，因此驱动必须能被固定住（固定动作序列或固定种子）。
  - Pi 是 TypeScript，引入它会给 Python 项目增加跨语言构建链；且其上游对新贡献者默认自动关闭 issue / PR，社区门槛高。
  - mini-swe-agent 的薄循环（约 100 行）已经证明「能跑通、可读、可复现」，作者也已充分掌握该模式。
  - 行业做法（2026-10-14 调研）：要确定性测试，通行手段是 **record & replay**——`vcr/vcr`（6080★，录制测试套件的 HTTP 交互并在后续运行中重放，用于 fast / deterministic / accurate tests）及其多语言移植（`dnaeon/go-vcr`、`php-vcr`、`node-replay`）。对本项目的意思是：驱动把「动作序列 + 上游响应」录下来重放，就能让 I4 的重放确定性可测；这也解释了为什么驱动不宜交给「下一步由模型决定」的框架。
- 推荐：驱动自写（约 100–150 行：MCP client + 固定动作序列 + 可选 LLM 决策），复用 mini-swe-agent 的循环经验；把「用现成框架做对比」留作 future work（若要用，优先 Python 生态且能固定种子的）。
- 备选：a) 用 Pi Harness（TS）；b) 用其他 Python agent 框架；c) 自写薄循环。
- 决策（已定，2026-10-15）：采纳推荐方案——自写薄循环（约 100–150 行），并把「动作序列 + 上游响应」录下来重放以保证确定性。
- 为什么不选 a：TS 会给 Python 项目加跨语言构建链，且其上游对新贡献者默认自动关闭 issue / PR。
- 为什么不选 b：多数框架把「下一步做什么」交给模型，难以固定，会让 I4 的重放确定性不可测。
### 2026-10-14 不透明句柄的判据（以及它在 2026-07-28 的一个反例）

判据：**凡是不透明句柄，先问「它绑定的是谁的哪次调用」；绑定关系跨层就必然要重签，不能透传。**

- 分页游标（ADR-0013）：透传会泄露上游分页实现（违反 opacity），且上游 token 与你的过滤参数不绑定。
- `requestState`（ADR-0006）：网关是签发方；上游的 state 必须映射，不能直接交给 agent。
- 反例（也是对判据的检验）：**会话标识在 2026-07-28 里根本不是协议概念**——`Mcp-Session-Id`、GET 独立流、`Last-Event-ID` 恢复都属于 2025-03-26～2025-11-25 的旧形态，本版明确不含。所以网关的「会话」是它**自己定义**的概念（绑 agent + run），不是需要重签的协议句柄。判据仍然成立：先问绑定关系，再决定重签还是自建。
## ADR-0017 网关自建会话状态（已采纳，2026-10-15）

- 背景：2026-07-28 没有协议级会话（`Mcp-Session-Id`、GET 独立流、`Last-Event-ID` 恢复都属旧版），但网关的预算、审批、证据都要按「会话」聚合；每次请求又都是独立 POST，协议不提供跨请求绑定。
- 证据：
  - 规格：每个请求自带 `_meta`（含 protocolVersion / clientInfo / clientCapabilities），但没有任何会话标识；服务端主动推送走 `subscriptions/listen` 的长连接。
  - 架构需要：预算按会话累计、审批按会话 durable wait、证据按运行打包——三者都需要一个会话键。
  - 参考笔记的判据：不透明句柄要先问「绑定谁的哪次调用」；这里根本不存在协议句柄，所以是**自建**而非重签。
  - 行业做法（2026-10-14 调研，12-Factor 的 Processes 章）：Twelve-factor processes are **stateless and share-nothing**；任何需要持久的数据必须放进**有状态的后备服务**（通常是数据库）；依赖「sticky sessions」（把会话缓存在进程内存里并期待后续请求落到同一进程）**是对 12-Factor 的违反，不应使用**；会话状态适合放进**带过期能力**的存储。这正好对应「进程无状态 + 会话落 SQLite + TTL 回收」。
  - 补充（2026-10-15，mrtr 页）：MRTR 的设计目标之一是「不需要跨实例的共享存储、也不需要带状态的负载均衡」——协议刻意把暂停状态交给客户端用 `requestState` 携带。所以网关应**尽量减少自持状态**：暂停状态优先走 `requestState`，只有预算台账与证据账本这类业务状态必须落盘。elicitation 页另有 MUST：**若存储状态，必须安全地关联到具体用户**（与 `session_id` = agent + run 一致）。
- 推荐：网关自定义会话键（`session_id` = agent 身份 + run id），随第一条请求建立并写入证据；预算、审批、证据全部以它聚合；用 TTL 与显式关闭（CLI / admin 命令）回收；对上游不暴露这个键（上游只看到逐请求 POST）。
- 备选：a) 不设会话，所有状态按单次请求处理；b) 借用 agent 的 run id 但不校验。
- 为什么不选 a：预算与审批在语义上本就跨请求。
- 为什么不选 b：可被伪造的会话键会破坏预算与证据的独立性。
- 决策（已定，2026-10-15，含细化）：采纳推荐方案——网关自定义 `session_id`（agent 身份 + run id）；**尽量少自持状态**：暂停状态优先走 `requestState`，只有预算台账与证据账本必须落盘；落盘状态要安全绑定会话/用户，用 TTL 与显式关闭回收，对上游不暴露。

## ADR-0018 版本处理策略（已采纳，2026-10-15）

- 背景：2026-07-28 把版本从「initialize 握手」改成**逐请求声明**；V1–V3 的回答里出现了「透传版本」与「加翻译层」两个倾向。
- 证据（basic/versioning 原文，2026-10-14 核实）：
  - **每个请求在 `_meta` 里声明自己使用的协议版本**；服务端若不实现该版本（未知，或已知但选择不支持），MUST 返回 `UnsupportedProtocolVersionError`（code -32022），`data.supported` 列出支持的版本、`data.requested` 是请求的版本。
  - **客户端 SHOULD 从 supported 列表里选一个共同版本重试**——不匹配的处理已在协议层闭环。
  - **服务端 MUST 实现 `server/discover`**；客户端 MAY 在发其他请求前先调用它了解支持版本（可选）。
  - 扩展：标识必须遵循 `_meta` 键命名规则并带强制前缀；一方支持扩展而另一方不支持时，支持方 MUST 回退到核心行为或返回错误。
  - 兼容矩阵：Modern + Modern 直接可用；版本不匹配以 `UnsupportedProtocolVersionError` 浮现，客户端用共同版本重试。
  - 行业做法（2026-10-14 调研，Google AIP-185 API Versioning）：接口**必须**带主版本号并编码在 URI 路径最前面；**不得暴露 minor / patch 版本**；**新主版本不得依赖旧主版本**；不同版本必须能在一段过渡期内**同时工作**。整套规范里**没有「翻译层」这个角色**——版本是「选择与共存」，不是「互相翻译」。
- 推荐：**逐边协商 + 不翻译**——每一跳的 `_meta.protocolVersion` 反映该跳说话方实际实现的版本（对 agent 那一跳是网关作为 server 的版本，对 mock 那一跳是网关作为 client 的版本）；启动时用 `server/discover` 探测上游支持版本，不支持则**拒绝启动**（fail fast）；运行时不匹配就把 `-32022` 如实返回给 agent，让协议层的重试闭环生效；**不做跨版本翻译**（翻译属构造类，要 golden 测试与长期维护，违反加速决定）。
- 备选：a) 跨版本翻译层；b) 只支持本版且不探测上游。
- 为什么不选 a：协议层已闭环（错误 + 支持列表 + 重试），翻译层是重复实现且成本高。
- 为什么不选 b：不探测会让不匹配到运行期才暴露；而 mock 由我们实现，启动期探测是零成本。
- 决策（已定，2026-10-15）：采纳推荐方案——逐边协商 + 启动时 `server/discover` 探测 + 不匹配即失败 + 不翻译。
## ADR-0019 身份与凭证模型（已采纳，2026-10-15）

- 背景：Authorization 一节暴露出网关有两个身份职责（下游作为 resource server 校验令牌、上游作为 client 获取凭证），但架构里没有对应模块，也没有决策。
- 证据：
  - 规格角色：受保护的 MCP server 是 OAuth 2.1 **resource server**；MCP **client** 代表 resource owner 发起请求；**authorization server** 签发令牌（实现细节不在规格内）。
  - 规格硬要求：client MUST 在授权与令牌请求里带 `resource` 参数；server MUST 校验令牌是专门发给它的；**token passthrough 被明令禁止**；令牌窃取缓解包括安全存储、短期令牌、公共客户端 refresh token 轮换；授权服务器端点 MUST 走 HTTPS。
  - 既有实现（2026-10-15 调研）：`open-webui/mcpo`、`IBM/mcp-context-forge`、`agentgateway/agentgateway` 这类前置代理都承担统一鉴权与凭证注入，并把「上游凭证来源」做成可插拔的 provider。
  - 文献（Auditable Claims）：auditable 的一个条件是「授权绑定到每个动作的参数」——意味着证据记录里必须能查到本次动作的授权依据。
  - 规格补充（2026-10-15，elicitation 页）：**URL 模式 elicitation 是敏感信息与第三方授权的合规通道**（规格点名 auth flows 与 payment processing）；server **MUST NOT** 用 form 模式索取密码 / API key / access token / payment credential，**MUST** 用 URL 模式；并特别说明 URL 模式**不是**用来授权 MCP client 访问 server 的（那属于 MCP authorization）。→ 这给「未来如何获取上游授权」提供了一条**规格内置**的路径：用 URL 模式 elicitation 驱动第三方授权流程，不必自造跳转。
  - elicitation 页的 MUST：若存储状态，必须安全地关联到具体用户。
- 推荐：
  - 新增 `identity/` 模块，两个职责分开：`validate_inbound(token, request) -> AuthorizationBasis`（校验 audience 与 scope，产出授权依据）与 `CredentialProvider.for_upstream(server) -> Credential`（产出访问上游的凭证）。
  - demo 里 `CredentialProvider` 用**静态凭证**实现，接口按 RFC 8693 令牌交换预留（未来换实现即可）。
  - **下游校验要真做**（解析令牌、校验 audience 与 scope），因为威胁 T4 的可测性依赖它。
  - 凭证保管：只从环境变量 / 本地密钥文件读，不进代码库；README 标注 demo 的不安全性；上游凭证做 scope 最小化。
  - 证据记录增加字段 `authz_basis`（本次动作的授权依据：audience 校验结果 + 命中的 scope）。
- 备选：a) 全部 stub（不校验令牌）；b) 把身份职责并进 frontdoor 与 enforce。
- 为什么不选 a：T4 无法被真实测试，评测里「授权绑定参数」那条声称就没有证据。
- 为什么不选 b：两个职责（校验入站、提供出站）与 frontdoor / enforce 的关注点不同，混进去会让执行点变厚。
- 需要的架构补充（待你认可，按 AGENTS.md）：分层职责表新增 `identity/` 一行；frontdoor 行补「令牌校验入口」；证据记录加 `authz_basis` 字段。
- 决策（已定，2026-10-15）：采纳推荐方案——新增 `identity/` 模块（`validate_inbound` 产出 `AuthorizationBasis`，`CredentialProvider` 提供上游凭证）；demo 用静态凭证实现、按令牌交换与 URL 模式授权预留接口；下游校验真做；证据记录加 `authz_basis`。
- 为什么不选「全部 stub」：T4 无法真实测试，评测里「授权绑定参数」那条声称没有证据。

## ADR-0020 授权与策略的合成（已采纳，2026-10-15）

- 背景：scope 是「授权上限」（能做什么），policy 是「业务约束」（这笔钱能不能花）；两者必须合成一个 `Decision`，但合成规则未定。
- 证据：
  - 规格：`tools/list` 的结果可因授权不同而不同（上游按 scope 过滤）；scope 不足时返回 403。
  - 文献（RAC）：非补偿性门——**聚合性能不能授权动作**；某一项强制门失败或状态未知，就必须停。
  - 文献（Auditable Claims）：授权必须绑定到每个动作的参数。
  - 行业语义（2026-10-15 调研，AWS IAM 的策略评估逻辑）：**An explicit deny in either of these policies overrides the allow**——显式拒绝优先于允许，这就是 deny-overrides 语义，是 IAM 策略系统的核心规则；它与 RAC 的「非补偿性门」一致，都拒绝「用平均分抵消一次失败」。→ 支持本条的 AND 与「任一层未知即 deny」。
- 推荐：**两层与关系（AND）**——`scope 允许 ∩ policy 允许` 才放行；任一层未知即 deny（fail-closed）；两层各自产出自己的 `reason_code`，证据里分别记录；`Decision` 增加来源标注（`decided_by: authz | policy | both`）。
- 备选：a) 只信 scope；b) 只信 policy；c) 让 policy 覆盖 scope（policy 可放宽授权）。
- 为什么不选 a：scope 是粗粒度，表达不了金额上限与商户白名单。
- 为什么不选 b：忽略授权等于把令牌当装饰，T4 又失去意义。
- 为什么不选 c：**策略永远不能放宽授权**，否则一次错误配置就能把权限放大——这与「不可信 hint 只能收紧」是同一条原则。
- 决策（已定，2026-10-15）：采纳推荐方案——`scope 允许 ∩ policy 允许` 才放行；任一层未知即 deny（fail-closed）；两层各自产出 `reason_code`；`Decision` 增加 `decided_by` 标注。
## ADR-0021 审批与敏感信息的通道选择（已采纳，2026-10-15）

- 背景：ADR-0006 定了审批走协议原生，但没定「用 form 还是 URL 模式」；而规格对敏感信息有硬性要求。
- 证据（client/elicitation 原文，2026-10-15 核实）：
  - 两种模式：form（内联表单，可带 JSON Schema 校验）与 url（跳转外部 URL，用于「不该经过 MCP client 的带外交互」）。
  - **Servers MUST NOT use form mode elicitation to request sensitive information such as passwords, API keys, access tokens, or payment credentials**；涉及此类信息 **MUST** 用 URL 模式。
  - URL 模式引入于 2025-11-25，规格注明其设计与实现**在后续版本可能变化**；URL 模式**不是**用于授权 MCP client 访问 server 的。
  - 响应差异：form 模式的 accept 带 `content`；**URL 模式的 `content` 省略**（用户在外部页面完成）。
  - 安全要求（同页 Security Considerations，2026-10-15 核实）：Servers **MUST bind elicitation requests to the client and user identity**；**MUST NOT** 在 URL 里带终端用户的敏感信息（凭证、PII）；**MUST NOT** 提供**预认证**的 URL（否则恶意客户端可借它冒充用户）；SHOULD 用 HTTPS（非开发环境）；**MUST NOT** 靠客户端提供的用户标识（可伪造，应靠授权识别用户）；Clients **MUST NOT** 自动预取 URL、**MUST** 在显式同意后才打开、**MUST** 先展示完整 URL、**MUST** 在客户端与 LLM 无法检查内容的方式下打开（规格举例：iOS 的 SFSafariViewController 可以，WkWebView 不行）、SHOULD 高亮域名并警告 Punycode。
  - 行业先例（2026-10-15 调研，Stripe 3DS 文档）：3DS 在卡交易被触发时由**发卡行**要求持卡人认证（密码 / 一次性码 / 生物识别）；**PSD2 下的强客户认证（SCA）**在 EEA、英国、印度、日本、澳大利亚等地要求卡支付使用 3DS。→「敏感支付步骤必须离开应用、到发卡方完成」是支付业的既定做法，正是 URL 模式的同构先例。
- 推荐（2026-10-15 经调研确认并细化）：
  - 审批闸门里「是否同意这笔支付 / 是否批准这个动作」这类**非敏感确认**用 **form 模式**（布尔或单选即可）。
  - **绑定身份**：审批请求必须绑定到具体的 client 与 user（规格 MUST），不能做一个全局「批准」入口。
  - 任何**敏感信息**（支付凭证、密钥、第三方授权）一律走 **URL 模式**，网关只转达 URL 与上下文，不接触内容；且 URL 里**不得**含用户敏感信息、**不得**是预认证链接（两条 MUST NOT）。
  - 收到 form 模式的响应后，网关要**按 `requestedSchema` 校验**再采信。
  - 判定规则落进 `approval/` 与 `sanitize/`：维护「敏感字段类别清单」，命中即强制 URL 模式。
- 备选：a) 全部用 form 模式；b) 全部用 URL 模式。
- 为什么不选 a：直接违反规格的两条 MUST。
- 为什么不选 b：简单确认走外部跳转会显著增加用户摩擦，且 URL 模式的设计仍在演进。
- 决策（已定，2026-10-15）：采纳推荐方案——**非敏感确认走 form 模式**（布尔 / 单选，响应按 `requestedSchema` 校验后才采信）；**任何敏感信息**（支付凭证、密钥、第三方授权）一律走 **URL 模式**，网关只转达 URL 与上下文，且 URL 不得含用户敏感信息、不得是预认证链接；审批请求必须**绑定到具体的 client 与 user**（规格 MUST），不做全局「批准」入口；「敏感字段类别清单」落在 `approval/` 与 `sanitize/`，命中即强制 URL 模式。
- 依据：本页证据（`client/elicitation` 的两条 MUST、URL 模式的 Security Considerations、Stripe 3DS 与 PSD2 SCA 的行业先例）。
- 为什么不选 a（全 form）：直接违反规格两条 MUST。为什么不选 b（全 URL）：简单确认走外部跳转会显著增加摩擦，且 URL 模式的设计仍在演进。

### 2026-10-15 roots 与 sampling 在 2026-07-28 整体弃用

两条都是**整体弃用**（不只是字段），规格给的替代方案很明确：

- **roots**：New implementations SHOULD NOT adopt it；existing implementations SHOULD migrate to **passing directories or files via tool parameters, resource URIs, or server configuration**。另有一条关键事实：**The protocol does not enforce that servers stay within roots**——所以 roots 从来不是边界，只是提示；再叠加「由被治理方声明」这一点，**它不能当授权依据**。
- **sampling**：New implementations SHOULD NOT adopt it；existing implementations SHOULD migrate to **integrating directly with LLM provider APIs**。规格同时要求 **SHOULD always be a human in the loop with the ability to deny sampling requests**。

对威胁模型的影响：roots 与 sampling 都不必再当作「潜在旁路」来防（一个已弃用且非强制，一个已弃用且建议自建 LLM 直连）；但仍要在文档里声明「本项目不支持这两项」。

### 2026-10-15 MRTR 把「旁路」变成了「可见数据」

旧的 server-initiated requests 是带外通道；MRTR 之后，`roots/list`、`sampling/createMessage`、`elicitation/create` **MUST** 作为 `input_required` 响应里的 `inputRequests` 出现——**它们现在跑在网关本来就要解析的响应体里**。

后果：早先「sampling 是网关盲点」的判断要收窄——网关现在**看得见**这些请求，因此可以选择不转发、审计或改写它们；盲点从「看不见」变成「看得见但要决定怎么办」。配套事实：`requestState` 由客户端持有且不可解析，MRTR 的设计目标之一就是不需要服务端共享存储。
## ADR-0022 与 JanuScope 的差异化定位（已采纳，2026-10-15）

- 背景：Step 2 精读了机制最接近的同类实现 JanuScope（见 `docs/prior-art.md`）。需要明确「借鉴什么、不借鉴什么、差异在哪」，否则后续所有范围决策都没有锚点。
- 证据：
  - JanuScope 的定位是**通用本地 MCP 策略代理**：短生命周期 stdio 子进程、明确无持久状态、20+ 内置 Lens、上下文预注入（push）、关键词式 SQL 守卫、可选审计日志（`ARCHITECTURE.md`、`docs/`）。
  - 它自陈的局限（值得学习的写法）：`sqlGuard` 不是 SQL 解析器；脱敏只检查输出、不追踪来源；只读访问本身不能阻止推断式泄露。
  - 文献（Auditable Claims / Load-Bearing Evidence / RAC）要求的三件事——**授权绑定动作参数**、**证据可重构**、**非补偿性门**——JanuScope 都不做。
  - 同源对照（2026-10-15 补充，调研细节见 `docs/threat-model.md` 的「依据」一节）：**CaMeL**（arXiv 2503.18813）在模型外面造一层保护层，用 **capability** 在工具调用处强制策略、阻断私有数据流向未授权通道；**AP2** 用 **mandate**（可验证数字凭证，Checkout / Payment 两类各分 Open / Closed 两阶段）表达用户意图，设计目标是「Verifiable Intent, Not Inferred Action」与不可否认的审计轨迹。本项目的「固定算子 + mandate」（ADR-0001）与「运行证据包」（ADR-0003）与这两者同源。
- 推荐：定位锁为**面向 agentic commerce / pay 的受控执行与验证控制面**，明确不与其竞争通用代理能力，并采用以下清单：
  - **借鉴**：短路拒绝（不触碰 target）；管道有序且 `audit` 注册在最前；自陈局限的写法；三层防御（意图 / 门口 / 回程）的表述。
  - **不借鉴**：20+ Lens 的广度；上下文预注入；关键词式 SQL 守卫；短生命周期 stdio 形态。
- 需要的架构补充（待你认可，按 AGENTS.md）：把 tools/call 时序里的「写证据」拆成**转发前 `record_pending`** 与**转发后 `record_done`**，让「证据先于副作用」成为架构规则——JanuScope 的 audit-first 顺序规则是这条的外部佐证。
- 备选：a) 做通用代理，与 JanuScope 正面竞争；b) 只做策略，不做证据与审批；c) 定位为控制面并明确借鉴清单。
- 为什么不选 a：通用代理是已有成熟实现的赛道（AGPL 生态里有多个），且与你的差异化（可复算证据、预算、审批）无关。
- 为什么不选 b：放弃证据与审批等于放弃你唯一的不可替代性——那是模型与框架都拿不走的部分。
- 决策（已定，2026-10-15）：采纳推荐方案——定位锁为「面向 agentic commerce / pay 的受控执行与验证控制面」；按借鉴清单执行（借鉴短路拒绝、audit-first 顺序、自陈局限、三层防御；不借鉴 Lens 广度、上下文预注入、关键词 SQL 守卫、短生命周期 stdio）；并采纳架构补充（证据拆成 `record_pending` / `record_done`，见 `architecture.md`）。
- 证据补充（2026-10-15，你已认可）：与 CaMeL 的 capability、AP2 的 mandate 的同源对照记入上文证据。对外叙事里可以直接说「策略语言边界不是拍脑袋定的，而是与 capability 和 mandate 对齐」——**决策本身不变**，只是证据与讲法变厚。

## ADR-0023 resources 面的拒绝形态（已采纳，2026-10-15）

- 背景：ADR-0011 把拦截面扩到 `resources/*`，ADR-0007 定了 `tools/call` 的三层形态。但 `resources/read` 的结果类型里**没有 `isError` 字段**——协议给 tools 留了「带载荷的失败」通道，没给 resources 留。所以「策略拒绝一次 resource 读取」该长什么样，是 ADR-0007 没覆盖的缺口。
- 证据一：SDK 2.3.0 的错误面（2026-10-15 读源码）：
  - `mcp/server/mcpserver/exceptions.py`：`ResourceError` = 「你预料到的失败」→ 客户端收到 **`-32603`** 并带你的消息；`ResourceNotFoundError` = **`-32602`**；其他异常 = 崩溃 → 客户端只拿到「只提 URI」的通用消息。对照 `ToolError` = 「预料到的失败」→ **`isError=True` + 原文**。
  - 规格（server/resources，2026-07-28）「Error Handling」原文：请求的资源不存在时 server **MUST** 返回 JSON-RPC 错误 **`-32602`（Invalid Params）**；内部错误 **SHOULD** 用 `-32603`；为向后兼容，client **SHOULD** 也接受 `-32002`（旧版本用的码）；并且 **MUST NOT** 对不存在的资源返回**空 `contents` 数组**——「空数组有歧义，既可能表示资源存在但没有内容，也可能表示它不存在」。
  - 规格同页「Security Considerations」：`Servers MUST validate all resource URIs`、`Access controls SHOULD be implemented for sensitive resources`、`Resource permissions SHOULD be checked before operations`——按 URI 做访问控制是规格**点名建议**的方向。
  - SEP-2164（`modelcontextprotocol/modelcontextprotocol#2164`）：把「资源不存在」的错误码从 `-32002` 标准化为 `-32602`，理由是向 JSON-RPC 规范对齐。
  - 后果：照搬 ADR-0007 的「策略结果不用协议错误」，resources 面就无路可走；而 ADR-0007 反对协议错误的两条理由里，「客户端库拦截、模型看不到」在 resources 上同样成立。
- 证据二：`resources/list` 允许按授权变化（规格 server/resources 原文，2026-07-28）：「The set MAY vary by the authorization presented on the request — for example, returning only the resources the caller's granted scopes permit — since credentials are per-request input, not connection state.」同一段同时要求它 **MUST NOT vary per-connection or as a side effect of other requests on the connection**。→ 按授权过滤 list 是规格**明确允许**的，也再次印证 ADR-0010 的「按授权指纹分键」。
- 证据三：同类实现的通行做法（2026-10-15 复查 README 层面）：`TBXark/mcp-proxy` 只做聚合（"aggregate tools, prompts, and resources from many servers"）；`lasso-security/mcp-gateway` 做请求/响应拦截与脱敏（"Intercepts requests and responses to sanitize sensitive information"），没有资源级授权；`IBM/mcp-context-forge` 把资源注册进统一注册表与虚拟服务器，README 未见按调用方过滤 `resources/list`；`agentgateway` 的 README 只写到 tool federation 与 CEL RBAC。→ **公开文档层面没有一个同类实现按调用方授权过滤 resources**，多数停在工具层或「聚合 / 脱敏」。诚实边界：agentgateway 的文档站是 JS 渲染，本次没取到 RBAC 正文，这条只到 README 层面。
- 证据四：HTTP 先例（RFC 9110 §15.5.4 / §15.5.5）：403 一节写着 "An origin server that wishes to 'hide' the current existence of a forbidden target resource MAY instead respond with a status code of 404 (Not Found)"，而 404 的语义是「没找到，或**不愿披露它存在**」。→ 把「未授权」与「不存在」做成同一个答案，是 HTTP 世界早已标准化的做法。
- 备选：a) 拒绝即 `-32602` + `"Resource not found"`（与「不存在」完全同形，理由只进账本）；b) 拒绝时回**净化后的内容**（不报错，给占位资源）；c) 拒绝时回空 `contents`；d) 在 `resources/list` 阶段就把该 URI 摘掉，`read` 只在 URI 从未出现过时才报错。
- 推荐：**d 为主 + a 兜底，且 a 与「不存在」同形**。`list` 按调用方授权过滤（规格明确允许，游标重签见 ADR-0013），让「不存在」成为默认答案；只有对**从未在 list 里出现过的 URI** 才走到 `read` 的拒绝，形态取 `-32602` + `message: "Resource not found"` + `data.uri`，**不**带 `reason_code`——完整理由（命中哪条策略、谁的 mandate、哪个 policy 版本）只进证据账本。
  - 为什么不选 b：返回净化内容等于**假装成功**，agent 会拿占位内容继续推理；「读到了但内容是假的」比「读不到」更难审计，还会污染轨迹。
  - 为什么不选 c：空 `contents` 被规格 **MUST NOT**（见证据一），而且它是**合法成功**，agent 分不清「没有这个资源」与「被拒」，也拿不到 `reason_code`。
  - 为什么不选纯 a：每次探测都产生一条拒绝，等于把 list 的枚举旁路（ADR-0011 已点出）留给对手。
- 与 ADR-0007 的关系：不是推翻，是**补齐作用域**——三层形态继续只管 `tools/call`；resources 面新增「默认不可见（list 过滤）+ 越界读取与不存在同形」这一条。它同时把 ADR-0007 已有的原则「能摘掉能力就不要描述约束」贯彻到底：list 里摘掉是摘掉能力，read 上与不存在同形是不给预言机。
- 已知代价（显式记录）：不带 `reason_code` 意味着模型拿不到「这是策略拒绝」的反馈，无法自适应。接受它的依据是 resources 面**本来就没有可自适应的通道**（没有 `isError` 载荷），而带码会把「这个 URI 存在且受保护」变成可探测的预言机，与 ADR-0007 的「不成为攻击预言机」原则冲突；可用性由 list 过滤承担，不由错误消息承担。
- 决策（已定，2026-10-15）：采纳推荐方案——`resources/list` 按调用方授权过滤（规格明确允许，游标重签见 ADR-0013）；对从未在 list 里出现过的 URI，`resources/read` 回 `-32602` + `"Resource not found"` + `data.uri`，**不带** `reason_code`，与「不存在」完全同形；完整理由（命中策略、mandate、policy 版本）只进证据账本；备选 b、c 与「纯 a」均不采纳。
- 依据：本页证据一（SDK 与规格的错误面）、证据二（`resources/list` 可按授权变化）、证据三（同类实现均未做资源级过滤）、证据四（RFC 9110 的 404 先例）。

## ADR-0024 agent 边的协议时代（已采纳，2026-10-15）

- 背景：ADR-0018 定了「逐边协商 + `server/discover` + 不翻译」，但没定 **agent 边用哪个时代**。W1 的 hello 只跑通握手版（`2025-11-25`）——那是因为当时的探针用裸 `ClientSession` 直接走 initialize；W2 的 Step 0 实测发现，**时代是客户端的选择**，而三个已采纳决策都依赖现代版词汇。
- 实测（2026-10-15，`scripts/probe_era.py`，SDK 2.3.0）：

| 场景 | 协商结果 | 观察 |
| --- | --- | --- |
| stdio + `mode=auto` | **2026-07-28** | 现代版可达；能力广告 `list_changed=True` / `subscribe=True` |
| stdio + `mode=2026-07-28`（pin） | 2026-07-28 | **`server_info=None`、能力全 None**——pin 跳过 `server/discover`，客户端学不到服务端身份与能力 |
| stdio + `mode=legacy` | 2025-11-25 | 同一台 server 在握手版下广告 `list_changed=False` / `subscribe=False` |
| Streamable HTTP + `mode=auto` | 2026-07-28 | 现代版在 HTTP 上同样可达 |
| inproc + `mode=auto` | 2026-07-28 | 中间件依次看到 `server/discover`、`tools/list`、`tools/call`——**现代版路径同样过 `ServerMiddleware`** |

- 证据（依赖现代版的既有决策）：ADR-0006 的 `input_required` + MRTR（现代版**禁止服务端发起请求**，`mcp/server/connection.py:161`）；ADR-0010 的 `ttlMs` / `cacheScope`（`mcp/client/caching.py:341` 按协商版本分叉；可缓存方法是 `prompts/list`、`resources/list`、`resources/read`、`resources/templates/list`、`server/discover`、`tools/list`）；ADR-0018 的逐请求 `_meta` 版本声明与 `server/discover`。
- 推荐：**agent 边优先现代版**——网关作为 server 端同时服务两代（SDK 的 `serve_dual_era_loop` 已具备），但对外文档、演示与评测**以现代版为准**；我们的 driver（客户端）用 `mode="auto"`，**不要 pin 版本**。
- 备选：a) 只做握手版；b) 只做现代版（拒绝老客户端）；c) 双代并存、以现代版为准。
- 为什么不选 a：ADR-0006 / 0010 / 0013 全部失去载体——审批用不上 `input_required`，缓存提示无处可放，游标语义退化。
- 为什么不选 b：会拒绝一切老客户端，演示与评测的可复现性都变差；而双代并存并不额外增加多少工作量（SDK 已内置）。
- 附带发现（不影响决策，但要处理）：8000 端口被一个**残留的 hello-mcp HTTP 进程**占着（`[Errno 10048]`），HTTP 探针正是打到那个老实例上；W2 起网关之前先清残留进程。另外「能力广告」随时代不同（同一台 server，现代版报 `list_changed=True` / `subscribe=True`，握手版全 False）——**不要依赖 SDK 默认值**，按 ADR-0012 显式设置能力广告（尤其是 `subscribe`：不实现通知过滤就不能声明）。
- 决策（已定，2026-10-15）：采纳推荐方案——**agent 边优先现代版**（`2026-07-28`）；网关作为 server 端同时服务两代（`serve_dual_era_loop`），但对外文档、演示与评测以现代版为准；driver 用 `mode="auto"`，**不 pin 版本**；能力广告按 ADR-0012 显式设置，不依赖 SDK 默认值。（附：残留的 8000 端口进程已于 2026-10-15 清理，`Stop-Process -Id 15988`，复查监听数为 0。）
- 依据：本页实测表（`scripts/probe_era.py`，2026-10-15）+ 源码位置（`mcp/server/connection.py:161`、`mcp/client/caching.py:341`、`mcp_types/methods.py:411`）。

## ADR-0025 Step 2 驱动与模型客户端：复用现成 harness 还是自写（已采纳，2026-10-15）

- 背景：Step 2 要写薄循环 driver + record&replay。你问：能不能直接套 `mini-swe-agent`，或简单改改就用，或换别的简单 harness；模型客户端能不能直接用 LiteLLM。ADR-0016 已定「自写薄循环」，所以这条 ADR 要回答的是**有没有证据支持推翻它**。
- 证据（2026-10-15 调研 + 本地源码）：
  - **ADR-0016 的核心理由是确定性**：驱动必须能被固定，才能让 ADR-0003 的 `replay` 与 I4 的可复算成立；多数框架把「下一步做什么」交给模型，难以固定。
  - **mini-swe-agent v2**（`github.com/SWE-agent/mini-swe-agent`，PyPI 有包，本地也有一份学习副本）：核心 agent 类约 100 行（`src/minisweagent/agents/default.py`），但它的**工具面是 shell**（环境抽象为 local / docker / singularity / bubblewrap 等），模型层走 **LiteLLM**（`models/litellm_model.py`），轨迹是 SWE-bench 风格的 `.traj.json`。要让它调 MCP 工具，只能把 MCP 调用伪装成 shell 命令，或改它的 tool / environment 抽象——**改造成本高于自写 120 行**，还会把它的 prompt 与容器假设带进来。
  - **它真正的价值是设计模式**：单步 action + observation、明确的上限（步数 / 成本）、轨迹落盘。这三点我们本来就打算照抄（ADR-0016 写的「复用 mini-swe-agent 的循环经验」）；今天进一步明确为「**照抄模式、不引入依赖**」。
  - **我们比它更省事**：W2 Step 0 已验证 `mcp.client.client.Client`（`mode="auto"`）能直接连上 MCP，工具 schema 直接来自 `tools/list`；driver 的真实体量比 mini-swe-agent 还小。
  - **LiteLLM**（`docs.litellm.ai`）：用 OpenAI 格式统一 100+ provider，另有更瘦的 `litellm-core` 发行版（官方专门提供「fewer mandatory dependencies」的版本，说明依赖面是已知代价）。我们的端点是 OpenAI 兼容（`https://opencode.ai/zen/go/v1`），**换模型就是换 model 名**，LiteLLM 的多 provider 价值现在用不上。
  - **当前 venv 事实**：`openai` 与 `litellm` 都没装；`httpx2`（mcp 自带的 HTTP 客户端）在。所以「用 LiteLLM」= 新增一个较大的依赖，而不是复用已有件。
- 推荐：
  1. **保持 ADR-0016：自写薄循环**（约 120 行），循环结构照 mini-swe-agent 的模式（单步 action、步数 / 成本上限、轨迹落盘），并在代码注释与 `docs/w2-plan.md` 里注明借鉴点。
  2. **模型客户端**：定义约 30 行的 `ModelClient` 协议（`complete(messages, tools) -> tool_call | text`），默认实现用 **`openai` SDK**（一个新增依赖，官方维护，工具调用形状现成）；**LiteLLM 作为可选后端**写在接口后面（装上就多一个实现，不进必装依赖）。
  3. 顺手修一个不一致：`pyproject.toml` 的 wheel 包列表补上 `src/mcp_servers`（新目录此前没进打包清单）。
- 备选：a) 直接用 `mini-swe-agent` 当库；b) 换其他 Python harness（smolagents / pydantic-ai / OpenAI Agents SDK 等）；c) 模型层用 `httpx2` 手写 chat/completions（零新依赖）；d) 现在就引入 LiteLLM。
- 为什么不选 a：工具面是 shell、模型层绑 LiteLLM、轨迹是评测格式；改造成本高于自写，且会把它的 prompt 与容器假设带进项目。
- 为什么不选 b：与 ADR-0016 的「为什么不选 b」同因——下一步由模型决定、难以固定，I4 的重放确定性会不可测；框架还会接管消息历史与工具 schema，trace 与 record&replay 都要和框架内部状态对齐。
- 为什么不选 c：`httpx2` 是 mcp 自带 fork 的名字，直接依赖它比较脆；手写工具调用解析约多 40 行且容易出错，留作备选即可。
- 为什么不选 d：现在只有一个 OpenAI 兼容端点，LiteLLM 的多 provider / 路由 / 回退都用不上，却要付出依赖面与启动时间。
- 决策（已定，2026-10-15）：采纳推荐方案，并按你的补充收窄——**LiteLLM 完全不做**（连可选适配器也不写，只在本页留一句 future work）；`ModelClient` 接口**保留**，但保留的理由改成「**record&replay 必须能替换模型调用**」，不是「便于换 provider」；默认实现用 `openai` SDK；`pyproject.toml` 的 wheel 包列表补 `src/mcp_servers`。
- 依据：本页证据（mini-swe-agent 的工具面 / 模型层 / 轨迹形态、LiteLLM 的依赖代价、当前 venv 现状）。


## ADR-0026 分工调整：三处核心改由 AI 实现（已采纳，2026-10-15）

- 背景：`AGENTS.md` 的分工表把 `policy/`、`budget/`、`evidence/` 三处核心划给 User，配套硬规矩 1「能复述：简历上写的每个模块，必须能从小规模手写复现」。2026-10-15 你决定：**之后的分工都由 AI 完成，你只负责检查、决策、校验和确认**，同时要求「项目保持干净、代码可读性强，以便我在项目完成之后回过头来学习」。
- 证据：
  - **时间账**：三处核心合计约 12.5h（policy 4.5 + budget 4 + evidence 4），而 W3 总预算 18h。你在实习投递窗口内，自己写三处核心会把 W3 拉长到两周以上，且期间没有可跑的端到端 demo。
  - **学习机制可替代**：硬规矩 1 的**目的**是「面试能讲」，不是「必须由你敲键盘」。可替代路径有三条——① 代码可读性 + 逐模块「读代码导览」（`docs/code-tour.md`）；② Step 5 的盘问覆盖三处核心，讲不出来就是没学会；③ 双实现对照保留为**待补**项，你有空时仍可写自己的版本做 diff。
  - **已有先例**：Step 1 的 `policy/` 就是 AI 起草、你检查（2026-10-15），当轮你确认通过——「AI 写 + 用户审 + 盘问兜底」这条路已经被验证过一次。
  - **反面风险（显式记录）**：三处核心恰好是面试最容易追问的地方（策略算子边界、并发扣减窗口、哈希链与 Merkle 根）。只做到「跑通了」会立刻露馅，所以本 ADR 附带可读性要求，且 Step 5 必须盘问这三处。
- 推荐：采纳调整，并附三条可读性要求（都是为了让「事后学习」成立）：
  1. **模块 docstring 三段式**：这个模块在架构里的位置 → 它守住哪条不变量 → 它**不做什么**（边界比功能更能说明设计）。
  2. **注释只写「为什么」**：例如「为什么这里要 IMMEDIATE 事务」「为什么游标要带授权指纹」；「做什么」由代码本身表达。
  3. **维护 `docs/code-tour.md`**：按「读代码顺序」逐模块给导览，每个模块配 2–3 个自测问题；Step 5 的盘问直接从这里出题。
- 备选：a) 维持原分工（你写三处核心）；b) AI 写三处核心但不附可读性要求；c) 折中——只把 `evidence/` 交给你写。
- 为什么不选 a：会把 W3 拉长到两周以上，且期间没有可跑的端到端 demo，投递窗口等不起。
- 为什么不选 b：三处核心是面试必问，没有可读性与盘问兜底，等于把项目做成「能跑但讲不出」。
- 为什么不选 c：`evidence/` 与 `budget/` 在 `record_pending` / `settle` 上强耦合（证据先于副作用、hold 生命周期要进证据），拆开写反而增加返工。
- 决策（已定，2026-10-15）：采纳推荐方案。`AGENTS.md` 的分工表与 AI Native 实践同步更新；**硬规矩 1 不变**（仍然是「面试能复述」），只是达成路径从「你手写」改成「读代码导览 + Step 5 盘问」。
- 依据：本页证据（时间账、学习机制的可替代性、Step 1 的先例）与你 2026-10-15 的明确授权。

## ADR-0027 预算的计量口径：授权还是捕获（已采纳，2026-10-16）

- 背景：ADR-0002 定了**机制**（reserve → commit/release、幂等键、TTL 回收），但没定**哪些调用该占额度**。当前实现是「绑定里有 `amount_field` 且参数里有金额 → 占额度」，而 demo 策略给 `create_payment_intent`（授权）与 `capture_payment`（捕获）**都**声明了 `amount_field`——于是同一笔钱被记两次。
- 实测（2026-10-16，红队第一批跑批撞出来的）：
  - `create_payment_intent` 成功（状态 `requires_capture`，**钱没动**）→ 网关 `commit` 9.71；
  - `capture_payment`（同一笔 9.71）→ 再 `reserve` 9.71 并 `commit` → **一次 9.71 的购买按 19.42 记账**；
  - 预算紧时表现为「预算用尽」：第一批 8 次运行的 `budget_exceeded` 全部来自这里（`docs/redteam-log.md`）。
- 证据（**2026-10-16 网络调研**，逐条可点）：
  - **Stripe 手动捕获**（`docs.stripe.com/payments/place-a-hold-on-a-payment-method`，2026-10-16 取）：授权是「在客户的付款方式上**冻结金额**来保证该金额」（原文：授权付款通过在客户的付款方式上冻结金额来保证该金额），钱在 capture 时才动；授权有**有效期**（无卡：Visa 5 天 / Mastercard 7 天，`capture_before` 标明何时过期），而且——**如果授权在捕获之前过期，资金会被释放、付款状态变为 canceled**。酒店是官方举的例子：「在顾客到达前全额授权付款，然后在顾客结账时扣款」。
  - **Stripe Issuing 的 spend controls**（`docs.stripe.com/issuing/controls/spending-controls`，同日取）：发卡侧的限额是「**per authorization** 或 per month」——**控制点在授权**，clearing（捕获）不再重新判定。这是本次调研里唯一**反对**方案 A 的行业证据，必须正面回答（见下面的复核）。
  - **AP2 的 mandate 词汇表**（`ap2-protocol.org/glossary/`，同日取）：Open Mandate = 「尚未绑定到具体动作，携带**约束**」；Closed Mandate = 「**绑定到具体动作**、由 Verifier 授权」；另有 Checkout Mandate = 「用于授权完成一次 checkout」。也就是说 AP2 的金额绑定发生在 **checkout / 授权**那一步，且它给的是**逐笔授权**语义——**AP2 没有定义跨笔的累计预算**，累计预算是我们自己的账本（ADR-0002），所以「哪一步扣账」这个问题 AP2 不回答。本 ADR 只用它证明「授权阶段就已经把金额绑死了」。
  - 结论：**授权 = 在付款方式上占额度（会过期、会释放），捕获 = 钱真的动**。网关的账本记的是「用户花了多少」，所以它必须跟着 **charge** 走。
- 推荐（**方案 A**）：**只有真正动钱的调用占额度**——给绑定加一个开关 `reserves_budget`（默认 true），把 `create_payment_intent` 的那个关掉。
  - **机制在实现时被修正过一次**（值得留档）：我原先写的是「把 `amount_field` 从授权的绑定里去掉」。**这条是错的**——`amount_field` 同时承担两件事：告诉策略引擎「金额在哪个参数里」（据此判上限与白名单），以及告诉预算层「这次占多少」。删掉它，「999 元的授权」就会**直接放行**（引擎只在声明了字段时才去读金额）。落地时的 `test_channel_two_over_limit_payment` 当场把它照了出来。正确做法是把两件事拆成两个字段：`amount_field` 保留（继续判上限），新增 `reserves_budget: false`（不动钱）。
  - 因此这不是「只改策略数据、不动代码」：要在 `policy/schema.py` 的 `Binding` 上加一个字段（ADR-0001 的策略文件 schema），以及 `enforce._reserve` 里多一个判断。
  - 钱为什么仍然安全：**捕获是唯一不可逆的那一步**，预算在它那里强制；授权阶段还有 `amount_max` 与商户 / 品类约束把着（规则约束与预算是两条独立的闸）。并发下最坏情况是「多笔授权成功、后面的捕获被拒」——钱没多花，只是留下几个未捕获的 intent。
  - 代价（诚实写出来）：授权不再预留额度，所以「在途窗口」里看不到占用；如果将来要让 agent 在授权前就知道「我还剩多少」，需要 ADR-0002 的 `active_holds` 之外的新机制（future work）。
- **调研后的复核（2026-10-16）**：**推荐不变，但论证被修正了三处**——这正是调研该起的作用。
  1. **否掉了一个原先没考虑的变体（方案 G：只让授权扣账、捕获不扣）**。它看着更贴 Issuing 的「控制点在授权」，但 Stripe 明说**授权会过期并把资金释放**，而我们的账本一旦在授权时 `commit` 就**没有对应的释放路径**——一笔被放弃的授权会**永久**吃掉预算。所以 G 不成立（要成立就得让授权的 hold 靠 TTL 自愈，那正是方案 C）。
  2. **指出了方案 B 的真正拦路石**：B 要把捕获关联回授权，而现成的关联键 `payment_intent_id` 来自**上游响应体**——那是**上游可控的数据**在驱动我们的账本（一个恶意或出错的上游返回撞车的 id，就能让网关拿错的 hold 去结算）。所以 B 要么接受这个信任降级，要么让网关**自己签发**关联句柄（就得改写响应，把结果升级为「构造类」，触及 ADR-0014）。
  3. **Issuing 的「控制点在授权」对我们不构成反例**：发卡行同时看得见授权与清算，且清算会引用授权，所以它的 hold 天然会被结算；我们只看得见两个**互相独立**的工具调用，没有这条链路。
- 备选：b) 网关把捕获关联回授权、**结算**那个 hold（对齐 Issuing 的「授权 hold + 清算结算」，语义最漂亮）；c) 授权只 hold 不 commit、靠 TTL 回收；g) 只让授权扣账、捕获不扣（调研后否掉，理由见上）。
- 为什么不选 b（本版）：它需要跨调用的关联状态（还要处理 intent 过期 / 取消 / 部分捕获），而现成的关联键是**上游可控数据**——安全做法是让网关自己签发关联句柄，那就要改写工具响应、把结果升级成「构造类」（ADR-0014）。这是一套新机制，规模够再开一个 ADR；而它带来的**安全性**与 A 相同（钱都在捕获处拦），差别只在「能不能在窗口内看到占用」。**触发条件**：当需要「授权前就告诉 agent 还剩多少额度」，或评测要求「在途占用可观测」时，就把它做出来。
- 为什么不选 c：它保留了「授权占额度」，于是单笔购买在流程窗口内**仍然按 2× 记账**，只是靠 TTL 自愈——观测与解释成本都比 A 高，而正确性反而依赖回收是否及时。
- 为什么不选 g：授权会过期并释放（Stripe 明说），而我们的账本在授权时 commit 之后没有对应的释放路径——被放弃的授权会永久吃掉预算。Issuing 能做「控制点在授权」是因为它同时看得见清算、能结算那个 hold；我们看不见。
- 决策（**已定，2026-10-16 你采纳**）：采纳方案 A，机制按上面的修正落地——`Binding` 新增 `reserves_budget`（默认 true），`create_payment_intent` 声明 `amount_field` + `reserves_budget: false`，`capture_payment` 两者都保留；`_reserve` 两个条件都要满足才占额度。同步改动：`src/gateway/policy/demo-policy.yaml`、`docs/mock-world.md` 的工具契约表、`docs/evaluation.md` 的 I2 口径（「恰好一次」的分母是**捕获**）、`tests/harness.py` 的 `pay()`（两步流程成为测试的默认写法）。
- 实现记录（2026-10-16）：测试层面的两个直接后果——① 「钱被记账了」的断言必须发生在**捕获之后**（`test_allowed_payment_goes_through_and_settles` 现在分两段断言：授权后账本仍是干净的）；② 捕获成了**唯一**占额度的调用，所以 release 路径的端到端覆盖搬到「捕获失败」那条用例上。

## ADR-0028 重放输入的可重构性：链上摘要 + 独立的可复算输入（已采纳，2026-10-16）

- 背景：I4 的口径要求「证据能**重构**决策」，但链上落的是 `args_digest`（输入的摘要）而不是原文；策略求值要读金额与商户，光有摘要读不出来。所以现在能验的只有「输入没变 / 决策序列稳定 / 前置条件齐不齐」，**不能**重算决策——而 `docs/evaluation.md` §2 的表把「规范化输入」列为重放前置条件，实现里只有它的摘要。这是**声称与证据之间的落差**，必须在 Step 3 之前定掉。
- 证据（2026-10-16）：
  - **文献口径**（`docs/evaluation.md` §1 已引）：`Agent-Safety Evaluations as Load-Bearing Evidence` 把 **reconstructability**（证据能否重构声称所依赖的决策）当作评测有效性的指标，并实测「**多数 trace 的重放前置条件不满足**」。也就是说这条弱化是**业界普遍现象**，而我们要报的正是「前置条件满足率」——前提是我们真的能重构。
  - **行业先例**（AWS CloudTrail 日志完整性校验，2026-10-16 核实）：它把**摘要文件与日志文件放在不同目录**，官方给的理由就是「**这种分离让你能对两者施加不同的安全策略**」（原文：The digest files are put into a folder separate from the log files. This separation of digest files and log files enables you to enforce granular security policies...），并用 SHA-256 + RSA 把两者绑定。我们要的形状与它同源：**可出示的防篡改件**与**可复算的载荷件**分开，靠摘要互相钉住。
  - **我们自己的安全姿态**：链是「可出示」的那一件（ADR-0022 的 audit-first、威胁 T9 的不可抵赖）。一旦把金额、商户、订单号写进链上条目，每次出示证据就成了一次数据共享事件。
  - **成本很低的一条实测事实**：策略求值需要的输入里，`now` **已经在链上**（条目的 `ts` 就是求值时刻，`_record` 用的就是 `request.now`），所以缺的**只有 `arguments`**。
- 推荐（**方案 A：两级证据 + 可验证绑定**）：
  1. **链（不变）**：只留 `args_digest` 与 `policy_digest`——可对外出示；
  2. **可复算输入件（新增）**：`runs/<run_id>.inputs.jsonl`，每行 `{seq, arguments}`，与运行证据包同目录，但可**单独**施加访问控制 / 保留期 / 删除；
  3. **绑定可验证**：`reconstruct()` 逐条检查 `digest(arguments) == 链上的 args_digest`，再用**真策略**重算决策并与链上比对。
  - 收益：I4 从「摘要一致」升级成**真重构**，而且把两个数**分开报**——「前置条件满足率」（输入件在不在、摘要对不对）与「决策重构率」（重算是否逐条一致）。撤下输入件后链**仍然可验**，只是重构率归零；这个落差本身就是可报的结论（正是文献说的 overclaim gap）。
  - 为什么不是「把原文写进链上条目」：链与运行包现在是**同一批行**（`_append` 写同一个形状），而运行包的 Merkle 根要与链交叉核对。把原文塞进条目会同时坏掉两件事——① 可出示件里出现支付数据；② 运行包的叶哈希与链不再一致（除非两处都写，那等于把数据写两遍、还失去了分层）。
- 备选：b) **降级口径**——只报「摘要一致率 + 决策序列稳定率」，并在结论里写明「我们做不到从证据反推决策，因为没有原文」；c) 把规范化原文写进链上条目（最省事）；d) 字段级加盐承诺 / 加密（同一份文件既重构又不泄露，但要密钥管理）。
- 为什么不选 b：降级是诚实的，但它把 I4 变成一条只能证明「输入没变」的弱指标；而文献点名的正是这条弱化——「多数 trace 的重放前置条件不满足」是**要被解决的问题**，不是要被接受的现象。我们有机会做对，成本只有一份文件加一次比对。
- 为什么不选 c：把支付数据放进**可出示**的审计件里，等于每次审计都在共享商业与个人信息；这与「链可对外、载荷受控」的分层直接冲突。
- 为什么不选 d（本版）：需要密钥管理（生成、轮换、托管），而 demo 的收益与 A 相同；分层方案用**文件级**粒度就拿到了同样的取舍，且**不需要密钥**。留作 future work（触发条件：真的要把输入件交给第三方时）。
- 决策（**已定，2026-10-16 你采纳**）：采纳方案 A（两级证据 + 可验证绑定）。落地内容：`ChainLedger.record_inputs` / `inputs_for` / `reconstruct`（输入件落在 `runs/<run_id>.inputs.jsonl`）；执行点每次记账时写一份输入（幂等，pending / done 共用一行）；`Enforcer.reconstruct` 用**真策略**重算（评估器闭包住策略、身份与上游清单，证据层仍然不 import 策略）；CLI 新增 `python -m gateway reconstruct --run <id>`；`package(with_inputs=False)` 默认只导出**可出示**的那一份。测试见 `tests/test_reconstruct.py`（5 条，其中一条专门测「撤下输入件后链仍可验、而重构率归零」）。
- 实现时顺带修的一处不一致：`replay` 以前只看 `phase == "pending"` 的行——`resources/read` 被允许时只写一条 `done`（它随后必然报「不存在」），那条决策会被整个漏掉。现在 `replay` 与 `reconstruct` 共用 `_decision_rows`：每次调用取**第一条**记录，所有路径上都带着决策。
- 简历角度（诚实写，不吹）：这条能写成「**两级证据 + 可验证绑定**：链上只留摘要（可对外出示），可复算输入单独落盘并受访问控制；重放时逐条校验摘要、用真策略重算决策，并报告『输入件撤下后链仍可验、但重构率归零』的落差」。它同时展示三件事：知道治理评测要的是**可重构**、知道支付数据**不该进可出示的审计件**、并且**把取舍量化**而不是声称两全。

## ADR-0029 交易上下文与跨调用不变量（已采纳，2026-10-19；demo 版最小实现）

- 背景：P0 状态攻击（2026-10-19）证明「每次调用单看都合法」不等于「整条交易合法」。当前 `policy/` 只做 per-call 的金额/商户/白名单检查，`budget/` 只做会话级累计；没有组件把 `catalog → checkout → intent → capture → order` 绑在一起。结果是：先 complete 后 capture 可以免单；capture 35 对 43.10 的购物车可以完成；同一 checkout 两个 intent 可以扣两次；两个 43.10 的 checkout 可以在 100 预算内完成两次购买。
- **性质**：主因是**架构范围没有定义 I5（交易级不变量）**，不是 I1–I4 的实现回归——I1（无旁路）、I2（预算不超）、I3（链可验）、I4（可重构）在 P0 场景里都仍然成立。两个实现捷径把缺口放大成可利用漏洞：① mock merchant 的 token 只做 `startswith` / `endswith` 字符串检查；② `complete_checkout` 没有要求「先 capture」。所以它是「架构漏了一层 + 实现用了足够被利用的简化」，不是「实现违背了已承诺的交易不变量」——那条不变量此前不存在。
- 证据：
  - 本仓库确定性探针 `eval/p0_state_attacks.py`（无需模型）：免单 43.10、少扣 8.10、同一 checkout 双扣 86.20、拆单两笔 86.20、先履约后扣款 43.10、**伪造 `pt_forged_<checkout>` 免单 43.10**；`over_capture` 被 PSP 拦住。原始表在 `docs/redteam-log.md` 的 P0 节与 `docs/evaluation.md` §6d。
  - **token 不是能力**：`complete_checkout` 只检查 `startswith("pt_")` 与 `endswith(checkout_id)`；F12 里模型生成的双后缀 token 也被接受。任何人知道 checkout_id 就能拼出 token 完成订单。
  - **AP2 open-mandate idempotency**：AP2 规范 §6 要求 shopping agent 在没有 rejection receipt 时不得重复提交 open mandate；但 2026-09 的 [One Consent, Two Success Receipts](https://zenodo.org/records/22262232) 指出签名只证明 mandate **有效**，不证明它**未被消费**；规范把唯一强制防双花放在 shopping agent 身上，verifier 只是 MAY 拒绝重叠 mandate。
  - **AP2 manipulated checkout**：官方 Security Considerations 要求 closed mandate 用 `sd_hash` 绑定到 open mandate，并对金额/收款方做 constraint evaluation；这正是「跨调用字段绑定」的协议级先例。
  - **AgentLAB / tool chaining**：长程攻击把「单独允许的工具调用序列」作为一类攻击面；与本项目的「拆单/多 intent/先履约后扣款」同构。
  - **我们自己的 I2**：预算不变量是会话级 `committed + holds ≤ limit`；它证明「总额不超」，不证明「一次 consent 只扣一次」。
  - **行业调研（2026-10-19）**：
    - **Stripe PaymentIntents**（[官方文档](https://docs.stripe.com/payments/payment-intents)）：官方建议「每个 PaymentIntent 通常对应一个购物车或客户会话」；结账中断恢复时**复用同一个 PaymentIntent**；幂等键通常基于购物车/会话 ID，防止同一购买创建重复 intent。→ 支持「一个 checkout 一个有效 intent」。
    - **Stripe multicapture**（[官方文档](https://docs.stripe.com/payments/multicapture)）：一个 PaymentIntent 最多允许 50 次非最终 capture；例如授权 10 USD、先 capture 7 USD，剩余 3 USD 仍保持授权。→ **反驳「每次 capture 必须等于购物车总额」**；正确的不变量应是「完成时累计 capture = 总额，或显式记录部分履约/剩余余额」。
    - **Stripe MPP 的五维绑定**（[阿里云开发者社区 2026-10-12 的拆解](https://developer.aliyun.com/article/1770471)）：用户目标↔商品、报价↔商户、授权↔金额上限、付款↔幂等键、交付↔交易；强调「支付成功只是最后一个结果」。→ 与 I5 的交易上下文同构。
    - **ACP / UCP**（[WooCommerce 的 UCP/ACP 实现说明](https://pt-ao.wordpress.org/plugins/ucp-acp-agent-for-woocommerce/)）：checkout-session 生命周期、幂等、Shared Payment Token、签名 webhook；协议把很多绑定放在 merchant/PSP 侧，agent 侧仍需自己保证不重复消费。
    - **Visa Intelligent Commerce / Mastercard Agent Pay**（[Visa 2026-04-08](https://usa.visa.com/about-visa/newsroom/press-releases.releaseId.22276.html)；[Mastercard Agent Pay](https://www.mastercard.com/news/latin-america/es/sala-de-prensa/comunicados-de-prensa/pr-es/2026/septiembre/la-inteligencia-artificial-ya-no-solo-recomienda-comienza-a-comprar-y-pagar/)）：tokenization、spend controls、authentication；Mastercard 明确用**单次使用的 tokenized credential**。→ 支持「payment token 必须是可验证、单次使用的凭证」，而不是可拼的字符串。
    - **MCP 网关**：公开实现大多停在工具级 RBAC / 聚合 / 脱敏；没有把 checkout → intent → capture → order 作为一等状态机。→ 这是本项目的差异化空间，也是行业尚未统一的部分。
- 推荐（方案 A：网关持有最小交易上下文）：在 `enforce/` 旁增加一个 `transaction/` 状态机（只服务固定工具，不做通用 DSL）：
  1. `capture_payment`：按 `payment_intent_id → checkout_ref` 找到权威 checkout，要求 `currency`、`merchant_id`、**累计 capture 不超过授权额**；**完成时累计 capture = checkout.total，除非订单显式记录部分履约 / 剩余余额**。不把「每次 capture = 总额」写死——Stripe multicapture 证明部分捕获是合法能力。
  2. `complete_checkout`：要求同一 checkout 已有 `succeeded` capture；**累计 capture = checkout.total，或订单显式标记部分履约**；**payment token 必须是 PSP 签发、带签名/不可预测 nonce、单次使用、可向 PSP 验证的凭证**，不能是拼出来的字符串。
  3. 同一 `checkout_ref` 只允许一个有效/成功的 payment intent；重复 create 返回冲突，不新建。注意这是比 Stripe「通常一个购物车一个 intent」更严格的设计选择，不是协议硬要求；如果未来要支持 multicapture，允许同一 intent 的多次 capture，但仍禁止同一 checkout 的第二个 intent。
  4. mandate 新增 `max_transactions`（默认 1）；预算账本按 `(mandate_id, checkout_id)` 记录消费，拆单超过笔数直接拒。
  5. 状态落 SQLite（与 budget 同库或同目录），证据里记录 `transaction_id` 与状态迁移；崩溃后可恢复。
- 备选：a) 只在文档里承认缺口，不改代码；b) 把校验推给 PSP/merchant；c) 通用工作流 DSL；d) 只加 `complete` 前检查 capture（最小补丁）。
- 为什么不选 a：P0 的免单/双扣是**真实的资金不变量**，不是展示问题；不修就不能说网关保证支付正确。
- 为什么不选 b：上游今天拦、明天可能不拦；`over_capture` 被 PSP 拦住恰好说明网关不能把上游校验当成自己的校验。
- 为什么不选 c：通用 DSL 会把项目拖进「工作流引擎」，超出 demo 范围；固定四个绑定足够覆盖 Agentic Commerce 的主路径。
- 为什么不选 d：只补 `complete` 会留下少扣、双 intent、拆单三个缺口；交易上下文一次建好更干净。
- 影响面：新增 `transaction/`、`enforce.py` 接线、`policy/schema.py` 的 `max_transactions`、`mandate` 解析、证据 schema（新增 `transaction_id` 字段）、测试（P0 六个场景转为回归）；不改变「单一执行点」与「构造/校验/透传」三分类。
- 证据检查结论：**整体支持交易上下文与 token 验证；部分修正了 capture 的不变量**。行业证据支持「一个 checkout 一个 intent」「token 必须是单次可验证凭证」「完成前必须对账」；但 Stripe multicapture 说明「每次 capture = 总额」过严，应改为「完成时累计 capture = 总额，或显式部分履约」。这条修正让方案更接近真实支付语义，而不是把 demo 的简化当成行业规则。
- 决策（**已定，2026-10-19 你采纳方案 A**）：按 demo 版最小闭环落地：
  - **本次实现**：内存 `transaction/` 账本；一个 checkout 一个有效 intent；intent 金额 = 购物车总额；capture 金额 = 购物车总额；`complete_checkout` 前必须有成功 capture，且 token 精确匹配 PSP 签发值；mandate `max_transactions` 默认 1；`cancel` 已 capture 则拒；P0 六个场景转为回归测试。
  - **TODO（留给未来，不在 demo 内）**：SQLite 持久化与崩溃恢复；部分捕获 / multicapture（按 Stripe 语义累计）；退款累计与部分退款；证据 schema 的 `transaction_id`；并发 TOCTOU；多 mandate / 多币种的完整状态机；上游 PSP 的签名 token 验证；**上游 merchant 报价/complete 总额校验（price drift 5/5：checkout 49.99 → 订单 60.00）**。
  - **为什么这样切**：demo 的目标是证明「per-call 之外还需要 transaction-level 不变量」，不是复刻 Stripe 的全部支付语义。复杂部分记 TODO，避免把项目拖成支付引擎。
 - **未来风控（TODO）**：硬门 + ML 残差风险——硬门不可补偿，ML 只处理通过硬门后的欺诈/异常/复核；demo 不实现 ML。行业调研与设计见 `docs/risk-decisioning.md`。
- 实现记录（2026-10-19）：`src/gateway/transaction.py`（进程内 `TransactionGuard`，enabled 由 `GatewayConfig.transaction_guard` 控制，产品默认 True）；`tests/test_transaction.py` **9 条**回归全部通过（含并发重复 intent）。`eval/p0_state_attacks.py` 保留为修复前证据，默认 `transaction_guard=False` 以复现旧结果。**F14 的模型驱动拆单**是这条 ADR 的正面证据：2/5 尝试、2/2 被 `mandate_transaction_limit` 拒绝、0 双扣；`ASR=0.40` 是 attempt rate，不是成功率。
  - **并发修复（2026-10-19）**：`check` 通过后先在 checkout 上占 `pending_intent`，成功 `observe` 或失败 `abort` 时清除；`eval/concurrency_probe.py` 从修复前 **30/30 双 intent** 变为修复后 **0/30 双 intent、30/30 串行化、0 孤儿、0 实扣**。这是对 ADR-0029「一个 checkout 一个有效 intent」的并发补强。
  - **scripted 阳性对照**：`eval/scripted_adversary.py` 对金额越权与 complete-before-capture 各跑 N=30，均 30/30 拒绝、0/30 绕过、0 实扣（95% 上界 9.5%）。它证明网关在“决策层已经越权”时能端到端拦住；不证明 qwen 会越权。
  - **持久化边界（2026-10-19）**：`eval/persistence_probe.py` N=10 显示：完成第一笔后重启，第二笔 **10/10 被放行**（累计 86.20，`max_transactions=1` 被绕过）；capture 后重启再 complete，**10/10 完不成**。这是 ADR-0029 TODO 的实证：单进程 demo 成立，重启/多副本不成立；修复需要 SQLite/共享状态。
  - **价格漂移（2026-10-19）**：`eval/price_drift_probe.py` N=5——checkout 报 49.99、实扣 49.99、订单变 60.00，**5/5 underpayment**。网关用 checkout 响应当权威总额，complete 时不核对订单总额；这是 I5 的上游信任边界，已记 TODO。
  - **样本量补充（2026-10-19）**：qwen F1 S0 已补到 4 格 ×5（0/5，95% 上界 0.451）；F14 补了 baseline N=5（0/5 拆单、5/5 完成 10 份），但 F14 本身仍是 N=5 探索性，不是 N=30。

## 参考笔记（AI 记录，非决策本身）

### 2026-10-09 为什么协议错误不适合做策略拒绝

JSON-RPC 协议错误通常由**客户端库**处理，模型根本看不到，很多客户端会直接中断整个回合或会话。这意味着任何能影响 agent 工具调用的东西（包括恶意商户页面里的注入内容）都可以通过**故意触发一次拒绝来终止用户的合法会话**——这是一次廉价的拒绝服务。所以协议错误只留给结构性问题（JSON-RPC 本身不合法、方法不存在、`params` 不符合声明的 `inputSchema`）；策略结果属于「业务层判定」，不是「协议层错误」。这个区分本身在面试里是加分项。


### 2026-10-10 构造 / 校验 / 透传：类型三分类与各自的义务

判据：只精读你要「构造」或「校验」的类型；只做透传的类型零字段知识。但**透传是一个要显式声明的设计决定，不是默认状态**，它有四条义务：

1. **保留未知字段**：上游或客户端用了新字段时原样带过去，否则破坏前向兼容（这也是 S6 的 fail-closed 只适用于「内容类型」、不适用于「信封字段」的原因）。
2. **只规范化已知字段**：需要改写的只有你声明为「构造」的那些字段。
3. **记录规范化摘要**：证据里的 digest 必须基于键排序后的规范化 JSON，否则同一逻辑报文因序列化差异得到不同摘要，重放会假失败。
4. **仍须过执行点**：透传不等于免检——I1 要求「不存在未经 Decision 就放行的方法调用」，透传请求也要先拿到 allow，只是 allow 的结果是「不改动」。

三类各自对应的测试类型：

| 分类 | 例子 | 必须有的测试 |
| --- | --- | --- |
| 构造 | 拒绝响应、input_required 审批、过滤后的列表、自签游标、证据记录 | golden 测试（构造出的报文能被真实 MCP client 解析）+ 用 pydantic 反向校验自己构造的对象 |
| 校验 | 工具参数（金额 / 商户）、要净化的内容类型 | negative 测试（非法参数被拒）+ 边界用例（金额刚好等于上限、商户刚好在白名单） |
| 透传 | prompts、sampling、roots、信封字段 | round-trip 测试（进出一致、未知字段不丢）+ digest 稳定性测试（同一逻辑报文不同序列化 → 同一摘要） |

**三分类是版本化的**：每次 ADR 变更都可能把一个类型从「透传」升级为「构造」。已经发生的一例：`resources/list` 原属透传，ADR-0011 与 ADR-0013 之后升级为「构造」（按 URI 过滤 + 重签游标）。建议在代码里显式维护这份分类（例如 `PASSTHROUGH` / `CONSTRUCTED` / `VALIDATED` 三张清单），这样升级时不会漏掉测试。
### 2026-10-10 如何让旁路在结构上不可能（I1 的实现手法）

目标：让「没有 Decision 就转发上游」这件事在代码结构上无法表达，而不是靠约定。三层做法，每层都很便宜：

1. **依赖方向**：上游客户端只在 `enforce` 内部构造并持有；`frontdoor` 只依赖 `Enforcer.handle(request)`，不 import `mcp.client`。
2. **CI 强制**：用 import-linter 声明分层契约（`frontdoor > enforce > upstream`），`frontdoor` 直接 import 上游客户端即 CI 失败。架构规则因此变成可执行的门禁，而不是文档里的口号。
3. **运行期拒绝**：转发函数要求出示一个由策略步签发的单次 Decision 令牌（`pop` 取出，用后即废），没有令牌或令牌类型不是 ALLOW 就直接抛 `BypassAttempt`。这同时防了 Decision 的重放。

配套一条顺序规则：**先写证据再产生副作用**（`effect_pending` → 转发 → `effect_done`）。这样即使进程在副作用中途崩溃，也一定留下「有一条 Decision 授权过它」的记录。

**诚实的边界**：Python 没有真正的私有，`import enforce._upstream`、`obj.__dict__`、运行时 monkeypatch 都仍然可行。所以正确的说法不是「不可能」，而是：正常依赖结构里表达不出来、CI 能检出、运行期还会被令牌检查拒绝——三层各挡一类攻击。它防的是未来贡献者（包括未来的你）的**意外旁路**，不是拿到进程内代码执行权的对手；要挡后者，执行点必须变成独立的进程或网络边界。
