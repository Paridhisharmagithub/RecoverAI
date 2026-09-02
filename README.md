# RecoverAI — Revenue Recovery Control Plane

> **Detect. Diagnose. Decide. Gate. Recover. Prove.**

RecoverAI is a hackathon-focused revenue recovery operator for merchants. It treats revenue leakage as a portfolio of opportunities rather than a retry queue.

## Why this is differentiated

The Razorpay AI Buildathon Track 03 brief asks for an agent that detects revenue at risk, determines the right intervention, executes a bounded recovery workflow, and proves measured money recovered with compliant escalation, stopping rules and an audit trail.

RecoverAI makes each of those requirements visible in the product:

- **Four leakage rails:** failed payments, checkout abandonment, subscription dunning and overdue receivables.
- **Expected-net-value ranking:** amount × recovery probability − intervention cost, with customer value affecting priority but never overriding controls.
- **Reason-aware playbooks:** timeouts, insufficient funds, stale instruments, mandate failures, checkout intent and receivables use different recovery strategies.
- **Counterfactual decisioning:** each case shows alternative interventions and their modeled recoverability so the selected action is explainable.
- **Independent guardrail gate:** prediction never authorizes a financial action. Merchant limits can block automation by amount, retry count, reminder count, fraud flag, dispute state, cost or a global switch.
- **Closed-loop execution:** every action writes a decision event, tool result, outcome and audit record.
- **Batch impact lab:** compare a simple baseline with RecoverAI across the full synthetic opportunity corpus and calculate gross and net incremental expected value.
- **Failure is explicit:** the live demo includes a deterministic provider-failure case that is logged and surfaced as a graceful escalation path instead of entering a retry loop.
- **Razorpay Test Mode adapter:** the project can create real Razorpay Payment Links in Test Mode when credentials are configured; local mode remains fully runnable without credentials.

## The product flow

```text
Event stream
   ↓
Opportunity detection
   ↓
Recovery model
   ↓
Agent planner
   ↓
Counterfactuals
   ↓
Expected-net-value ranking
   ↓
Merchant policy gate
   ├── BLOCK → human review + audit
   └── PASS  → bounded tool
                    ↓
               provider result
                    ↓
             outcome + revenue
                    ↓
               audit trail
                    ↓
               impact lab
```

## Product screens

1. **Overview** — merchant exposure, expected net recovery, recovered value and the six-step system contract.
2. **Revenue queue** — a unified queue across all four leakage rails, ranked by expected net value.
3. **Live demo** — a judge-ready case walkthrough with decision trace, counterfactuals, guardrails, execution and outcome.
4. **Impact lab** — baseline vs RecoverAI, incremental expected revenue, net value and contribution by leakage rail.
5. **Revenue insights** — root-cause leakage map, customer economics and concrete merchant recommendations.
6. **Playbooks** — reason-specific recovery strategies.
7. **Decision trace** — audit events across the recovery lifecycle.
8. **Guardrails** — merchant-configurable boundaries.

## Demo in under two minutes

1. Open **Live demo**.
2. Choose **Successful recovery**.
3. Show the trace: Observe → Predict → Plan → Counterfactual → Gate → Execute.
4. Execute the action and show the recovered ₹ amount.
5. Choose **Graceful failure / escalation** and execute the second curated case.
6. Show that the provider error is recorded and the system does not keep retrying.
7. Open **Impact lab** and run the 50-case controlled batch.
8. Point to **incremental expected value**, **net incremental value**, and **by-rail contribution**.

## Synthetic data and evaluation

On first startup the app creates a reproducible synthetic corpus with:

- 800 customers
- 5,000 payment events
- 2,400+ failed payments
- 850 checkout-abandonment opportunities
- 650 subscription-dunning opportunities
- 220 receivable opportunities

The payment recovery model is trained on an 80/20 time-ordered split and the ML screen exposes precision, recall, F1, ROC-AUC, PR-AUC, false positives and false-positive cost.

The Impact Lab is a separate strategy simulation over the full opportunity corpus; it is intentionally labeled as expected-value simulation rather than presenting synthetic outcomes as live merchant results.

## Local setup — Windows

```powershell
cd "D:\Paridhi\paridhi_desktop\PARIDHI SHARMA\recoverai"
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
python -m uvicorn app.main:app --reload
```

Open:

**http://127.0.0.1:8000**

Or use:

```powershell
.\run.ps1
```

No Node/npm is required.

## Razorpay Test Mode

Set:

```env
USE_RAZORPAY=true
RAZORPAY_KEY_ID=rzp_test_...
RAZORPAY_KEY_SECRET=...
RAZORPAY_WEBHOOK_SECRET=...
```

The real adapter uses Razorpay Payment Links for fresh-payment-surface recovery. Test-mode Payment Links are recommended by Razorpay for testing. The webhook endpoint validates `X-Razorpay-Signature` when a webhook secret is configured.

Local development defaults to the deterministic simulator, so the full product works without network credentials.

## API

- `GET /api/health`
- `GET /api/dashboard`
- `GET /api/opportunities`
- `GET /api/opportunities/{id}`
- `POST /api/opportunities/{id}/execute`
- `POST /api/opportunities/batch?limit=50`
- `GET /api/impact`
- `GET /api/insights`
- `GET /api/playbooks`
- `GET /api/outcomes`
- `GET /api/audit`
- `GET /api/settings`
- `PUT /api/settings`
- `POST /api/simulator/reset`
- `POST /api/webhooks/razorpay`

Legacy payment endpoints are retained for compatibility with the earlier version.

## Project structure

```text
recoverai/
├── app/
│   ├── main.py
│   ├── opportunities.py
│   ├── agent.py
│   ├── ml.py
│   ├── data_generator.py
│   ├── db.py
│   ├── policy.py
│   ├── integrations.py
│   ├── config.py
│   └── static/
│       ├── index.html
│       ├── app.js
│       └── style.css
├── tests/
├── data/
├── models/
├── .env.example
├── requirements.txt
├── run.ps1
├── run.bat
└── run.sh
```

## Design principle

> **The model proposes. The agent plans. The policy engine authorizes. The tool executes. The audit trail proves.**

That separation is the core trust boundary of RecoverAI.
