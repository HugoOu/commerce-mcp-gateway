# 架构决策记录（ADR）

> 格式：决策 / 备选 / 为什么不选 / 后来证伪了什么。
> 分工：AI 可以帮你查既有实践，判断必须由你写。

## ADR-0001 策略语言边界

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

### ADR-0007 建议方案（AI 建议，2026-10-09，待你确认）

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

## ADR-0008 工具名冲突消歧（候选，待决策）

- 背景：工具名唯一性只在单个 server 内成立；网关聚合 merchant 与 PSP 会撞名；规格建议加 server 标识前缀，且不能依赖 serverInfo 的 name。
- 备选 / 决策 / 为什么不选：TODO(你)

## ADR-0009 是否采用 x-mcp-header（候选，待决策）

- 背景：该扩展把参数镜像进 HTTP 头供中间人使用；规格禁止镜像敏感参数。
- 备选 / 决策 / 为什么不选：TODO(你)

## ADR-0010 tools/list 的过滤与缓存语义（候选，待决策）

- 背景：tools/list 结果可因授权不同而不同（可做工具隐藏），并带 ttlMs / cacheScope；若上游声明 public 而网关的过滤结果依赖 agent 身份，直接沿用会越权泄露。
- 备选 / 决策 / 为什么不选：TODO(你)

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

## ADR-0012 能力暴露与聚合（候选，含证据链，待你决策）

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

## ADR-0013 分页与过滤语义（候选，含证据链，待你决策）

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
## 参考笔记（AI 记录，非决策本身）

### 2026-10-09 为什么协议错误不适合做策略拒绝

JSON-RPC 协议错误通常由**客户端库**处理，模型根本看不到，很多客户端会直接中断整个回合或会话。这意味着任何能影响 agent 工具调用的东西（包括恶意商户页面里的注入内容）都可以通过**故意触发一次拒绝来终止用户的合法会话**——这是一次廉价的拒绝服务。所以协议错误只留给结构性问题（JSON-RPC 本身不合法、方法不存在、`params` 不符合声明的 `inputSchema`）；策略结果属于「业务层判定」，不是「协议层错误」。这个区分本身在面试里是加分项。


### 2026-10-10 如何让旁路在结构上不可能（I1 的实现手法）

目标：让「没有 Decision 就转发上游」这件事在代码结构上无法表达，而不是靠约定。三层做法，每层都很便宜：

1. **依赖方向**：上游客户端只在 `enforce` 内部构造并持有；`frontdoor` 只依赖 `Enforcer.handle(request)`，不 import `mcp.client`。
2. **CI 强制**：用 import-linter 声明分层契约（`frontdoor > enforce > upstream`），`frontdoor` 直接 import 上游客户端即 CI 失败。架构规则因此变成可执行的门禁，而不是文档里的口号。
3. **运行期拒绝**：转发函数要求出示一个由策略步签发的单次 Decision 令牌（`pop` 取出，用后即废），没有令牌或令牌类型不是 ALLOW 就直接抛 `BypassAttempt`。这同时防了 Decision 的重放。

配套一条顺序规则：**先写证据再产生副作用**（`effect_pending` → 转发 → `effect_done`）。这样即使进程在副作用中途崩溃，也一定留下「有一条 Decision 授权过它」的记录。

**诚实的边界**：Python 没有真正的私有，`import enforce._upstream`、`obj.__dict__`、运行时 monkeypatch 都仍然可行。所以正确的说法不是「不可能」，而是：正常依赖结构里表达不出来、CI 能检出、运行期还会被令牌检查拒绝——三层各挡一类攻击。它防的是未来贡献者（包括未来的你）的**意外旁路**，不是拿到进程内代码执行权的对手；要挡后者，执行点必须变成独立的进程或网络边界。
