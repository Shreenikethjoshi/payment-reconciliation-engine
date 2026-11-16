# Payment Reconciliation Engine

An automated, two-pass payment reconciliation engine built for Indian D2C e-commerce operations (Meesho/Nykaa scale) reconciling internal order transactions against payment gateway settlement batches (Razorpay UPI, RuPay/Visa Cards, NetBanking).

---

## Overview

In modern e-commerce ecosystems, orders placed on digital storefronts are recorded in an internal Expected Collections Ledger. Settlement of funds occurs asynchronously through payment gateways (PSPs) and acquiring banks via batch settlement files. Discrepancies routinely arise from:
- MDR processing fees and 18% GST deductions
- Differing settlement turnaround times (T+1 for UPI, T+2 for Cards and NetBanking)
- Reference formatting variations, truncations, and typos
- Network dropouts resulting in missing or duplicate settlements
- Partial refunds mimicking smaller independent transactions

This engine provides a robust two-pass matching engine, automated exception classification, queue-based ingestion staging, idempotency guarantees, and an interactive Streamlit investigation workbench.

---

## System Architecture

```text
+---------------------------------------------------------------------------------+
|                               1. DATA GENERATION                                |
|   src/generator.py (seed=42)                                                    |
|   |-- data/expected_ledger.csv      (40,000 internal ledger transactions)       |
|   |-- data/settlement_report.csv    (39,900 settlements + realistic noise)      |
|   `-- data/answer_key.csv           (1,600 ground-truth seeded errors)          |
+----------------------------------------+----------------------------------------+
                                         |
                                         v
+---------------------------------------------------------------------------------+
|                              2. INGESTION PIPELINE                              |
|   src/ingestion.py                                                              |
|   |-- Producer / consumer queue-based staging                                   |
|   |-- Normalization: integer paise, date parsing, reference stripping           |
|   |-- Rejected Rows: captured in logs/rejected_rows.jsonl & rejected_rows table |
|   `-- Validated Rows: batch inserted into SQLite (reconciliation.db)            |
+----------------------------------------+----------------------------------------+
                                         |
                                         v
+---------------------------------------------------------------------------------+
|                               3. MATCHING ENGINE                                |
|   src/matcher.py                                                                |
|   |-- Pass 1: Exact Match (Normalized Reference + Gross Amount in Paise)        |
|   |-- Pass 2: Fuzzy Match (Candidate Blocking +-5 days, Score: 0.5R+0.2D+0.3A)  |
|   `-- Exception Classification: 6 typed exceptions (Missing, Duplicate, etc.)   |
+----------------------------------------+----------------------------------------+
                                         |
                                         v
+---------------------------------------------------------------------------------+
|                             4. OUTPUTS & WORKBENCH                              |
|   |-- src/reporting.py   -> Daily HTML and CSV reports (reports/)               |
|   |-- src/evaluator.py   -> Precision, recall, and F1 vs ground truth           |
|   `-- workbench/app.py   -> Streamlit interactive review workbench              |
+---------------------------------------------------------------------------------+
```

---

## Quick Start

### 1. Environment Setup

```bash
# Clone repository
git clone https://github.com/your-username/reconcilation-engine.git
cd reconcilation-engine

# Create and activate virtual environment
python -m venv venv

# Windows
venv\Scripts\activate
# Linux/macOS
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 2. Run the Full Reconciliation Pipeline

Executes synthetic data generation, queue-based ingestion, two-pass matching, evaluation against ground truth, report generation, and an automated second run verifying idempotency:

```bash
python run_pipeline.py
```

Optional flags:
- `--date YYYY-MM-DD`: Specify the run date (defaults to today).
- `--reset`: Reset database tables and regenerate datasets.

### 3. Launch the Workbench

```bash
streamlit run workbench/app.py
```

Access the dashboard at `http://localhost:8501`.

### 4. Run the Test Suite

```bash
pytest tests/ -v
```

---

## Matching Strategy

The engine utilizes a two-pass matching algorithm:

### Pass 1: Exact Match
- **Criteria**: Strict equality of normalized reference (`ref.strip().upper()`) and exact integer paise gross amount.
- **Deduplication**: One-to-one consumption prevents settlement double-counting.
- **Classification**: Detects `DUPLICATE_SETTLEMENT`, `LATE_SETTLEMENT`, and `PARTIAL_REFUND_CONFUSION` during exact reference matching.

### Pass 2: Fuzzy Match with Composite Scoring
- **Candidate Blocking**: Settlements are partitioned into date buckets, limiting comparisons to a ±5 calendar day window.
- **Hard Constraints**: Settlement date within ±3 days and gross amount variance $\le 20\%$.
- **Composite Score Formula**:
  $$\text{Composite Score} = 0.50 \cdot S_{\text{ref}} + 0.20 \cdot S_{\text{date}} + 0.30 \cdot S_{\text{amount}}$$
  Where $S_{\text{ref}}$ is computed using RapidFuzz `token_sort_ratio`.
- **Numeric Suffix Penalty**: Deducts 15 points if references share an identical prefix of $\ge 8$ characters but differ only in trailing digits (e.g. `pay_QkX1234` vs `pay_QkX1235`), avoiding sequential collision errors.
- **Confidence Tiers**:
  - Score $\ge 90$: Auto-reconcile (`MATCHED_FUZZY`)
  - Score $70$ to $89$: Flagged for manual review (`NEEDS_REVIEW`)
  - Score $< 70$: Route to Exception Classifier
  - Multiple candidates $\ge 70$: Flagged as `AMBIGUOUS_MATCH` to prevent false positive matching.

---

## Seeded Error Types & Detection Performance

Evaluated against the independent `data/answer_key.csv` (1,600 seeded errors across 40,000 transactions):

| Error Type | Ground Truth Count | Precision | Recall | F1 Score | Description |
|:---|:---:|:---:|:---:|:---:|:---|
| `DUPLICATE_SETTLEMENT` | 200 | 1.000 | 1.000 | 1.000 | Same transaction settled twice by the gateway. |
| `LATE_SETTLEMENT` | 350 | 1.000 | 1.000 | 1.000 | Settlement received past standard turnaround window (T+1/T+2). |
| `PARTIAL_REFUND_CONFUSION` | 100 | 1.000 | 1.000 | 1.000 | Net settlement altered by refund, mimicking separate transactions. |
| `AMOUNT_MISMATCH` | 400 | 1.000 | 0.760 | 0.864 | Gross settlement deviates by ±5% to ±15% from expected amount. |
| `MISSING_SETTLEMENT` | 300 | 0.959 | 0.697 | 0.807 | Expected order with no corresponding gateway settlement. |
| `REFERENCE_MISMATCH` | 250 | 0.627 | 0.760 | 0.687 | Typographical errors or ID truncation (`pay_...`). |

---

## Safe Reruns & Idempotency

The engine guarantees deterministic results across repeated runs:
- **Primary Key Constraints**: `txn_id` and `settlement_id` enforce row uniqueness via `INSERT OR IGNORE`.
- **Unique Run Isolation**: The `reconciliation_results` table maintains a `(run_id, txn_id)` unique constraint.
- **SHA-256 Fingerprinting**: Each run computes a deterministic hash of sorted results. Rerunning against identical source files produces matching hashes (`result_hash`) and zero duplicate rows.
- **Audit Logging**: Manual resolutions in the workbench are saved as append-only records in `exception_resolutions`, keeping the raw ledger and settlements immutable.

---

## Project Structure

```
reconcilation-engine/
├── data/                       # CSV inputs and generated datasets
│   ├── expected_ledger.csv
│   ├── settlement_report.csv
│   └── answer_key.csv
├── src/                        # Core engine modules
│   ├── database.py             # SQLAlchemy models and SQLite connection
│   ├── generator.py            # Synthetic dataset generator (seed=42)
│   ├── ingestion.py            # Queue-based ingestion, validation, normalization
│   ├── matcher.py              # Two-pass matching engine & composite scoring
│   ├── evaluator.py            # Precision, recall, and F1 evaluation
│   └── reporting.py            # Daily HTML and CSV report generation
├── workbench/                  # Streamlit investigation interface
│   └── app.py                  # Exception workbench dashboard
├── tests/                      # Automated test suite
│   ├── conftest.py             # Fixtures (in-memory SQLite)
│   ├── test_generator.py       # Dataset tests
│   ├── test_matching.py        # Matching logic unit tests
│   └── test_idempotency.py     # Idempotency and safe rerun tests
├── logs/                       # JSONL rejected rows log
├── reports/                    # Daily reconciliation reports (HTML/CSV)
├── run_pipeline.py             # Pipeline orchestrator and idempotency verifier
├── inspect_db.py               # Database inspection utility
├── writeup.md                  # 1-page technical write-up
└── requirements.txt            # Project dependencies
```
