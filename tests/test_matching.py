import pytest
from datetime import date, datetime
from sqlalchemy import text
from src.database import ExpectedTransaction, Settlement, RunLog, get_session
from src.matcher import run_matching, normalize_reference, calc_similarity_score

def test_normalize_reference():
    assert normalize_reference("  pay_1234  ") == "PAY_1234"
    assert normalize_reference(" pay_ABC ") == "PAY_ABC"

def test_calc_similarity_score():
    score, is_penalized = calc_similarity_score("PAY_QKXXYZ001", "PAY_QKXXYZ002")
    # Should detect numeric suffix penalty (stem PAY_QKXXYZ length >= 8)
    assert is_penalized is True

def test_exact_match(in_memory_engine):
    with get_session(in_memory_engine) as session:
        session.add(RunLog(run_id="RUN-TEST-001", started_at=datetime.utcnow()))
        txn = ExpectedTransaction(
            txn_id="TXN-000001",
            reference="pay_QkX123456",
            customer_id="CUS-100001",
            order_id="ORD-00000001",
            transaction_date=date(2026, 11, 1),
            expected_amount_paise=150000,
            payment_method="UPI",
            currency="INR"
        )
        stl = Settlement(
            settlement_id="STL-000000000001",
            reference="pay_QkX123456",
            settlement_date=date(2026, 11, 2),
            gross_amount_paise=150000,
            fee_amount_paise=2700,
            net_amount_paise=147300,
            provider="Razorpay",
            raw_reference="pay_QkX123456"
        )
        session.add(txn)
        session.add(stl)

    stats = run_matching("RUN-TEST-001", in_memory_engine)
    assert stats["exact_matches"] == 1
    assert stats["unmatched"] == 0

def test_missing_settlement(in_memory_engine):
    with get_session(in_memory_engine) as session:
        session.add(RunLog(run_id="RUN-TEST-002", started_at=datetime.utcnow()))
        txn = ExpectedTransaction(
            txn_id="TXN-000002",
            reference="pay_MISSING123",
            customer_id="CUS-100002",
            order_id="ORD-00000002",
            transaction_date=date(2026, 11, 1),
            expected_amount_paise=250000,
            payment_method="UPI",
            currency="INR"
        )
        session.add(txn)

    stats = run_matching("RUN-TEST-002", in_memory_engine)
    assert stats["unmatched"] == 1
    assert stats["exact_matches"] == 0

def test_amount_mismatch(in_memory_engine):
    with get_session(in_memory_engine) as session:
        session.add(RunLog(run_id="RUN-TEST-003", started_at=datetime.utcnow()))
        txn = ExpectedTransaction(
            txn_id="TXN-000003",
            reference="pay_DIFF_AMT",
            customer_id="CUS-100003",
            order_id="ORD-00000003",
            transaction_date=date(2026, 11, 1),
            expected_amount_paise=100000,
            payment_method="UPI",
            currency="INR"
        )
        stl = Settlement(
            settlement_id="STL-000000000003",
            reference="pay_DIFF_AMT",
            settlement_date=date(2026, 11, 2),
            gross_amount_paise=90000, # 10% mismatch
            fee_amount_paise=1620,
            net_amount_paise=88380,
            provider="Razorpay",
            raw_reference="pay_DIFF_AMT"
        )
        session.add(txn)
        session.add(stl)

    stats = run_matching("RUN-TEST-003", in_memory_engine)
    with in_memory_engine.connect() as conn:
        res = conn.execute(text("SELECT exception_type FROM reconciliation_results WHERE txn_id='TXN-000003'")).fetchone()
        assert res[0] == "AMOUNT_MISMATCH"
