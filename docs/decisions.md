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

## ADR-0003 证据语义

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

## ADR-0004 网关传输选择（Step 3 决定）

- 决策：TODO(你)
- 依据：TODO(你)

## ADR-0005 SDK 代理能力与转发实现方式（Step 3 决定）

- 决策：TODO(你)
- 依据：TODO(你)


## ADR-0006 审批闸门机制（候选，待决策）

- 背景：2026-07-28 起 `tools/call` 可用 `resultType: "input_required"` 走 mrtr 机制暂停等人；架构第 7 条目前写的是「审批是 durable wait，投递通道为 CLI 与 MCP elicitation」。
- 证据：
  - 规格给了两条 in-band 通道：`elicitation`（客户端能力）与 `input_required` + mrtr（工具调用可暂停等人）；且 Servers MAY respond to tools/call with an InputRequiredResult——网关作为 agent 的 server，可以**自己合成** input_required 来做审批，不必等上游发起。
  - 既有实现（2026-10-10 调研）：
    - node9-proxy：危险动作 held for review，审批就在对话里（inline review），也可走团队审批人（dashboard / Slack）；明确 held action 在你回答前绝不执行（fail-closed）。
    - cordum：策略判定里有 `REQUIRE_APPROVAL` 这一类；且**不信任审批记录本身**——要求同一租户下已 resolved 的审批审计事件、`approval_ref` 与 `action_hash` 同时匹配，光有审批请求事件不算。
- 推荐：默认走**协议原生**——网关自己合成 `input_required`，把 `requestState` 持久化到 SQLite（满足 durable wait），CLI 作为兜底通道；审批必须**绑定动作指纹**（参数摘要 + 策略版本）且单次使用；等待期间动作绝不执行（fail-closed）；超时默认拒绝。
- 备选：a) 纯协议原生（input_required / elicitation）；b) 纯自造 CLI 通道；c) 两者并用。
- 决策（已定，2026-10-10）：采纳推荐方案——协议原生 input_required + CLI 兜底；审批绑定动作指纹且单次使用；等待期间动作绝不执行（fail-closed）；超时默认拒绝；`requestState` 持久化到 SQLite。
- 为什么不选 a：纯协议原生依赖客户端支持 elicitation / mrtr，通用性差。
- 为什么不选 b：纯自造通道不可被通用 MCP 客户端使用，且无法复用协议的状态语义。
- 注：会触及先验架构，按 AGENTS.md 需要证据 + User 认可。

## ADR-0007 策略拒绝的返回形态（候选，待决策）

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
- 证据：
  - 规格：工具名 SHOULD 只允许 `A-Za-z0-9`、`_`、`-`、`.`；唯一性只在单个 server 内成立；聚合方 SHOULD 用 server 标识前缀消歧。
  - 既有实现（2026-10-10 调研）：openziti/mcp-gateway 自动加前缀，用 `fs:read_file` 形式——注意它用了 `:`，而 `:` 不在规格建议的字符集内；metamcp 用 namespace 分组并支持按 namespace 覆盖工具名。
- 推荐：由网关用**配置里的 server 别名**生成前缀，格式 `<alias>__<tool>`（双下划线，落在规格允许字符集内）；别名重复或加前缀后仍冲突则**启动即失败**（fail fast）；映射关系写入证据，便于从一次调用反查上游 server。
- 决策（已定，2026-10-10）：采纳推荐方案。
- 备选：不加前缀（撞名即拒绝）；用 `:` 作分隔符（对齐 openziti 的做法）。
- 为什么不选：不加前缀无法同时聚合 merchant 与 PSP；`:` 不在规格建议的字符集内（规格只允许字母、数字、`_`、`-`、`.`）。

## ADR-0009 是否采用 x-mcp-header（候选，待决策）

- 背景：该扩展把参数镜像进 HTTP 头供中间人使用；规格禁止镜像敏感参数。
- 证据：
  - 规格：把工具参数镜像成 `Mcp-Param-{name}` HTTP 头，供负载均衡 / 代理 / WAF 在不解析消息体的情况下路由与检查；约束含仅限原始类型（integer / string / boolean）、必须静态可达、整数须在 IEEE754 安全范围内、大小写不敏感唯一、不得含控制字符；并明确警告不要把密码 / 密钥 / 令牌 / PII 标成 header。
  - 采用情况（2026-10-10 调研）：按 `x-mcp-header` / `Mcp-Param` 在 GitHub 检索几乎没有真实采用，属全新扩展。
- 推荐：**不主动使用**（网关本来就要解析 body，用它没有收益），但实现两条防御——转发时保留上游声明的 `Mcp-Param-*` 头（否则破坏上游的中间人语义）；策略层检查敏感参数是否被镜像成 header，命中则拒绝或告警。
- 决策（已定，2026-10-10）：采纳推荐方案。
- 备选：主动使用它做参数级路由与检查。
- 为什么不选：网关本来就要解析 body，用它没有收益；而且会把参数暴露给链路上所有中间人（规格明确警告不要把密码 / 密钥 / PII 标成 header）。

## ADR-0010 tools/list 的过滤与缓存语义（候选，待决策）

- 背景：tools/list 结果可因授权不同而不同（可做工具隐藏），并带 ttlMs / cacheScope；若上游声明 public 而网关的过滤结果依赖 agent 身份，直接沿用会越权泄露。
- 证据：
  - 规格：列表操作带 `ttlMs` 与 `cacheScope`，且结果可因授权不同而不同。
  - 先例（2026-10-10 调研）：HTTP 的经典解法是 `Vary`——在响应里声明除方法与 URL 外还有哪些请求头影响了内容，从而让缓存按这些维度分键（MDN：Vary 用来为内容协商创建 cache key）；metamcp 则用按 endpoint / namespace 划分视图的方式规避跨客户端复用。
- 推荐：网关缓存上游列表时按（上游标识, 上游游标）分键；对外返回的列表按**授权指纹**分键，绝不跨授权复用；若上游声明 `cacheScope: public` 而你的过滤依赖 agent 身份，出口必须收紧为 `private`（或按指纹分区），等价于给列表加上 `Vary: Authorization`。
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
## ADR-0014 显式维护类型分类清单（候选，待你决策）

- 背景：网关对协议类型分「构造 / 校验 / 透传」三类（见 `architecture.md` 第 7 节）。ADR 变更可能把某个类型从透传升级为构造（已发生一例：`resources/list`）。
- 证据（2026-10-11 调研）：2026-07-28 通过 **SEP-2577** 批量弃用了一批接口（logging 的 RPC、roots、sampling 的 `ModelHint` / `ToolChoice`、`ToolResultContent` / `ToolUseContent`），并设了 deprecated features registry——说明协议面本身会变。若分类只存在脑子里，升级或弃用时不会触发对应的测试补齐。
- 推荐：在代码里维护三张显式清单（`PASSTHROUGH` / `CONSTRUCTED` / `VALIDATED`），并让 CI 检查「每个构造类类型都有 golden 测试、每个校验类类型都有 negative 测试」。附带收益：可以拿清单快速核对规格的弃用清单，避免把弃用接口当推荐路径。
- 备选：只在文档里维护分类（成本更低，但不会在 diff 里可见）。
- 决策 / 为什么不选：TODO(你)
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
