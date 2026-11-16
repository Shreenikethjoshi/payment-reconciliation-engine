"""
reporting.py
============
Generates the daily reconciliation report for a given pipeline run.

Outputs:
  1. reports/daily_report_{YYYYMMDD}_{run_id}.csv   — machine-readable
  2. reports/daily_report_{YYYYMMDD}_{run_id}.html  — pretty HTML (Jinja2)

Usage (CLI):
    python reporting.py <run_id> [db_path] [output_dir]
"""

from __future__ import annotations

import csv
import os
from datetime import datetime
from typing import Any, Dict

from jinja2 import Template
from sqlalchemy import text

AT_RISK_TYPES = {
    "MISSING_SETTLEMENT",
    "DUPLICATE_SETTLEMENT",
    "AMOUNT_MISMATCH",
    "UNMATCHED_SETTLEMENT",
    "REFERENCE_MISMATCH",
    "PARTIAL_REFUND_CONFUSION",
}

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>Daily Reconciliation Report — {{ run_id }}</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: 'Segoe UI', Arial, sans-serif;
      background: #f4f6fb;
      color: #22283a;
    }
    header {
      background: linear-gradient(90deg, #1a237e 0%, #283593 100%);
      color: #fff;
      padding: 28px 40px 18px;
    }
    header h1 { font-size: 1.7rem; font-weight: 700; }
    header p  { font-size: 0.9rem; opacity: 0.8; margin-top: 4px; }

    .cards {
      display: flex;
      flex-wrap: wrap;
      gap: 18px;
      padding: 28px 40px 10px;
    }
    .card {
      background: #fff;
      border-radius: 10px;
      box-shadow: 0 2px 10px rgba(0,0,0,0.07);
      padding: 22px 28px;
      min-width: 190px;
      flex: 1;
    }
    .card .label {
      font-size: 0.75rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: #6b7280;
      margin-bottom: 8px;
    }
    .card .value {
      font-size: 2rem;
      font-weight: 700;
      color: #1a237e;
    }
    .card .value.risk { color: #b71c1c; }
    .card .sub {
      font-size: 0.78rem;
      color: #9ca3af;
      margin-top: 4px;
    }

    .section {
      margin: 18px 40px;
      background: #fff;
      border-radius: 10px;
      box-shadow: 0 2px 10px rgba(0,0,0,0.06);
      padding: 24px 28px;
    }
    .section h2 {
      font-size: 1.1rem;
      font-weight: 600;
      color: #1a237e;
      margin-bottom: 16px;
      border-bottom: 2px solid #e8eaf6;
      padding-bottom: 8px;
    }

    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 0.88rem;
    }
    thead tr { background: #e8eaf6; }
    th {
      text-align: left;
      padding: 10px 14px;
      font-weight: 600;
      color: #3949ab;
      white-space: nowrap;
    }
    td {
      padding: 10px 14px;
      border-bottom: 1px solid #f0f0f0;
    }
    tr:last-child td { border-bottom: none; }
    tr:hover td { background: #f5f7ff; }

    .badge {
      display: inline-block;
      padding: 2px 10px;
      border-radius: 999px;
      font-size: 0.75rem;
      font-weight: 600;
    }
    .badge-red    { background: #fde8e8; color: #b71c1c; }
    .badge-orange { background: #fff3e0; color: #e65100; }
    .badge-blue   { background: #e3f2fd; color: #0d47a1; }
    .badge-green  { background: #e8f5e9; color: #1b5e20; }
    .badge-grey   { background: #f3f4f6; color: #374151; }

    .amounts { margin-top: 4px; display: flex; gap: 10px; flex-wrap: wrap; }
    .amt-pill {
      background: #f3f4f6;
      border-radius: 6px;
      padding: 4px 12px;
      font-size: 0.82rem;
    }

    footer {
      background: #1a237e;
      color: rgba(255,255,255,0.75);
      padding: 16px 40px;
      font-size: 0.8rem;
      margin-top: 28px;
    }
    footer strong { color: #fff; }
    .run-meta { display: flex; flex-wrap: wrap; gap: 24px; }
    .run-meta div { line-height: 1.8; }
  </style>
</head>
<body>

<header>
  <h1>💳 Daily Reconciliation Report</h1>
  <p>Generated on {{ generated_at }} &nbsp;|&nbsp; Run: <strong>{{ run_id }}</strong></p>
</header>

<!-- Summary Cards -->
<div class="cards">
  <div class="card">
    <div class="label">Expected Transactions</div>
    <div class="value">{{ "{:,}".format(total_expected_txns) }}</div>
    <div class="sub">Internal ledger rows</div>
  </div>
  <div class="card">
    <div class="label">Settlement Records</div>
    <div class="value">{{ "{:,}".format(total_settlement_records) }}</div>
    <div class="sub">From provider file</div>
  </div>
  <div class="card">
    <div class="label">Matched</div>
    <div class="value">{{ "{:,}".format(total_matched) }}</div>
    <div class="sub">Exact: {{ "{:,}".format(matched_exact_count) }} &nbsp;|&nbsp; Fuzzy: {{ "{:,}".format(matched_fuzzy_count) }}</div>
  </div>
  <div class="card">
    <div class="label">Needs Review</div>
    <div class="value">{{ "{:,}".format(needs_review_count) }}</div>
    <div class="sub">Ambiguous or unresolved</div>
  </div>
  <div class="card">
    <div class="label">Unmatched</div>
    <div class="value">{{ "{:,}".format(total_unmatched_count) }}</div>
    <div class="sub">No settlement found</div>
  </div>
  <div class="card">
    <div class="label">💰 Money at Risk</div>
    <div class="value risk">₹{{ "{:,.2f}".format(money_at_risk_total_inr) }}</div>
    <div class="sub">Across all exception types</div>
  </div>
</div>

<!-- Amount Summary -->
<div class="section">
  <h2>Amount Summary</h2>
  <div class="amounts">
    <div class="amt-pill">📋 Expected &nbsp;<strong>₹{{ "{:,.2f}".format(total_expected_amount_inr) }}</strong></div>
    <div class="amt-pill">📥 Gross Settled &nbsp;<strong>₹{{ "{:,.2f}".format(total_gross_settled_inr) }}</strong></div>
    <div class="amt-pill">📤 Net Settled &nbsp;<strong>₹{{ "{:,.2f}".format(total_net_settled_inr) }}</strong></div>
    <div class="amt-pill">❌ Rejected Rows &nbsp;<strong>{{ "{:,}".format(total_rejected_rows) }}</strong></div>
  </div>
</div>

<!-- Exception Breakdown -->
<div class="section">
  <h2>Exception Breakdown</h2>
  <table>
    <thead>
      <tr>
        <th>Exception Type</th>
        <th>Count</th>
        <th>Amount at Risk (INR)</th>
        <th>Category</th>
      </tr>
    </thead>
    <tbody>
    {% for etype, info in exception_breakdown.items() %}
      <tr>
        <td>
          {% if etype == 'MISSING_SETTLEMENT' %}
            <span class="badge badge-red">{{ etype }}</span>
          {% elif etype == 'DUPLICATE_SETTLEMENT' %}
            <span class="badge badge-orange">{{ etype }}</span>
          {% elif etype == 'AMOUNT_MISMATCH' %}
            <span class="badge badge-orange">{{ etype }}</span>
          {% elif etype == 'LATE_SETTLEMENT' %}
            <span class="badge badge-blue">{{ etype }}</span>
          {% elif etype == 'UNMATCHED_SETTLEMENT' %}
            <span class="badge badge-red">{{ etype }}</span>
          {% else %}
            <span class="badge badge-grey">{{ etype }}</span>
          {% endif %}
        </td>
        <td>{{ "{:,}".format(info.count) }}</td>
        <td>
          {% if info.is_delayed %}
            <em style="color:#6b7280">— delayed funds —</em>
          {% else %}
            ₹{{ "{:,.2f}".format(info.total_amount_at_risk_inr) }}
          {% endif %}
        </td>
        <td>
          {% if info.is_delayed %}
            <span class="badge badge-blue">Delayed</span>
          {% elif info.total_amount_at_risk_inr > 0 %}
            <span class="badge badge-red">At Risk</span>
          {% else %}
            <span class="badge badge-green">OK</span>
          {% endif %}
        </td>
      </tr>
    {% else %}
      <tr><td colspan="4" style="text-align:center;color:#9ca3af">No exceptions recorded.</td></tr>
    {% endfor %}
    </tbody>
  </table>
</div>

<!-- Run Info Footer -->
<footer>
  <div class="run-meta">
    <div>
      <strong>Run ID</strong><br>{{ run_id }}
    </div>
    <div>
      <strong>Run Date</strong><br>{{ run_date }}
    </div>
    <div>
      <strong>Ledger File Hash</strong><br>{{ ledger_file_hash or "—" }}
    </div>
    <div>
      <strong>Settlement File Hash</strong><br>{{ settlement_file_hash or "—" }}
    </div>
    <div>
      <strong>Run Status</strong><br>{{ run_status or "—" }}
    </div>
  </div>
</footer>

</body>
</html>
"""


def generate_daily_report(
    run_id: str, engine, output_dir: str = "reports", date_str: str | None = None, **kwargs
) -> Dict[str, Any]:
    """
    Generate a daily reconciliation report for the given run_id.

    Parameters
    ----------
    run_id : str
        The pipeline run to report on.
    engine : SQLAlchemy engine
        Connected to the reconciliation SQLite database.
    output_dir : str
        Directory where output files are written (created if absent).

    Returns
    -------
    dict
        All computed metrics plus the paths to the two output files.
    """
    os.makedirs(output_dir, exist_ok=True)

    with engine.connect() as conn:
        # ----------------------------------------------------------------
        # Run metadata
        # ----------------------------------------------------------------
        run_row = conn.execute(
            text(
                "SELECT run_id, started_at, ledger_file_hash, settlement_file_hash, status "
                "FROM run_log WHERE run_id = :run_id"
            ),
            {"run_id": run_id},
        ).fetchone()

        run_date = ""
        ledger_file_hash = ""
        settlement_file_hash = ""
        run_status = ""
        if run_row:
            started_at = run_row[1]
            run_date = started_at[:10] if started_at else ""
            ledger_file_hash = run_row[2] or ""
            settlement_file_hash = run_row[3] or ""
            run_status = run_row[4] or ""

        # ----------------------------------------------------------------
        # Total expected transactions & settlements
        # ----------------------------------------------------------------
        total_expected_txns = conn.execute(
            text("SELECT COUNT(*) FROM expected_transactions")
        ).scalar() or 0

        total_settlement_records = conn.execute(
            text("SELECT COUNT(*) FROM settlements")
        ).scalar() or 0

        # ----------------------------------------------------------------
        # Rejected rows for this run
        # ----------------------------------------------------------------
        total_rejected_rows = conn.execute(
            text(
                "SELECT COUNT(*) FROM rejected_rows WHERE run_id = :run_id"
            ),
            {"run_id": run_id},
        ).scalar() or 0

        # ----------------------------------------------------------------
        # Match type counts
        # ----------------------------------------------------------------
        match_counts = conn.execute(
            text(
                """
                SELECT match_type, COUNT(*) as cnt
                FROM reconciliation_results
                WHERE run_id = :run_id
                GROUP BY match_type
                """
            ),
            {"run_id": run_id},
        ).fetchall()

        match_type_map: Dict[str, int] = {}
        for row in match_counts:
            match_type_map[row[0]] = row[1]

        matched_exact_count = match_type_map.get("MATCHED_EXACT", 0)
        matched_fuzzy_count = match_type_map.get("MATCHED_FUZZY", 0)
        needs_review_count = match_type_map.get("NEEDS_REVIEW", 0)
        total_matched = matched_exact_count + matched_fuzzy_count
        total_unmatched_count = match_type_map.get("UNMATCHED", 0)

        # ----------------------------------------------------------------
        # Amount totals
        # ----------------------------------------------------------------
        total_expected_amount_paise = conn.execute(
            text("SELECT COALESCE(SUM(expected_amount_paise), 0) FROM expected_transactions")
        ).scalar() or 0

        # Gross/net settled only for matched rows
        settled_amounts = conn.execute(
            text(
                """
                SELECT
                    COALESCE(SUM(s.gross_amount_paise), 0),
                    COALESCE(SUM(s.net_amount_paise), 0)
                FROM reconciliation_results rr
                JOIN settlements s ON s.settlement_id = rr.settlement_id
                WHERE rr.run_id = :run_id
                  AND rr.match_type IN ('MATCHED_EXACT', 'MATCHED_FUZZY')
                """
            ),
            {"run_id": run_id},
        ).fetchone()

        total_gross_settled_paise = settled_amounts[0] if settled_amounts else 0
        total_net_settled_paise = settled_amounts[1] if settled_amounts else 0

        total_expected_amount_inr = total_expected_amount_paise / 100
        total_gross_settled_inr = total_gross_settled_paise / 100
        total_net_settled_inr = total_net_settled_paise / 100

        # ----------------------------------------------------------------
        # Exception breakdown with money at risk
        # ----------------------------------------------------------------
        exception_rows = conn.execute(
            text(
                """
                SELECT
                    rr.exception_type,
                    COUNT(*) as cnt,
                    COALESCE(SUM(et.expected_amount_paise), 0)   as sum_expected,
                    COALESCE(SUM(s.gross_amount_paise), 0)       as sum_gross,
                    COALESCE(SUM(ABS(COALESCE(rr.amount_diff_paise, 0))), 0) as sum_diff
                FROM reconciliation_results rr
                LEFT JOIN expected_transactions et ON et.txn_id = rr.txn_id
                LEFT JOIN settlements s ON s.settlement_id = rr.settlement_id
                WHERE rr.run_id = :run_id
                  AND rr.exception_type IS NOT NULL
                  AND rr.exception_type != ''
                GROUP BY rr.exception_type
                """
            ),
            {"run_id": run_id},
        ).fetchall()

        exception_breakdown: Dict[str, Dict[str, Any]] = {}
        money_at_risk_total_paise = 0

        for row in exception_rows:
            etype = row[0]
            count = row[1]
            sum_expected = row[2]
            sum_gross = row[3]
            sum_diff = row[4]

            is_delayed = etype == "LATE_SETTLEMENT"
            at_risk_paise = 0

            if not is_delayed:
                if etype == "MISSING_SETTLEMENT":
                    at_risk_paise = sum_expected
                elif etype == "DUPLICATE_SETTLEMENT":
                    at_risk_paise = sum_gross
                elif etype == "AMOUNT_MISMATCH":
                    at_risk_paise = sum_diff
                elif etype == "UNMATCHED_SETTLEMENT":
                    at_risk_paise = sum_gross
                elif etype in ("REFERENCE_MISMATCH", "PARTIAL_REFUND_CONFUSION"):
                    at_risk_paise = sum_expected
                else:
                    at_risk_paise = sum_expected  # fallback

                money_at_risk_total_paise += at_risk_paise

            exception_breakdown[etype] = {
                "count": count,
                "total_amount_at_risk_inr": round(at_risk_paise / 100, 2),
                "is_delayed": is_delayed,
            }

    money_at_risk_total_inr = round(money_at_risk_total_paise / 100, 2)

    # ----------------------------------------------------------------
    # Assemble report dict
    # ----------------------------------------------------------------
    report: Dict[str, Any] = {
        "run_id": run_id,
        "run_date": run_date,
        "ledger_file_hash": ledger_file_hash,
        "settlement_file_hash": settlement_file_hash,
        "run_status": run_status,
        "total_expected_txns": total_expected_txns,
        "total_settlement_records": total_settlement_records,
        "total_rejected_rows": total_rejected_rows,
        "matched_exact_count": matched_exact_count,
        "matched_fuzzy_count": matched_fuzzy_count,
        "needs_review_count": needs_review_count,
        "total_matched": total_matched,
        "total_unmatched_count": total_unmatched_count,
        "total_expected_amount_inr": round(total_expected_amount_inr, 2),
        "total_gross_settled_inr": round(total_gross_settled_inr, 2),
        "total_net_settled_inr": round(total_net_settled_inr, 2),
        "exception_breakdown": exception_breakdown,
        "money_at_risk_total_inr": money_at_risk_total_inr,
    }

    # ----------------------------------------------------------------
    # Determine date stamp (use run_date or today)
    # ----------------------------------------------------------------
    date_stamp = (run_date or datetime.now().strftime("%Y-%m-%d")).replace("-", "")

    # ----------------------------------------------------------------
    # Write CSV
    # ----------------------------------------------------------------
    csv_filename = f"daily_report_{date_stamp}_{run_id}.csv"
    csv_path = os.path.join(output_dir, csv_filename)

    csv_rows = [
        {"field": "run_id", "value": run_id},
        {"field": "run_date", "value": run_date},
        {"field": "ledger_file_hash", "value": ledger_file_hash},
        {"field": "settlement_file_hash", "value": settlement_file_hash},
        {"field": "run_status", "value": run_status},
        {"field": "total_expected_txns", "value": total_expected_txns},
        {"field": "total_settlement_records", "value": total_settlement_records},
        {"field": "total_rejected_rows", "value": total_rejected_rows},
        {"field": "matched_exact_count", "value": matched_exact_count},
        {"field": "matched_fuzzy_count", "value": matched_fuzzy_count},
        {"field": "needs_review_count", "value": needs_review_count},
        {"field": "total_matched", "value": total_matched},
        {"field": "total_unmatched_count", "value": total_unmatched_count},
        {"field": "total_expected_amount_inr", "value": report["total_expected_amount_inr"]},
        {"field": "total_gross_settled_inr", "value": report["total_gross_settled_inr"]},
        {"field": "total_net_settled_inr", "value": report["total_net_settled_inr"]},
        {"field": "money_at_risk_total_inr", "value": money_at_risk_total_inr},
    ]

    # Flatten exception breakdown into CSV rows
    for etype, info in exception_breakdown.items():
        csv_rows.append(
            {
                "field": f"exception.{etype}.count",
                "value": info["count"],
            }
        )
        csv_rows.append(
            {
                "field": f"exception.{etype}.total_amount_at_risk_inr",
                "value": info["total_amount_at_risk_inr"],
            }
        )

    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["field", "value"])
        writer.writeheader()
        writer.writerows(csv_rows)

    # ----------------------------------------------------------------
    # Write HTML via Jinja2
    # ----------------------------------------------------------------
    html_filename = f"daily_report_{date_stamp}_{run_id}.html"
    html_path = os.path.join(output_dir, html_filename)

    template = Template(HTML_TEMPLATE)
    html_content = template.render(
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        **report,
    )

    with open(html_path, "w", encoding="utf-8") as fh:
        fh.write(html_content)

    report["csv_path"] = csv_path
    report["html_path"] = html_path

    return report


if __name__ == "__main__":
    import sys
    import json
    from database import get_engine, init_db  # noqa: E402

    if len(sys.argv) < 2:
        print("Usage: python reporting.py <run_id> [db_path] [output_dir]")
        sys.exit(1)

    rid = sys.argv[1]
    db_path = sys.argv[2] if len(sys.argv) > 2 else "reconciliation.db"
    out_dir = sys.argv[3] if len(sys.argv) > 3 else "reports"

    eng = get_engine(db_path)
    init_db(eng)

    result = generate_daily_report(rid, eng, out_dir)
    print(json.dumps(result, indent=2, default=str))
