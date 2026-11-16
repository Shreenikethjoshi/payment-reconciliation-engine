import os
from pathlib import Path
import pandas as pd
import pytest

from src.generator import generate_all

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"

def test_generator_creates_all_files():
    expected_ledger = DATA_DIR / "expected_ledger.csv"
    settlement_report = DATA_DIR / "settlement_report.csv"
    answer_key = DATA_DIR / "answer_key.csv"

    assert expected_ledger.exists(), "expected_ledger.csv should exist"
    assert settlement_report.exists(), "settlement_report.csv should exist"
    assert answer_key.exists(), "answer_key.csv should exist"

def test_ledger_row_count():
    df_ledger = pd.read_csv(DATA_DIR / "expected_ledger.csv")
    assert len(df_ledger) >= 10000, f"Expected at least 10,000 ledger rows, got {len(df_ledger)}"

def test_settlement_has_errors():
    df_ak = pd.read_csv(DATA_DIR / "answer_key.csv")
    assert "seeded_error_type" in df_ak.columns
    error_types = set(df_ak["seeded_error_type"].unique())
    required_errors = {
        "MISSING_SETTLEMENT",
        "DUPLICATE_SETTLEMENT",
        "AMOUNT_MISMATCH",
        "LATE_SETTLEMENT",
        "REFERENCE_MISMATCH",
    }
    assert required_errors.issubset(error_types), f"Missing required error types: {required_errors - error_types}"

def test_no_overlap_between_error_types():
    df_ak = pd.read_csv(DATA_DIR / "answer_key.csv")
    txn_counts = df_ak["txn_id"].value_counts()
    duplicates = txn_counts[txn_counts > 1]
    assert len(duplicates) == 0, f"Found txn_ids assigned multiple error types: {duplicates.to_dict()}"

def test_reference_formats():
    df_ledger = pd.read_csv(DATA_DIR / "expected_ledger.csv")
    prefixes = df_ledger["reference"].apply(lambda r: r.split("_")[0]).unique()
    valid_prefixes = {"pay", "card", "nb"}
    assert any(p in valid_prefixes for p in prefixes), f"Unexpected reference prefixes: {prefixes}"
