# 架构决策记录（ADR）

> 格式：决策 / 备选 / 为什么不选 / 后来证伪了什么。
> 分工：AI 可以帮你查既有实践，判断必须由你写。

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

## ADR-0005 SDK 代理能力与转发实现方式（候选，含 AI 已核实事实，待你决策）

- 背景：网关需要把请求转发到上游 MCP server，但 SDK 没有内置代理封装（2026-10-04 已核实）；Step 3 又跑通了 hello-MCP，需要据实测事实决定转发怎么写。
- AI 已核实的事实（2026-10-15，SDK 2.3.0）：
  - 没有内置代理：`mcp.server.mcpserver` 里不存在 proxy 实现；转发逻辑要自己写。
  - 但有两个可用抓手：`MCPServer.middleware`（中间件钩子）与 `MCPServer.streamable_http_app` / `session_manager`（可直接挂进自己的 ASGI 应用）。
  - 客户端侧：`ClientSession` 有 `dispatch_input_request`（MRTR 钩子）、`discover` / `send_discover`（版本探测）、`validate_tool_result`（输出校验）、`subscribe_resource`。
  - 协议版本：`HANDSHAKE_PROTOCOL_VERSIONS` 到 `2025-11-25`，`MODERN_PROTOCOL_VERSIONS` 只有 `2026-07-28`；两条传输默认都协商到 `2025-11-25`。
  - 两条传输都跑通（stdio 与 Streamable HTTP）；工具异常返回 `isError=True` 的 result（验证 ADR-0007）。
- 决策：TODO(你)
- 依据：TODO(你)


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
## ADR-0021 审批与敏感信息的通道选择（候选，含证据链，待你决策）

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
- 决策 / 为什么不选：TODO(你)

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
- 推荐：定位锁为**面向 agentic commerce / pay 的受控执行与验证控制面**，明确不与其竞争通用代理能力，并采用以下清单：
  - **借鉴**：短路拒绝（不触碰 target）；管道有序且 `audit` 注册在最前；自陈局限的写法；三层防御（意图 / 门口 / 回程）的表述。
  - **不借鉴**：20+ Lens 的广度；上下文预注入；关键词式 SQL 守卫；短生命周期 stdio 形态。
- 需要的架构补充（待你认可，按 AGENTS.md）：把 tools/call 时序里的「写证据」拆成**转发前 `record_pending`** 与**转发后 `record_done`**，让「证据先于副作用」成为架构规则——JanuScope 的 audit-first 顺序规则是这条的外部佐证。
- 备选：a) 做通用代理，与 JanuScope 正面竞争；b) 只做策略，不做证据与审批；c) 定位为控制面并明确借鉴清单。
- 为什么不选 a：通用代理是已有成熟实现的赛道（AGPL 生态里有多个），且与你的差异化（可复算证据、预算、审批）无关。
- 为什么不选 b：放弃证据与审批等于放弃你唯一的不可替代性——那是模型与框架都拿不走的部分。
- 决策（已定，2026-10-15）：采纳推荐方案——定位锁为「面向 agentic commerce / pay 的受控执行与验证控制面」；按借鉴清单执行（借鉴短路拒绝、audit-first 顺序、自陈局限、三层防御；不借鉴 Lens 广度、上下文预注入、关键词 SQL 守卫、短生命周期 stdio）；并采纳架构补充（证据拆成 `record_pending` / `record_done`，见 `architecture.md`）。
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
