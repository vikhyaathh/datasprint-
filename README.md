# The Procurement Desk: Playbook Agent

**Data Sprint 2.0 (Anveshan, NIE Mysuru), Agentic AI track** · Team **Claude's plan**
(Vishwa H, Vikhyaath Mallikarjun, Varun T)

A deterministic agent that reads each supplier negotiation thread, applies Acme's procurement
playbook, and outputs `ACTION:REASON:COUNTER_PRICE`.

| | |
|---|---|
| Public leaderboard | **1.00000** (exact-match accuracy) |
| Worked examples (`sample_threads.csv`) | **40 / 40** |
| AI model / API used | **None.** Pure Python standard library, with no LLM and no external API. Every label is produced by code. |
| Runtime | about 1 second for all 2,000 threads, on CPU |

## How to run

Requires **Python 3.10+**. No third-party packages are needed.

1. Download the competition data from Kaggle (The Procurement Desk → Data → Download All) and
   unzip it into `data/` so it looks like this:

   ```
   data/threads.csv
   data/sample_threads.csv
   data/sample_answers.csv
   data/sample_submission.csv
   data/playbook/            <- unzip playbook.zip here (data/playbook/playbook/... also works)
   ```

2. Run:

   ```
   python agent.py validate              # checks the 40 worked examples -> "40/40 correct"
   python agent.py predict               # writes submission.csv (2,000 rows)
   python agent.py explain T00028        # prints the step-by-step reasoning for one thread
   ```

`submission.csv` is fully deterministic. Its SHA-256 is
`e7cb2ed18e971f21fcc2766a2b9fa02b059daf303a5e1c38005cf1355aaa7d98`, identical to the file that
scored 1.00000.

## Approach

The playbook is a precise rulebook, so the agent implements it as code instead of asking an
LLM to interpret it. That makes every decision exact, reproducible and explainable, which matters
because the metric needs action, reason *and* counter price to be exactly right. It also makes the
agent immune to prompt injection by design: supplier text is only ever *parsed*, never followed.

### 1. Load the rules in force for each thread
- Category values (target, walk-away, rounds *R*, concession exponent β, GST rate, approval limit).
- **Amendments** whose effective date is on or before the thread date, applied in date order.
- **Supplier contracts**: a price cap replaces the walk-away price for that category; a contract
  exchange rate replaces the table rate for that currency. Supplier status (Approved / PROBATION /
  BLACKLISTED).

### 2. Parse and normalise the supplier's latest offer to *p* (INR, excl. GST, per normalized unit)
Handles: ₹ / Rs. / INR / USD / EUR / GBP, Indian digit grouping, prices in words ("one lakh
seventeen thousand eight hundred rupees"), **corrections** (replace the corrected price), "a further
D% off our previous quote" (applied to the supplier's own earlier quote), list-price discounts,
**price ranges** (upper end, precedents P-01/P-04), **tiered prices** by quantity, **volume
discounts** (only if the quantity meets the threshold), **free units** (P-02/P-05), per dozen / pack
/ ream / per-N units, totals ÷ N, **GST-inclusive** ÷ (1 + rate), and monthly ↔ yearly billing.

### 3. Decision procedure (first rule that fires wins, as in `policies/decision-procedure.md`)
1. **Missing info**: no price, an unlisted currency (AED, JPY, SGD…), or a subscription price with
   no billing period → `ESCALATE:MISSING_INFO`
2. **Blacklisted supplier** → `REJECT:BLACKLISTED_SUPPLIER`
3. **Prompt injection** anywhere in the thread (English and Hindi), with a whitelist for ordinary
   business wording ("AI-powered", "ERP system", "assistant manager", "system integration team")
   → `ESCALATE:PROMPT_INJECTION`
4. **Banned terms** in the latest message: auto-renewal in any wording, >30% upfront/advance
   (digits or words such as "half"), unlimited liability, exclusivity. Negated forms ("no automatic
   renewal", "not asking for exclusivity") are allowed → `ESCALATE:BANNED_TERM`
5. **Price vs concession schedule** `L(r) = target + (walkaway − target) · ((r−1)/(R−1))^β`
   (PROBATION suppliers: L = target in every round). If *p* > L: `COUNTER` at L rounded half-up, or
   `REJECT:ABOVE_WALKAWAY` on the final round.
6. **Approval limit**: *p* × quantity above the limit → `ESCALATE:OVER_APPROVAL_LIMIT`, otherwise
   `ACCEPT:ACCEPTABLE_PRICE`.

## Known ambiguity

`approval-limits.md` says "one billing period for subscriptions" without saying which period. We
use the category's normalized period (the final submission). `python agent.py predict --alt-billing`
uses the period the supplier quoted instead; only 10 of the 2,000 threads differ.

## Files

- `agent.py`: the complete agent (playbook loader, offer parser, decision procedure, CLI)
- `submission.csv`: final submission (regenerate with `python agent.py predict`)
