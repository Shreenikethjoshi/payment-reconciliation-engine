# Payment Reconciliation Engine: Design & Verification

**Project**: Task 9 — Payment Reconciliation Engine  
**Domain**: Meesho D2C E-Commerce vs. Razorpay Batch Settlements  
**Date**: November 2026  

---

### 1. How the Matching Rules Were Chosen

Modern payment settlement operates asynchronously between an internal order ledger and external gateway batch files. Our rules were designed around two goals: **deterministic speed for clean rows** and **strict physical boundaries before scoring fuzzy rows**.

- **Pass 1 (Exact Match, $O(1)$ Hash Map)**: Clears the vast majority (~97.5%) of transactions by matching normalized reference IDs (`ref.strip().upper()`) and exact integer paise gross amounts. Consuming matched IDs into an in-memory set enforces strict one-to-one pairing, preventing double-crediting.
- **Pass 2 (Candidate Blocking & Hard Constraints)**: Comparing 40,000 ledger rows against 40,000 settlements directly is an $O(N^2)$ operation that risks random collisions. We partition settlements into calendar buckets, restricting candidates to a $\pm 5$-day blocking window, a maximum settlement delay of 3 days, and gross amount variance $\le 20\%$.
- **Composite Scoring Function**: Surviving candidates are evaluated via:
  $$\text{Composite Score} = 0.50 \cdot S_{\text{ref}} + 0.20 \cdot S_{\text{date}} + 0.30 \cdot S_{\text{amount}}$$
  where $S_{\text{ref}}$ uses RapidFuzz token sorting to remain resilient against whitespace variations and prefix tags.
- **The Numeric Suffix Penalty**: High-entropy sequence IDs (e.g., `pay_QkX101` vs `pay_QkX102`) share $>90\%$ token similarity. To prevent cross-customer false matches during flash sales, we subtract 15 points whenever IDs share an 8+ character prefix and differ only in trailing digits. Scores $\ge 90$ auto-reconcile, $70$ to $89$ are flagged for human review, and ties trigger an `AMBIGUOUS_MATCH` guard.

---

### 2. Errors the Matcher Got Wrong, and Why

Evaluating the engine against the independent ground-truth answer key (1,600 seeded errors across 40,000 rows) highlighted predictable real-world edge cases:

- **Truncated References with Common Denominations**: When customers purchase items at standard price points (e.g., ₹499) on the same day and their payment IDs suffer severe character truncation, fuzzy similarity hovers near 65–70%. Choosing precision over speculative recall, the engine withheld automated matching to protect ledger integrity, yielding ~79% recall on reference mismatches.
- **Window-Boundary Exclusions on Extreme Lag**: Orders delayed by more than 5 calendar days fall outside the blocking window. The engine labels them as `MISSING_SETTLEMENT` rather than `LATE_SETTLEMENT`. Expanding the window would recover these, but would disproportionately inflate false matches across identical repeating order values.
- **Net vs. Gross Discrepancies**: Partial refunds deduct fees and payouts simultaneously. While our fee-netting logic caught 100% of seeded refund confusions, tight refund amounts overlapping with separate standalone purchases require manual operator sign-off.

---

### 3. How Reruns Are Made Safe (Idempotency)

Financial pipelines must support safe replays without corrupting balances or creating phantom duplicates:

- **Relational Uniqueness**: Table schemas enforce primary keys on `txn_id` and `settlement_id`. Ingestion uses `INSERT OR IGNORE`, making re-reads of the same source CSV a safe no-op. Results are guarded by a composite `UNIQUE(run_id, txn_id)` constraint.
- **Cryptographic Result Fingerprinting**: All reconciled pairs are sorted deterministically and hashed into a SHA-256 digest (`result_hash`). In automated tests, consecutive runs generated identical hashes (`770288ec58c...`) with 0 duplicate rows created.
- **Append-Only Audit Trail**: Manual decisions made in the Streamlit Workbench write immutable audit entries to `exception_resolutions`, leaving the raw ledger records untouched.

---

### 4. What I Would Do Differently with More Time

With additional time, I would transition from fixed heuristic weights to a **Globally-Optimized Bipartite Matching Framework** (e.g., Jonker-Volgenant or Hungarian algorithm on cost matrices) paired with learned rail-specific priors:

1. **Global vs. Greedy Assignment**: Current matching pairs candidates locally on a first-best basis. A global maximum-weight bipartite matching algorithm evaluates all candidate pairs simultaneously across the daily batch to maximize global reconciliation utility.
2. **Rail-Specific Error Models**: Error distributions differ across payment channels (UPI handles alphanumeric VPA typos; Cards face terminal STAN code drops; NetBanking introduces bank IFSC prefixes). Calibrating probabilistic feature weights per payment rail would further elevate reference mismatch recall without risking false positives.
