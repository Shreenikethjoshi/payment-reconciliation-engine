import pytest
from datetime import date, datetime
from sqlalchemy import text
from src.database import ExpectedTransaction, Settlement, RunLog, get_session
from src.matcher import run_matching

def test_idempotent_matching(in_memory_engine):
    """Running matcher twice with same data produces identical result counts and no primary key collision errors."""
    with get_session(in_memory_engine) as session:
        session.add(RunLog(run_id="RUN-IDEMP-1", started_at=datetime.utcnow()))
        session.add(RunLog(run_id="RUN-IDEMP-2", started_at=datetime.utcnow()))
        txn = ExpectedTransaction(
            txn_id="TXN-IDEMP-01",
            reference="pay_IDEMP001",
            customer_id="CUS-IDEMP1",
            order_id="ORD-IDEMP1",
            transaction_date=date(2026, 11, 1),
            expected_amount_paise=50000,
            payment_method="UPI",
            currency="INR"
        )
        stl = Settlement(
            settlement_id="STL-IDEMP-01",
            reference="pay_IDEMP001",
            settlement_date=date(2026, 11, 2),
            gross_amount_paise=50000,
            fee_amount_paise=900,
            net_amount_paise=49100,
            provider="Razorpay",
            raw_reference="pay_IDEMP001"
        )
        session.add(txn)
        session.add(stl)

    # First run
    stats1 = run_matching("RUN-IDEMP-1", in_memory_engine)
    assert stats1["exact_matches"] == 1

    # Second run with different run_id on same dataset
    stats2 = run_matching("RUN-IDEMP-2", in_memory_engine)
    assert stats2["exact_matches"] == 1

    # Same run_id rerun (pure idempotency check - no duplicate inserts)
    stats3 = run_matching("RUN-IDEMP-1", in_memory_engine)
    
    with in_memory_engine.connect() as conn:
        count_run1 = conn.execute(
            text("SELECT COUNT(*) FROM reconciliation_results WHERE run_id='RUN-IDEMP-1'")
        ).scalar()
        assert count_run1 == 1, f"Expected 1 record for RUN-IDEMP-1, got {count_run1}"
