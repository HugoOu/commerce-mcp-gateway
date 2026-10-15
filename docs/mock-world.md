# mock 世界：业务语义与工具契约（Step 1）

> 依据（2026-10-15 调研）：AP2 v0.2（五角色、Checkout / Payment Mandate、Human Present / Not Present 与 `unresolved_constraint`）、ACP（checkout session 生命周期、权威购物车状态、`payment_data` 里的支付令牌）、x402（HTTP 402 式支付）、Stripe（授权 → 捕获与幂等键，见 ADR-0002）。出处见文末。
> 分工：语义与契约由 AI 起草（你授权），**你负责检查与确认**；骨架代码由 AI 写，三处核心（策略求值 / 预算原子扣减 / 哈希链证据）仍是你的（W3）。
> 原则：mock 要**长得像真实世界的对端**——不是为了跑通，而是让 W3 / W4 的策略、预算、证据、对抗用例都有真实落点。

## 1. 我们模拟的是谁

AP2 定义了五个角色：Shopping Agent（SA）、Credential Provider（CP）、Merchant（M）、Merchant Payment Processor（MPP）、Trusted Surface（TS），并注明「一个实体可以扮演多个角色」。映射如下：

| AP2 角色 | 本项目里的对应物 |
| --- | --- |
| SA | `src/agent/driver.py`（薄循环） |
| M | **mock merchant MCP**（`src/mcp_servers/merchant.py`） |
| CP + MPP | **mock PSP MCP**（`src/mcp_servers/psp.py`） |
| TS | 网关的审批闸门（ADR-0006 的 `input_required` + CLI）——**网关站在 TS 的位置上** |
| —— | 网关是 AP2 里**没有**的角色：它坐在 SA 与 M / CP / MPP 之间做策略、预算、证据 |

这条映射本身就是简历上的话术：**AP2 的五角色模型里没有「网关」这个位置，我们补的正是它**。

## 2. mandate：把用户意图变成可检查的约束

AP2 的 mandate 是可验证数字凭证（VDC）：open mandate 记录「购物车未定之前的约束与目标」，closed mandate 绑定到「某个已定 checkout」（`transaction_id` 是 `checkout_jwt` 的哈希）。我们用同一个两阶段结构，但**不做密码学签名**（取舍见 §7）。

```yaml
mandate_id: mnd_2026_10_15_001
kind: open                      # open | closed
currency: CNY
constraints:
  budget_total: 100.00          # 整个 run 的累计上限
  per_txn_max: 50.00            # 单笔上限
  allowed_merchants: [m1]       # 商户白名单
  allowed_categories: [food]
  expires_at: 2026-10-15T20:00:00+08:00
binding:                        # 只有 closed 才有
  checkout_ref: ck_7f3a
  amount: 42.00
```

对应关系：AP2 closed Payment Mandate 的必填 `payee` → `allowed_merchants`；`transaction_id` 的绑定 → `binding.checkout_ref`；open / closed 两阶段 → `kind`。ADR-0001 的策略算子只需要读这几个字段。

另外，AP2 的 flows 里有一个对 ADR-0006 直接有用的模式：自主流程遇到 mandate 覆盖不了的情况时，Merchant 或 CP 返回 **`unresolved_constraint`**，把用户拉回流程里批准 closed mandate。这就是我们的 `require_approval`：**约束无法自动判定 → 交给人**，而不是硬拒。

## 3. 一笔完整交易的状态机（两边都遵守）

```
[merchant]  create_checkout ──► open ──► update_checkout ──► open（重算总额）
                                   ├─ complete_checkout(payment_token) ──► completed（生成 order）
                                   └─ cancel_checkout ──────────────────► cancelled
[psp]       create_payment_intent ──► requires_capture（授权 / 占用，未扣款）
                                   ├─ capture_payment ──► succeeded
                                   ├─ cancel_payment_intent ──► canceled（释放）
                                   └─ refund_payment ──► refunded
```

顺序约束（真实世界就是这么要求的，也是 W3 预算两阶段的依据）：

- `complete_checkout` **必须**带一个由 PSP 签发的支付令牌（ACP 的 `payment_data`；Stripe 侧叫 Shared Payment Token），merchant 校验令牌与 checkout 的绑定；缺失或不匹配 → 拒绝且**不生成订单**。
- PSP 的 `create_payment_intent` 是「授权 / 占用」（对应 ADR-0002 的 reserve），`capture_payment` 才是扣款（commit）；merchant 的 `complete_checkout` 只在 capture 成功后进入 `completed`。
- 任何一步失败都要能回滚：`cancel_payment_intent` 释放占用，`cancel_checkout` 释放购物车。

## 4. mock merchant 的工具契约

| 工具 | 入参 | 出参 | 依据 |
| --- | --- | --- | --- |
| `list_catalog` | `query?`, `category?`, `max_price?` | `items[]`（`sku` / `title` / `description` / `price` / `currency` / `stock` / `category`） | ACP 把发现留给平台；目录做成 fixture，便于注入对抗内容 |
| `create_checkout` | `sku`, `quantity`, `fulfillment_option?` | `checkout_id`、`status=open`、`line_items[]`、`totals{subtotal, shipping, tax, total}`、`currency`、`expires_at`、`messages[]` | ACP `POST /checkout_sessions` + 「每次响应都返回权威购物车状态」 |
| `get_checkout` | `checkout_id` | 权威购物车状态 | ACP `GET /checkout_sessions/{id}` |
| `update_checkout` | `checkout_id`, `quantity?`, `fulfillment_option?` | 重算后的权威状态 | ACP `POST /checkout_sessions/{id}` |
| `complete_checkout` | `checkout_id`, `payment_token`, `mandate_ref?` | `order_id`、`status=completed`、`permalink`、`receipt{amount, currency, paid_at}` | ACP complete + AP2「Merchant 校验 mandate 的内容与完整性」 |
| `cancel_checkout` | `checkout_id` | `status=cancelled` | ACP cancel |
| `get_order` | `order_id` | `status`、`fulfillment`、`permalink` | ACP 支持异步购买 |
| `request_refund` | `order_id`, `amount?`, `reason` | `refund_id`、`status=refund_requested` | 真实退款走 PSP 的 `refund_payment`，这里只做受理与状态 |

**对抗面（刻意保留，供 W4 用）**：`description` 与 `messages[]` 是不可信内容进入模型上下文的两条真实通道——ACP 明确规定购物车状态里带 `messages`，所以把注入载荷放这里既真实又合规。

## 5. mock PSP 的工具契约

| 工具 | 入参 | 出参 | 依据 |
| --- | --- | --- | --- |
| `create_payment_intent` | `amount`, `currency`, `merchant_id`, `checkout_ref`, `mandate_ref?`, `idempotency_key` | `payment_intent_id`、`status=requires_capture`、`payment_token`、`expires_at` | AP2 的 CP / MPP 分工；Stripe 的授权 → 捕获 |
| `capture_payment` | `payment_intent_id`, `amount?`, `idempotency_key` | `status=succeeded`、`captured_amount`、`receipt_id` | 同上 |
| `cancel_payment_intent` | `payment_intent_id`, `idempotency_key` | `status=canceled` | 释放占用 |
| `refund_payment` | `payment_intent_id`, `amount?`, `reason`, `idempotency_key` | `refund_id`、`status=refunded` | 退款 |
| `get_payment` | `payment_intent_id` | 当前状态 | 对账 |
| `get_spend_controls` | `mandate_ref` | `per_txn_limit`、`daily_limit`、`allowed_merchants`、`spent_today` | 网络侧消费控制（Visa / Mastercard 的 agentic token 都有类似控制）；给网关一个**外部 ground truth** 去对照自己的账本 |

**幂等语义（照 Stripe 来，ADR-0002 已引用）**：同一个 `idempotency_key` 重复调用 → **原样重放首次响应**（失败响应也重放）；同一个 key 但参数不同 → 冲突错误；同一个 intent 用**不同** key 再次 capture → 必须拒绝（防重复扣款）。

## 6. 故障注入矩阵

| 开关 | merchant | PSP | 用途 |
| --- | --- | --- | --- |
| `none` | ✓ | ✓ | 正常路径 |
| `slow` | 注入 N 秒延迟 | 同 | 超时与有界等待（T6 / R7 相关） |
| `error5xx` | 模拟崩溃 | 同 | 上游故障（模型只看到通用消息） |
| `decline` | — | 授权被拒 | 策略之外的业务失败 |
| `insufficient_funds` | — | 授权被拒（特定码） | 同上 |
| `drop_after_order` | **订单已创建但响应丢失** | — | 重复购买的经典触发点（T5） |
| `duplicate_capture` | — | 第二次 capture 也「成功」 | 必须被幂等键挡住（T5） |
| `inject_in_catalog` | 目录描述里带注入串 | — | T2 |
| `inject_in_messages` | 购物车 `messages` 里带注入串 | — | T2 |
| `huge_payload` | 返回超大字段 | 同 | 成本型风险 |

实现方式：`MOCK_MODE` / `PSP_MODE` 环境变量（**必须走 `StdioServerParameters(env=…)`**，W1 实测 stdio 子进程只继承环境白名单）。

两种失败刻意分开（W1 实测的 SDK 语义）：可预期的业务失败抛 `ToolError` → 客户端拿到 `isError=True` + 我们的消息；模拟崩溃抛普通异常 → SDK 包成 `UnexpectedToolError`，模型只看到 `Error executing tool <name>`，原文只进日志。

### 6.1 矩阵要不要覆盖对抗场景（2026-10-15，回应你的问题）

矩阵里的开关其实分三类，判定的归属完全不同：

| 类别 | 开关 | 它模拟什么 | 判定归谁 |
| --- | --- | --- | --- |
| 传输 / 时序类 | `slow`、`error5xx`、`huge_payload` | 环境与上游的行为缺陷 | 网关自己的鲁棒性与不变量（I2 / I4、有界等待、fail-closed） |
| 业务语义类 | `decline`、`insufficient_funds`、`duplicate_capture`、`drop_after_order` | 支付世界的正常失败与危险路径 | 预算与幂等（T5） |
| 内容类 | `inject_in_catalog`、`inject_in_messages` | 上游在「说什么」上作恶 | 净化与策略（T1 / T2），判定在 W4 的对抗套件 |

结论：**矩阵必须提供对抗载荷的投放能力，但不该表达对抗策略**。规则一句话——**mock 只负责「世界有多坏」，不负责「网关该不该拦」**。

理由是测试不能自证：如果 mock 里写死「这条应该被 deny」，那 W4 的对抗套件就变成在验证 mock 的假设，而不是验证网关。把「投放能力」与「判定标准」分开，同一套 mock 才能同时服务正常路径测试、故障注入测试和对抗测试。

另外**一半的对抗场景根本不需要 mock 支持**：当攻击者扮演的是**调用方**时（篡改游标、伪造 mandate、重放 `approval_id`、改工具名、超上限下单），作恶发生在客户端一侧，mock 侧开关覆盖不到。这类用例由 W4 的对抗套件直接构造请求，矩阵不需要为它们加开关——这是这个问题的另一半答案。

## 7. 明确不做（demo 取舍）

- 不做密码学签名与 VDC 验证：AP2 的 mandate 是 SDJWT 风格的签名凭证，我们只保留**结构与绑定语义**。理由：要证明的是「授权绑定到动作参数」，用哈希绑定即可复现，`docs/evaluation.md` 的判定口径不依赖签名。
- 不做真实支付网络、不做 KYC、不做多币种汇率换算。
- 不做 ACP 的 REST 端点（我们只做 MCP 形态；ACP 本身也说 REST 与 MCP 都支持）。
- 不做 x402 的链上结算（它的 402 语义已经用在 ADR-0006 / 0021 的「事前告知 + 重试」里，不需要真链）。

## 8. 出处

- AP2：`ap2-protocol.org/ap2/specification/index.md`（五角色与职责）、`/ap2/flows/index.md`（Human Present / Not Present 与 `unresolved_constraint`）、`/ap2/payment_mandate/index.md`（`vct` 版本、`transaction_id` 绑定、`payee`）、`/glossary/`。
- ACP：`agenticcommerce.dev/llms.txt`（五个 checkout session 端点、权威购物车状态、`payment_data`、Stripe Shared Payment Token、Stripe + OpenAI 联合开发）。
- x402：`docs.x402.org/introduction`、`/faq`（HTTP 402、Apache-2.0、约 1 秒结算）。
- 支付侧两阶段与幂等：Stripe manual capture 与 idempotency keys（ADR-0002 已引用）。
- 同类项目的 mock 做法：`TBXark/mcp-proxy` 仓库自带 `testdata/stdio-server` 作为测试上游——**上游 mock 用 stdio 子进程**是这类项目的通行做法，与 ADR-0004 一致。
