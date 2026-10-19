# Risk decisioning: hard gates plus ML residual risk

[中文](risk-decisioning.md) · English

> This note records the decision principle and the industry survey after W4 Step 4. **The current demo does not implement ML**; it is future work, so the project does not turn into a risk-scoring platform.

## 1. Principle

A **hard gate** is non-compensatory. If the amount exceeds the mandate, the merchant is not on the allowlist, the mandate has expired, the tool is not authorized, approval has not passed, or the transaction context is inconsistent (token / total / ordering), the call is denied. No other signal can offset the failure.

**ML only handles residual risk outside the hard gates.** For calls that are already allowed, it estimates fraud, abuse, anomalous merchants, quote drift or injection suspicion. Its output may trigger allow / step-up approval / manual review / deny; it cannot overrule a hard gate.

In one sentence: **rules guarantee the boundary; models improve judgement inside the boundary.**

## 2. What Step 4 taught us

The hand-set weights in Step 4 are illustrative, not learned or calibrated. The sensitivity analysis in `eval/score_sensitivity.py` shows:

- among 2,000 random Dirichlet weightings, **625 (31.2%)** achieve 0 false accepts and 0 false denies on the 11 labelled scenarios;
- raising the amount weight from 0.10 to **0.15** is enough to separate them perfectly at threshold 0.86.

So "no score threshold is perfect" holds only for that one hand-set weighting. It must not be written as "every scoring rule fails." What Step 4 really shows is: **once compensation is allowed, a threshold must trade false accepts against false denies.** The value of a hard gate is a distribution-free guarantee, not winning on these 11 points.

## 3. What the industry does (2026)

| Approach | Risk structure | Implication for this project |
| --- | --- | --- |
| **Stripe Radar** | network-level ML risk signals from millions of businesses + rules + adaptive 3DS; extends from the transaction moment to the user/agent lifecycle (KYA) | rules and ML are layered; high risk triggers stronger authentication rather than a blanket block |
| **Stripe Shared Payment Token (ACP)** | payment credentials are tokenized so the agent never sees the card; biometric user authorization; transaction limits, time windows and merchant allowlists | the token is the capability boundary; amount/time/merchant limits are hard gates; ML is an additional risk layer |
| **Visa Intelligent Commerce / Agentic Ready** | tokenization, identity, risk and controls together; issuer readiness tests for agent-initiated transactions | risk control is multi-party: network, issuer and merchant, not the agent judging itself |
| **Mastercard Agent Pay Trust Framework** | identity, intent, behavioral and fraud insights; signals for "probability this transaction is agent-initiated" and "is it anomalous / does it need review"; agentic tokens | the industry already combines agent identity/intent + behavior + fraud scoring; scores feed authorization and review |
| **ACP / UCP** | merchant/PSP-side checkout sessions, idempotency, delegated tokens; the agent does not hold raw payment credentials | transaction state and token binding are hard gates; risk scoring lives on the merchant/PSP side |
| **AP2** | cryptographic mandates bind amount, payee and checkout; the Security Considerations treat the agent as a potential attacker; human-present signals can feed a risk system | a mandate guarantees "authorization is correct," not "the decision is correct"; risk scoring cannot replace mandate constraints |
| **Ravelin / Corgi / MRC 2026** | traditional fraud models are trained on human behavior; agent shopping needs new signals such as agent identity, intent, velocity, device and merchant; the shift is from uniform rules to targeted friction | ML needs new features and retraining; human fraud models cannot be reused as-is |
| **IntentLock (open source)** | rules/hard gates + graph risk score + intent diff; scenarios emit capture / hold / void / reject | closest shape to "rules + score + manual review"; a useful reference for a future ML layer |

**Common pattern:** nobody uses a single ML score as the only decision. The industry pattern is **tokens/identity + hard controls + risk score + step-up/review**.

## 4. Recommended architecture (future version)

```text
authorize
  ├─ hard gate (non-compensatory)
  │   ├─ mandate: amount / merchant / category / expiry
  │   ├─ tool allowlist / scope
  │   ├─ approval state and binding
  │   └─ transaction context: checkout / intent / capture / token / total
  ├─ any hard gate fails → deny (record reason_code; ML does not participate)
  └─ all pass → residual risk
        ├─ ML risk score (calibrated probability)
        ├─ score < low → allow
        ├─ low ≤ score < high → require_approval / step-up
        └─ score ≥ high → manual_review / deny
```

**Evidence:** each decision records `hard_gate_result`, `risk_score`, `model_version`, `feature_digest` and `decision_layer`. Hard-gate denials and ML denials are recorded separately so audit and replay can tell them apart.

**Why ML must not overrule a hard gate:** model output is a probability, not an authorization. Training distributions drift and attackers adapt. Amount over-limit or merchant escalation are mandate constraints, not "average risk" questions.

## 5. If ML is added later (TODO)

- **Data and labels:** chargebacks, fraud, manual review, successful attacks, merchant anomalies. Label delay, extreme class imbalance and selection bias need separate treatment.
- **Features:** amount/limit ratio, merchant history, transaction velocity, quote-versus-checkout consistency, tool risk, agent identity/intent, time, approval state, injection suspicion, budget utilization.
- **Model:** start with logistic regression (interpretable, easy to calibrate), then consider GBM. Do not start with deep learning.
- **Evaluation:** PR-AUC, recall at a fixed FPR, calibration curves, cost-sensitive thresholds, out-of-time and adversarial validation. Do not use accuracy.
- **Scope:** keep it in `eval/risk_model.py` with synthetic data; do not put it in `src/` and do not claim production readiness. First show that "hard gate + ML" is more robust out of distribution than "ML only."

## 6. Interview framing

**Q: Why not let one ML score decide?**
A: A score can optimize average discrimination but cannot guarantee a single hard constraint. An over-limit amount, an unauthorized merchant or an expired mandate are authorization boundaries, not compensable risks. The industry also combines tokens + hard controls + risk score + step-up/review.

**Q: Where does ML fit here?**
A: It only handles residual risk after the hard gates. It outputs a calibrated probability that can trigger approval or review. Evidence records the model version and feature digest; hard-gate denials and model denials are audited separately.

**Q: What does the Step 4 scoring experiment show?**
A: It shows the threshold trade-off in compensatory decisioning. But the weights are hand-set, and the sensitivity analysis shows 31.2% of random weightings also achieve 0/0, so it must not be written as "all scoring rules fail." The real argument is that hard gates still hold under distribution shift.

## 7. Sources (retrieved 2026-10-19)

> Some are vendor blogs or trade media rather than peer-reviewed work; label the source type when citing.

- Stripe Radar / Shared Payment Token: Stripe Sessions 2026 coverage (ChainCatcher, Foresight News; secondary)
- Visa Agentic Ready: https://www.visa.com/en-kw/newsroom/agentic-ready-ai-commerce-kuwait
- Mastercard Agent Pay Trust Framework: https://www.pymnts.com/news/payment-methods/2026/mastercard-deploys-new-risk-insights-to-support-agentic-payments/
- ACP / UCP delegated token: https://www.factualminds.com/glossary/agent-checkout/
- AP2 threat analysis: arXiv:2608.23858 (Beyond the Mandate); arXiv:2601.22569 (Whispers of Wealth)
- AP2 mandate amplification: https://zenodo.org/records/22262232
- Ravelin 2026 payments trends: https://www.ravelin.com/blog/2026-in-payments-analysis-and-advice
- Corgi Labs agentic commerce fraud: https://www.corgilabs.ai/insights/fraud-detection-agentic-commerce
- IntentLock (open-source rules + scoring example): https://github.com/BrienAustinClayton/intentlock
- MRC Vegas 2026 industry observations: https://www.signifyd.com/blog/mrc-vegas/
