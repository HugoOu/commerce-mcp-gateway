# 风控门：硬门 + ML 残差风险（2026-10-19）

[中文](risk-decisioning.md) · [English](risk-decisioning.en.md)


> 这是 Step 4 之后的决策原则与行业调研记录。**当前 demo 不实现 ML**；它作为未来 TODO，避免把项目拖成风控平台。

## 1. 原则

**硬门**是不可补偿的：金额超 mandate、商户不在白名单、mandate 过期、工具未授权、审批未通过、交易上下文不一致（token/总额/顺序）——任一不满足就拒绝。

**ML 只处理硬门之外的残差风险**：在“已经允许”的调用里，估计欺诈、滥用、异常商户、报价漂移、注入可疑度等。ML 的输出只能触发“放行 / 加强审批 / 人工复核 / 拒绝”，不能推翻硬门。

一句话：**规则保证边界，模型优化边界内的判断。**

## 2. Step 4 的教训

Step 4 的手设权重是示意值，不是学习或校准出来的。敏感性分析（`eval/score_sensitivity.py`）显示：

- 2000 组随机 Dirichlet 权重中，**625 组（31.2%）**能在 11 个场景上做到 0/0；
- amount 权重从 0.10 提到 **0.15**，就能完美分离（阈值 0.86）。

所以“分数规则没有完美阈值”只对当前这组手设权重成立，不能写成“所有分数规则都失败”。Step 4 真正说明的是：**只要允许补偿，阈值就要在误放与误拒之间取舍**；硬门的价值是提供与分布无关的保证，而不是在这 11 个点上赢一次。

## 3. 行业怎么做（2026 前沿）

| 方案 | 风控结构 | 对本项目的含义 |
| --- | --- | --- |
| **Stripe Radar** | 500 万企业的网络级 ML 风险信号 + 规则 + adaptive 3DS；从“交易瞬间”扩展到用户/agent 全生命周期（KYA） | 规则与 ML 是叠加的；高风险才加强认证，不是所有交易都一刀切 |
| **Stripe Shared Payment Token（ACP）** | 支付凭证 token 化，agent 不接触卡号；用户生物识别授权，可设交易限额、时间窗口、商户白名单 | token 是能力边界；额度/时间/商户限制是硬门，ML 是额外风险层 |
| **Visa Intelligent Commerce / Agentic Ready** | tokenization、identity、risk、controls 一起提供；issuer readiness 测试 agent 发起的交易 | 风险控制是网络/发卡行/商户多层协作，不是 agent 自己判断 |
| **Mastercard Agent Pay Trust Framework** | identity、intent、behavioral、fraud insights；提供“这笔交易由 AI agent 发起的概率”和“是否异常/需要复核”的信号；agentic token | 行业已经在做“agent 身份/意图 + 行为 + 欺诈”的组合评分；评分用于授权决策与复核 |
| **ACP / UCP** | merchant/PSP 侧的 checkout session、幂等、delegated token；agent 不持有原始支付凭证 | 交易级状态与 token 绑定是硬门；风险评分在 merchant/PSP 侧 |
| **AP2** | 密码学 mandate 绑定金额/收款方/checkout；Security Considerations 把 agent 视为潜在攻击者；human-present 指标可供风险系统参考 | mandate 保证“授权正确”，不保证“决策正确”；风险评分不能替代 mandate 约束 |
| **Ravelin / Corgi / MRC 2026** | 传统欺诈模型是按人类行为训练的；agent 购物需要 agent 身份、意图、速度、设备、商户等新信号；从统一规则转向 targeted friction | ML 需要新特征与再训练；不能直接复用人类欺诈模型 |
| **IntentLock（开源）** | 规则/硬门 + graph risk score + intent diff；场景输出 capture / hold / void / reject | 与本项目最接近的“规则 + 评分 + 人工复核”形态；可作为未来 ML 层的参考 |

**共同模式**：没有一家把“一个 ML 分数”当成唯一决策。行业做法是 **tokens/identity + hard controls + risk score + step-up/review** 的组合。

## 4. 推荐架构（本项目未来版）

```text
authorize
  ├─ hard gate（不可补偿）
  │   ├─ mandate：金额 / 商户 / 品类 / 过期
  │   ├─ 工具白名单 / scope
  │   ├─ 审批状态与绑定
  │   └─ transaction context：checkout / intent / capture / token / 总额
  ├─ 任一硬门失败 → deny（记录 reason_code；ML 不参与）
  └─ 全部通过 → residual risk
        ├─ ML risk score（校准概率）
        ├─ score < low → allow
        ├─ low ≤ score < high → require_approval / step-up
        └─ score ≥ high → manual_review / deny
```

**证据**：每次决策记录 `hard_gate_result`、`risk_score`、`model_version`、`feature_digest`、`decision_layer`；硬门拒绝与 ML 拒绝分开记，便于审计和复算。

**为什么不让 ML 覆盖硬门**：模型输出是概率，不是授权；训练分布会漂移，攻击者会适配；金额超限/商户越权是 mandate 约束，不是“平均风险”问题。

## 5. 如果未来做 ML（TODO）

- **数据/标签**：拒付、欺诈、人工复核、攻击成功、商户异常；标签延迟、类别极不平衡、选择偏差要单独处理。
- **特征**：金额/额度比、商户历史、交易速度、报价与 checkout 一致性、工具风险、agent 身份/意图、时间、审批状态、注入可疑度、预算使用率。
- **模型**：先逻辑回归（可解释、易校准），再考虑 GBM；不要一上来上深度学习。
- **评估**：PR-AUC、固定 FPR 下的 recall、校准曲线、代价敏感阈值、out-of-time / adversarial validation；不要用 accuracy。
- **范围**：只放 `eval/risk_model.py` 与合成数据；不进 `src/`，不声称生产级。先证明“硬门 + ML”比“ML only”在分布外更稳。

## 6. 面试表述

**问：为什么不用一个 ML 分数决定放行？**
答：分数可以优化平均判别，但不能保证单条硬约束；金额超限、商户越权、mandate 过期不是可补偿的风险，而是授权边界。行业也是 tokens + hard controls + risk score + step-up/review 的组合。

**问：ML 在这个项目里放哪？**
答：只处理通过硬门后的残差风险；输出校准概率，触发审批/复核；证据里记录模型版本与特征摘要，硬门拒绝与模型拒绝分开审计。

**问：Step 4 的分数规则实验说明什么？**
答：说明补偿式决策的阈值取舍；但权重是手设的，敏感性分析显示 31.2% 的随机权重也能 0/0，所以不能把它写成“所有分数规则都失败”。真正的论据是硬门在分布漂移下仍有保证。

## 7. 来源（2026-10-19 检索）

> 部分是厂商博客/行业媒体，非同行评审；引用时标注来源类型。

- Stripe Radar / Shared Payment Token：Stripe Sessions 2026 报道（ChainCatcher、Foresight News，二手报道）
- Visa Agentic Ready：https://www.visa.com/en-kw/newsroom/agentic-ready-ai-commerce-kuwait
- Mastercard Agent Pay Trust Framework：https://www.pymnts.com/news/payment-methods/2026/mastercard-deploys-new-risk-insights-to-support-agentic-payments/
- ACP / UCP delegated token：https://www.factualminds.com/glossary/agent-checkout/
- AP2 威胁分析：arXiv:2608.23858（Beyond the Mandate）；arXiv:2601.22569（Whispers of Wealth）
- AP2 mandate amplification：https://zenodo.org/records/22262232
- Ravelin 2026 支付趋势：https://www.ravelin.com/blog/2026-in-payments-analysis-and-advice
- Corgi Labs agentic commerce fraud：https://www.corgilabs.ai/insights/fraud-detection-agentic-commerce
- IntentLock（开源规则 + 评分示例）：https://github.com/BrienAustinClayton/intentlock
- MRC Vegas 2026 行业观察：https://www.signifyd.com/blog/mrc-vegas/
