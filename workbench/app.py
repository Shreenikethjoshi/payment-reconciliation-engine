"""
app.py – Reconciliation Engine Exception Workbench (Enterprise Edition)
Professional financial reconciliation workbench for high-volume payment settlement.

Run with:  streamlit run workbench/app.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from datetime import datetime, date, timedelta

# Ensure project root is importable
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from sqlalchemy import func, text

from src.database import (
    Base,
    ExceptionResolution,
    ExpectedTransaction,
    ReconciliationResult,
    RunLog,
    Settlement,
    get_engine,
    init_db,
    get_session,
)

# ── Page Configuration ──────────────────────────────────────────────────────
st.set_page_config(
    page_title="Reconciliation Engine | Enterprise Operations",
    layout="wide",
    initial_sidebar_state="expanded",
)

DB_PATH = ROOT / "reconciliation.db"
REPORTS_DIR = ROOT / "reports"

# ── Modern Clean Enterprise CSS (Pure White / Light Slate) ──────────────────
CUSTOM_CSS = """
<style>
/* Eliminate Dark Header Bar */
header[data-testid="stHeader"], [data-testid="stHeader"] {
    background: #ffffff !important;
    background-color: #ffffff !important;
    border-bottom: 1px solid #f1f5f9 !important;
}

[data-testid="stToolbar"] {
    visibility: hidden !important;
    display: none !important;
}

/* Global Canvas */
[data-testid="stAppViewContainer"], .main {
    background-color: #ffffff !important;
    color: #0f172a !important;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Inter", sans-serif !important;
}

/* Sidebar Canvas & Text */
[data-testid="stSidebar"] {
    background-color: #f8fafc !important;
    border-right: 1px solid #e2e8f0 !important;
}

[data-testid="stSidebar"] * {
    color: #0f172a !important;
}

[data-testid="stSidebar"] .stRadio label {
    font-size: 0.9rem !important;
    font-weight: 500 !important;
    color: #1e293b !important;
    padding: 4px 0 !important;
}

/* Typography */
h1, h2, h3, h4, h5, h6 {
    color: #0f172a !important;
    font-weight: 700 !important;
    letter-spacing: -0.02em !important;
}

p, span, div, label {
    color: #334155;
}

/* Enterprise Header */
.top-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 0.8rem 0 1.5rem 0;
    border-bottom: 1px solid #e2e8f0;
    margin-bottom: 1.5rem;
}

.title-text {
    font-size: 1.5rem;
    font-weight: 800;
    color: #0f172a;
    letter-spacing: -0.02em;
}

.subtitle-text {
    color: #64748b;
    font-size: 0.85rem;
    margin-top: 3px;
}

.status-badge {
    background-color: #f1f5f9;
    color: #334155;
    font-size: 0.72rem;
    font-weight: 600;
    padding: 4px 10px;
    border-radius: 4px;
    border: 1px solid #cbd5e1;
    letter-spacing: 0.04em;
    text-transform: uppercase;
}

/* Metric KPI Cards */
.metric-card {
    background: #ffffff;
    border: 1px solid #e2e8f0;
    border-radius: 8px;
    padding: 1.2rem;
    box-shadow: 0 1px 2px 0 rgba(0, 0, 0, 0.04);
}

.metric-title {
    font-size: 0.75rem;
    text-transform: uppercase;
    font-weight: 600;
    letter-spacing: 0.05em;
    color: #64748b;
    margin-bottom: 6px;
}

.metric-val {
    font-size: 1.85rem;
    font-weight: 800;
    color: #0f172a;
    line-height: 1.1;
}

.metric-val.alert {
    color: #dc2626;
}

.metric-val.success {
    color: #16a34a;
}

.metric-desc {
    font-size: 0.76rem;
    color: #94a3b8;
    margin-top: 6px;
}

/* Proof comparison cards */
.proof-box {
    background: #f8fafc;
    border: 1px solid #e2e8f0;
    border-radius: 6px;
    padding: 14px 16px;
}

.proof-header {
    font-size: 0.82rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    font-weight: 700;
    color: #0f172a;
    margin-bottom: 8px;
}

.proof-row {
    font-size: 0.82rem;
    color: #334155;
    line-height: 1.7;
}

.proof-row b {
    color: #0f172a;
    font-weight: 600;
}

/* Tables and DataFrames */
[data-testid="stDataFrame"] {
    border: 1px solid #e2e8f0 !important;
    border-radius: 6px !important;
}

/* Buttons */
.stButton > button {
    background-color: #0f172a !important;
    color: #ffffff !important;
    font-weight: 600 !important;
    border-radius: 6px !important;
    border: 1px solid #0f172a !important;
    padding: 0.45rem 1rem !important;
    font-size: 0.85rem !important;
}

.stButton > button:hover {
    background-color: #1e293b !important;
    border-color: #1e293b !important;
}

hr {
    border: none !important;
    height: 1px !important;
    background-color: #e2e8f0 !important;
    margin: 1.5rem 0 !important;
}
</style>
"""
st.markdown(CUSTOM_CSS, unsafe_allow_html=True)


# ── Engine Singleton ────────────────────────────────────────────────────────
@st.cache_resource
def _engine():
    eng = get_engine(str(DB_PATH))
    init_db(eng)
    return eng


engine = _engine()


# ── Currency Formatter ──────────────────────────────────────────────────────
def fmt_inr(paise: float | int | None) -> str:
    """Format paise → ₹X,XX,XXX (Indian numbering format)."""
    if paise is None:
        return "₹0"
    inr = int(paise) // 100
    s = str(abs(inr))
    if len(s) <= 3:
        result = s
    else:
        result = s[-3:]
        s = s[:-3]
        while s:
            result = s[-2:] + "," + result
            s = s[:-2]
    return f"{'-' if inr < 0 else ''}₹{result}"


def _no_data(msg: str = "No data available yet. Execute the reconciliation pipeline first."):
    st.info(msg)


# ═══════════════════════════════════════════════════════════════════════════
# QUERIES
# ═══════════════════════════════════════════════════════════════════════════

@st.cache_data(ttl=30)
def q_total_transactions() -> int:
    with get_session(engine) as s:
        return s.query(func.count(ExpectedTransaction.txn_id)).scalar() or 0


@st.cache_data(ttl=30)
def q_latest_run_id() -> str | None:
    with get_session(engine) as s:
        row = s.query(RunLog.run_id).order_by(RunLog.started_at.desc()).first()
        return row[0] if row else None


@st.cache_data(ttl=30)
def q_dashboard_metrics(run_id: str | None) -> tuple[int, int, int]:
    if not run_id:
        return 0, 0, 0
    with get_session(engine) as s:
        matched = (
            s.query(func.count(ReconciliationResult.id))
            .filter(
                ReconciliationResult.run_id == run_id,
                ReconciliationResult.match_type.in_(["MATCHED_EXACT", "MATCHED_FUZZY"]),
            )
            .scalar()
            or 0
        )

        at_risk_rows = (
            s.query(
                ReconciliationResult.amount_diff_paise,
                ExpectedTransaction.expected_amount_paise,
                ReconciliationResult.exception_type,
            )
            .outerjoin(ExpectedTransaction, ExpectedTransaction.txn_id == ReconciliationResult.txn_id)
            .filter(
                ReconciliationResult.run_id == run_id,
                ReconciliationResult.exception_type.isnot(None),
                ReconciliationResult.match_type.notin_(["RESOLVED", "MATCHED_EXACT"]),
            )
            .all()
        )
        total_risk = 0
        for diff, exp, etype in at_risk_rows:
            if etype == "LATE_SETTLEMENT":
                continue
            elif etype == "AMOUNT_MISMATCH":
                total_risk += abs(diff or 0)
            elif exp:
                total_risk += exp

        open_exc = (
            s.query(func.count(ReconciliationResult.id))
            .filter(
                ReconciliationResult.run_id == run_id,
                ReconciliationResult.exception_type.isnot(None),
                ReconciliationResult.match_type != "RESOLVED",
            )
            .scalar()
            or 0
        )
        return matched, total_risk, open_exc


@st.cache_data(ttl=30)
def q_exception_type_breakdown(run_id: str) -> pd.DataFrame:
    with get_session(engine) as s:
        rows = (
            s.query(
                ReconciliationResult.exception_type,
                func.count(ReconciliationResult.id).label("count"),
            )
            .filter(
                ReconciliationResult.run_id == run_id,
                ReconciliationResult.exception_type.isnot(None),
            )
            .group_by(ReconciliationResult.exception_type)
            .all()
        )
        return pd.DataFrame(rows, columns=["Category", "Count"]).set_index("Category")


@st.cache_data(ttl=30)
def q_last_5_runs() -> pd.DataFrame:
    with get_session(engine) as s:
        rows = s.query(RunLog).order_by(RunLog.started_at.desc()).limit(5).all()
        if not rows:
            return pd.DataFrame()
        data = []
        for r in rows:
            matched = (
                s.query(func.count(ReconciliationResult.id))
                .filter(
                    ReconciliationResult.run_id == r.run_id,
                    ReconciliationResult.match_type.in_(["MATCHED_EXACT", "MATCHED_FUZZY"]),
                )
                .scalar()
                or 0
            )
            exceptions = (
                s.query(func.count(ReconciliationResult.id))
                .filter(
                    ReconciliationResult.run_id == r.run_id,
                    ReconciliationResult.exception_type.isnot(None),
                )
                .scalar()
                or 0
            )
            data.append(
                {
                    "Run ID": r.run_id,
                    "Execution Time": r.started_at.strftime("%Y-%m-%d %H:%M:%S") if r.started_at else "-",
                    "Status": r.status,
                    "Matched": f"{matched:,}",
                    "Exceptions": f"{exceptions:,}",
                    "Ledger Hash": (r.ledger_file_hash[:10] + "...") if r.ledger_file_hash else "-",
                }
            )
        return pd.DataFrame(data)


@st.cache_data(ttl=30)
def q_all_run_ids() -> list[str]:
    with get_session(engine) as s:
        rows = s.query(RunLog.run_id).order_by(RunLog.started_at.desc()).all()
        return [r[0] for r in rows]


@st.cache_data(ttl=30)
def q_exceptions(
    run_id: str | None,
    exc_types: list[str],
    statuses: list[str],
    date_from,
    date_to,
) -> pd.DataFrame:
    if not run_id:
        return pd.DataFrame()
    with get_session(engine) as s:
        q = (
            s.query(
                ReconciliationResult.id.label("exception_id"),
                ExpectedTransaction.reference.label("reference"),
                ReconciliationResult.exception_type,
                ExpectedTransaction.expected_amount_paise,
                Settlement.gross_amount_paise.label("actual_amount_paise"),
                ReconciliationResult.amount_diff_paise,
                ReconciliationResult.match_type.label("status"),
                ReconciliationResult.days_late,
                ReconciliationResult.reference_similarity,
                ReconciliationResult.match_score,
                ExpectedTransaction.transaction_date,
                ExpectedTransaction.payment_method,
                ExpectedTransaction.customer_id,
                ExpectedTransaction.order_id,
                Settlement.settlement_date,
                Settlement.net_amount_paise,
                Settlement.provider,
            )
            .outerjoin(ExpectedTransaction, ExpectedTransaction.txn_id == ReconciliationResult.txn_id)
            .outerjoin(Settlement, Settlement.settlement_id == ReconciliationResult.settlement_id)
            .filter(
                ReconciliationResult.run_id == run_id,
                ReconciliationResult.exception_type.isnot(None),
            )
        )
        if exc_types:
            q = q.filter(ReconciliationResult.exception_type.in_(exc_types))
        if statuses:
            q = q.filter(ReconciliationResult.match_type.in_(statuses))
        if date_from:
            q = q.filter(ExpectedTransaction.transaction_date >= date_from)
        if date_to:
            q = q.filter(ExpectedTransaction.transaction_date <= date_to)

        rows = q.order_by(ReconciliationResult.id.asc()).all()
        if not rows:
            return pd.DataFrame()
        return pd.DataFrame([r._asdict() for r in rows])


@st.cache_data(ttl=30)
def q_resolution_history(result_id: int) -> pd.DataFrame:
    with get_session(engine) as s:
        rows = (
            s.query(ExceptionResolution)
            .filter(ExceptionResolution.result_id == result_id)
            .order_by(ExceptionResolution.resolved_at.desc())
            .all()
        )
        if not rows:
            return pd.DataFrame()
        return pd.DataFrame(
            [
                {
                    "Timestamp": r.resolved_at.strftime("%Y-%m-%d %H:%M:%S") if r.resolved_at else "-",
                    "Resolved By": r.resolved_by,
                    "Previous Status": r.previous_status,
                    "New Status": r.new_status,
                    "Resolution Notes": r.resolution_reason,
                }
                for r in rows
            ]
        )


@st.cache_data(ttl=30)
def q_evaluation_report() -> pd.DataFrame:
    path = REPORTS_DIR / "evaluation_report.csv"
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path)


def resolve_exception(result_id: int, reason: str, resolved_by: str, new_status: str):
    with get_session(engine) as s:
        result = s.query(ReconciliationResult).filter(ReconciliationResult.id == result_id).first()
        if result is None:
            return
        prev_status = result.match_type
        resolution = ExceptionResolution(
            result_id=result_id,
            resolved_by=resolved_by,
            resolution_reason=reason,
            previous_status=prev_status,
            new_status=new_status,
        )
        result.match_type = new_status
        s.add(resolution)
    q_exceptions.clear()
    q_resolution_history.clear()
    q_dashboard_metrics.clear()


# ═══════════════════════════════════════════════════════════════════════════
# SIDEBAR
# ═══════════════════════════════════════════════════════════════════════════

with st.sidebar:
    st.markdown(
        """
        <div style="padding: 12px 0 20px 0; border-bottom: 1px solid #e2e8f0; margin-bottom: 16px;">
            <div style="font-size: 1.15rem; font-weight: 800; color: #0f172a; letter-spacing: -0.01em;">
                Reconciliation Engine
            </div>
            <div style="font-size: 0.75rem; color: #64748b; font-weight: 500; margin-top: 2px;">
                Internal Ledger vs Gateway Settlement
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    page = st.radio(
        "Navigation",
        ["Dashboard", "Exception Workbench", "Daily Reports", "Evaluation Metrics"],
        label_visibility="collapsed",
    )

    st.markdown("---")
    st.caption("Engine: Two-Pass Hybrid Matcher")
    st.caption("Storage: SQLite WAL Mode")


# ═══════════════════════════════════════════════════════════════════════════
# 1. DASHBOARD
# ═══════════════════════════════════════════════════════════════════════════

if page == "Dashboard":
    st.markdown(
        """
        <div class="top-header">
            <div>
                <div class="title-text">Reconciliation Overview</div>
                <div class="subtitle-text">Daily transaction settlement status and financial exposure.</div>
            </div>
            <div>
                <span class="status-badge">Automated Run Ready</span>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    latest_run = q_latest_run_id()
    total_txns = q_total_transactions()
    matched, money_at_risk_paise, open_exc = q_dashboard_metrics(latest_run)

    # 4 Metric Cards
    c1, c2, c3, c4 = st.columns(4)

    with c1:
        st.markdown(
            f"""
            <div class="metric-card">
                <div class="metric-title">Total Orders</div>
                <div class="metric-val">{total_txns:,}</div>
                <div class="metric-desc">Expected ledger records</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    with c2:
        st.markdown(
            f"""
            <div class="metric-card">
                <div class="metric-title">Reconciled</div>
                <div class="metric-val success">{matched:,}</div>
                <div class="metric-desc">Exact & high-confidence matches</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    with c3:
        st.markdown(
            f"""
            <div class="metric-card">
                <div class="metric-title">Money at Risk</div>
                <div class="metric-val alert">{fmt_inr(money_at_risk_paise)}</div>
                <div class="metric-desc">Disputed variance & missing items</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    with c4:
        st.markdown(
            f"""
            <div class="metric-card">
                <div class="metric-title">Open Exceptions</div>
                <div class="metric-val">{open_exc:,}</div>
                <div class="metric-desc">Transactions requiring review</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    st.markdown("<br>", unsafe_allow_html=True)

    col_chart, col_table = st.columns([1, 1.2])

    with col_chart:
        st.markdown("##### Exceptions by Category")
        if latest_run:
            breakdown_df = q_exception_type_breakdown(latest_run)
            if breakdown_df.empty:
                _no_data("No exceptions flagged in current run.")
            else:
                st.bar_chart(breakdown_df, color="#2563eb", height=300)
        else:
            _no_data()

    with col_table:
        st.markdown("##### Recent Execution Pipeline Runs")
        runs_df = q_last_5_runs()
        if runs_df.empty:
            _no_data("No reconciliation runs recorded yet.")
        else:
            st.dataframe(runs_df, use_container_width=True, hide_index=True)


# ═══════════════════════════════════════════════════════════════════════════
# 2. EXCEPTION WORKBENCH
# ═══════════════════════════════════════════════════════════════════════════

elif page == "Exception Workbench":
    st.markdown(
        """
        <div class="top-header">
            <div>
                <div class="title-text">Exception Workbench</div>
                <div class="subtitle-text">Review discrepancies, examine source proofs, and record resolution audits.</div>
            </div>
            <div>
                <span class="status-badge">Audit Trail Enabled</span>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    ALL_EXCEPTION_TYPES = [
        "MISSING_SETTLEMENT",
        "DUPLICATE_SETTLEMENT",
        "AMOUNT_MISMATCH",
        "LATE_SETTLEMENT",
        "REFERENCE_MISMATCH",
        "PARTIAL_REFUND_CONFUSION",
        "AMBIGUOUS_MATCH",
        "UNMATCHED_SETTLEMENT",
    ]
    ALL_STATUSES = ["OPEN", "RESOLVED", "REVIEWING", "ESCALATED", "NEEDS_REVIEW", "UNMATCHED"]

    all_runs = q_all_run_ids()
    if not all_runs:
        _no_data()
        st.stop()

    filter_c1, filter_c2, filter_c3, filter_c4 = st.columns([1.2, 1.5, 1.2, 1.2])

    with filter_c1:
        sel_run = st.selectbox("Pipeline Run", all_runs)
    with filter_c2:
        sel_types = st.multiselect("Category Filter", ALL_EXCEPTION_TYPES, default=[])
    with filter_c3:
        sel_statuses = st.multiselect("Status Filter", ALL_STATUSES, default=[])
    with filter_c4:
        date_filter = st.date_input(
            "Transaction Date Window",
            value=(date.today() - timedelta(days=45), date.today()),
        )
        date_from = date_filter[0] if len(date_filter) > 0 else None
        date_to = date_filter[1] if len(date_filter) > 1 else None

    exc_df = q_exceptions(sel_run, sel_types, sel_statuses, date_from, date_to)

    if exc_df.empty:
        _no_data("No exception records match the current filter criteria.")
        st.stop()

    total_rows = len(exc_df)
    PAGE_SIZE = 20
    max_page = max(1, (total_rows - 1) // PAGE_SIZE + 1)

    th_c1, th_c2 = st.columns([3, 1])
    with th_c1:
        st.markdown(f"##### Flagged Discrepancies ({total_rows:,} records)")
    with th_c2:
        page_num = st.number_input("Page", min_value=1, max_value=max_page, value=1, step=1)

    start_idx = (page_num - 1) * PAGE_SIZE
    page_df = exc_df.iloc[start_idx : start_idx + PAGE_SIZE].copy()

    display_cols = {
        "exception_id": "ID",
        "reference": "Reference",
        "exception_type": "Category",
        "expected_amount_paise": "Expected",
        "actual_amount_paise": "Settled",
        "amount_diff_paise": "Variance",
        "status": "Status",
        "days_late": "Lag (Days)",
    }
    view_df = page_df[[c for c in display_cols if c in page_df.columns]].rename(columns=display_cols)

    for col in ["Expected", "Settled", "Variance"]:
        if col in view_df.columns:
            view_df[col] = view_df[col].apply(lambda v: f"₹{int(v / 100):,}" if pd.notna(v) else "-")

    st.dataframe(view_df, use_container_width=True, hide_index=True)
    st.caption(f"Showing items {start_idx + 1}–{min(start_idx + PAGE_SIZE, total_rows)} of {total_rows:,}")

    st.markdown("---")

    # Side-by-Side Proof Comparison & Resolution
    col_details, col_action = st.columns([1.4, 1])

    with col_details:
        st.markdown("##### Transaction Comparison Proof")
        exc_ids = exc_df["exception_id"].tolist()
        sel_exc_id = st.selectbox(
            "Select Item to Inspect:",
            options=exc_ids,
            format_func=lambda x: f"Record #{x} | Ref: {exc_df.loc[exc_df['exception_id'] == x, 'reference'].values[0]} ({exc_df.loc[exc_df['exception_id'] == x, 'exception_type'].values[0]})",
        )

        if sel_exc_id is not None:
            sel_row = exc_df[exc_df["exception_id"] == sel_exc_id].iloc[0]

            left_box, right_box = st.columns(2)
            with left_box:
                st.markdown(
                    f"""
                    <div class="proof-box">
                        <div class="proof-header">Expected Ledger</div>
                        <div class="proof-row"><b>Reference:</b> <code>{sel_row.get('reference', '-')}</code></div>
                        <div class="proof-row"><b>Order Amount:</b> {fmt_inr(sel_row.get('expected_amount_paise', 0))}</div>
                        <div class="proof-row"><b>Order ID:</b> {sel_row.get('order_id', '-')}</div>
                        <div class="proof-row"><b>Customer ID:</b> {sel_row.get('customer_id', '-')}</div>
                        <div class="proof-row"><b>Txn Date:</b> {sel_row.get('transaction_date', '-')}</div>
                        <div class="proof-row"><b>Payment Rail:</b> {sel_row.get('payment_method', '-')}</div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

            with right_box:
                diff_val = sel_row.get("amount_diff_paise")
                ref_sim = sel_row.get("reference_similarity")
                st.markdown(
                    f"""
                    <div class="proof-box">
                        <div class="proof-header">Bank Settlement</div>
                        <div class="proof-row"><b>Provider:</b> {sel_row.get('provider', 'Razorpay')}</div>
                        <div class="proof-row"><b>Gross Settled:</b> {fmt_inr(sel_row.get('actual_amount_paise', 0))}</div>
                        <div class="proof-row"><b>Net Settled:</b> {fmt_inr(sel_row.get('net_amount_paise', 0))}</div>
                        <div class="proof-row"><b>Settlement Date:</b> {sel_row.get('settlement_date', '-')}</div>
                        <div class="proof-row"><b>Variance:</b> <span style="color: #dc2626; font-weight: 600;">{fmt_inr(diff_val) if pd.notna(diff_val) else '-'}</span></div>
                        <div class="proof-row"><b>Ref Similarity:</b> {f'{ref_sim:.1f}%' if pd.notna(ref_sim) else '-'}</div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

            st.markdown("<br>", unsafe_allow_html=True)
            st.markdown("###### Resolution History")
            hist_df = q_resolution_history(int(sel_exc_id))
            if hist_df.empty:
                st.caption("No resolution actions recorded yet.")
            else:
                st.dataframe(hist_df, use_container_width=True, hide_index=True)

    with col_action:
        st.markdown("##### Record Resolution")
        with st.form(key=f"resolve_form_{sel_exc_id}"):
            res_reason = st.text_area(
                "Audit Reason",
                placeholder="Enter justification (e.g. MDR fee adjustment verified, payment timing approved).",
                height=110,
            )
            res_user = st.text_input("Reviewer", value="finance_ops")
            res_status = st.selectbox("Resolution State", ["RESOLVED", "REVIEWING", "ESCALATED"])
            res_submit = st.form_submit_button("Confirm Resolution")

            if res_submit:
                if not res_reason.strip():
                    st.error("Resolution reason is required for financial compliance.")
                else:
                    resolve_exception(int(sel_exc_id), res_reason.strip(), res_user.strip(), res_status)
                    st.success(f"Item #{sel_exc_id} marked as {res_status}.")
                    st.rerun()

    # Bulk Operations
    st.markdown("---")
    with st.expander("Batch Operations"):
        all_ids_in_view = exc_df["exception_id"].tolist()
        bulk_select_all = st.checkbox("Select all filtered exceptions")
        if bulk_select_all:
            bulk_targets = all_ids_in_view
            st.caption(f"{len(bulk_targets):,} transactions selected.")
        else:
            bulk_targets = st.multiselect("Select individual IDs:", options=all_ids_in_view, format_func=str)

        b_c1, b_c2 = st.columns([2, 1])
        with b_c1:
            bulk_justification = st.text_area("Batch Reason", placeholder="e.g. Standard holiday settlement latency.")
        with b_c2:
            bulk_officer = st.text_input("Reviewer Name", value="ops_lead")
            bulk_target_status = st.selectbox("Target State", ["RESOLVED", "REVIEWING", "ESCALATED"], key="bulk_st")

        if st.button("Apply to Selected"):
            if not bulk_targets:
                st.error("No transactions selected.")
            elif not bulk_justification.strip():
                st.error("Batch justification is required.")
            else:
                count = len(bulk_targets)
                prog_bar = st.progress(0)
                for idx, item_id in enumerate(bulk_targets, 1):
                    resolve_exception(int(item_id), bulk_justification.strip(), bulk_officer.strip(), bulk_target_status)
                    prog_bar.progress(idx / count)
                    time.sleep(0.005)
                st.success(f"Successfully updated {count:,} records.")
                st.rerun()


# ═══════════════════════════════════════════════════════════════════════════
# 3. DAILY REPORTS
# ═══════════════════════════════════════════════════════════════════════════

elif page == "Daily Reports":
    st.markdown(
        """
        <div class="top-header">
            <div>
                <div class="title-text">Daily Reconciliation Reports</div>
                <div class="subtitle-text">Reconciliation certificates and structured financial breakdown.</div>
            </div>
            <div>
                <span class="status-badge">Certified Output</span>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if not REPORTS_DIR.exists():
        _no_data("No reports generated yet.")
        st.stop()

    html_reports = sorted(REPORTS_DIR.glob("daily_report_*.html"), reverse=True)
    csv_reports = sorted(REPORTS_DIR.glob("daily_report_*.csv"), reverse=True)
    all_reports = html_reports + csv_reports

    if not all_reports:
        _no_data("No daily reports found in reports/ directory.")
        st.stop()

    report_picker = st.selectbox("Available Reports", [r.name for r in all_reports])
    selected_path = REPORTS_DIR / report_picker

    if report_picker.endswith(".html"):
        html_code = selected_path.read_text(encoding="utf-8")
        components.html(html_code, height=850, scrolling=True)
    elif report_picker.endswith(".csv"):
        df_report = pd.read_csv(selected_path)
        st.dataframe(df_report, use_container_width=True)


# ═══════════════════════════════════════════════════════════════════════════
# 4. EVALUATION METRICS
# ═══════════════════════════════════════════════════════════════════════════

elif page == "Evaluation Metrics":
    st.markdown(
        """
        <div class="top-header">
            <div>
                <div class="title-text">Matcher Accuracy & Evaluation</div>
                <div class="subtitle-text">Precision and recall verified against the independent ground-truth answer key.</div>
            </div>
            <div>
                <span class="status-badge">Ground Truth Verified</span>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    eval_df = q_evaluation_report()
    if eval_df.empty:
        _no_data("No evaluation report generated yet.")
        st.stop()

    st.markdown("##### Precision & Recall by Error Type")

    # Format numeric float columns cleanly
    formatted_eval = eval_df.copy()
    for col in ["precision", "recall", "f1"]:
        if col in formatted_eval.columns:
            formatted_eval[col] = formatted_eval[col].apply(lambda x: f"{x:.3f}" if isinstance(x, (int, float)) else x)

    st.dataframe(formatted_eval, use_container_width=True, hide_index=True)

    st.markdown("---")
    st.markdown("##### Safe Reruns & Idempotency Audit")

    all_runs = q_all_run_ids()
    if len(all_runs) >= 2:
        with get_session(engine) as s:
            rows = (
                s.query(
                    ReconciliationResult.run_id,
                    func.count(ReconciliationResult.id).label("total"),
                    func.sum(
                        (ReconciliationResult.match_type == "MATCHED_EXACT").cast(
                            type_=__import__("sqlalchemy", fromlist=["Integer"]).Integer
                        )
                    ).label("exact"),
                    func.sum(
                        (ReconciliationResult.match_type == "MATCHED_FUZZY").cast(
                            type_=__import__("sqlalchemy", fromlist=["Integer"]).Integer
                        )
                    ).label("fuzzy"),
                )
                .group_by(ReconciliationResult.run_id)
                .all()
            )
            audit_df = pd.DataFrame(rows, columns=["Pipeline Run", "Total Records", "Exact Matches", "Fuzzy Matches"])
            st.dataframe(audit_df, use_container_width=True, hide_index=True)
            st.caption("Consecutive pipeline runs on identical source files create no duplicate records and produce identical results.")
    else:
        st.info("Execute pipeline multiple times to view cross-run idempotency logs.")
