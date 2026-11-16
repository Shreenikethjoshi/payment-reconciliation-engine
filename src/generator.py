#!/usr/bin/env python3
"""
generator.py — Synthetic Data Generator for Payment Reconciliation.
Generates expected_ledger.csv, settlement_report.csv, and answer_key.csv with seed=42.
"""

import random
import string
import uuid
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from faker import Faker

SEED = 42
LEDGER_ROWS = 40_000
BASE_DATE = date(2026, 11, 1)
DATE_RANGE_DAYS = 30

RAZORPAY_FEE_RATE = 0.018
GST_RATE = 0.18

ORDER_MIN_INR = 299
ORDER_MAX_INR = 12_999

SETTLEMENT_DELAY = {"UPI": 1, "CARD": 2, "NETBANKING": 2}

N_MISSING = 300
N_DUPLICATE = 200
N_AMOUNT_MISMATCH = 400
N_LATE_SETTLEMENT = 350
N_REFERENCE_MISMATCH = 250
N_PARTIAL_REFUND = 100

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

random.seed(SEED)
np.random.seed(SEED)
fake = Faker("en_IN")
Faker.seed(SEED)


def rand_alphanumeric(length: int, rng: random.Random) -> str:
    chars = string.ascii_letters + string.digits
    return "".join(rng.choice(chars) for _ in range(length))


def make_reference(method: str, rng: random.Random) -> str:
    if method == "UPI":
        return "pay_" + rand_alphanumeric(14, rng)
    elif method == "CARD":
        bank = rng.choice(["HDFC", "ICICI", "SBI", "AXIS", "KOTAK"])
        digits = "".join(rng.choices(string.digits, k=6))
        return f"card_{bank}_TXN{digits}"
    else:
        bank = rng.choice(["ICICI", "HDFC", "SBI", "PNB", "BOB"])
        digits = "".join(rng.choices(string.digits, k=6))
        return f"nb_{bank}_TXN{digits}"


def calc_fees(gross_paise: int) -> tuple[int, int]:
    fee = round(gross_paise * RAZORPAY_FEE_RATE)
    gst = round(fee * GST_RATE)
    return fee, gst


def new_settlement_id() -> str:
    return "STL-" + uuid.uuid4().hex[:12]


def format_date_varied(d: date, style: int) -> str:
    s = style % 3
    if s == 0:
        return d.strftime("%Y-%m-%d")
    elif s == 1:
        return d.strftime("%d-%m-%Y")
    else:
        return d.strftime("%d/%m/%Y")


def add_whitespace_noise(ref: str, rng: random.Random) -> str:
    noise_type = rng.randint(0, 2)
    if noise_type == 0:
        return " " + ref
    elif noise_type == 1:
        return ref + " "
    else:
        mid = len(ref) // 2
        return ref[:mid] + "  " + ref[mid:]


def mutate_case(ref: str, rng: random.Random) -> str:
    return ref.upper() if rng.random() < 0.5 else ref.lower()


def mutate_reference(ref: str, rng: random.Random) -> str:
    mutation = rng.randint(0, 2)
    if mutation == 0:
        return ref[:-2] if len(ref) > 4 else ref[:-1]
    elif mutation == 1:
        lst = list(ref)
        idx = rng.randint(0, len(lst) - 2)
        lst[idx], lst[idx + 1] = lst[idx + 1], lst[idx]
        return "".join(lst)
    else:
        return "0" + ref


def build_ledger(rng: random.Random) -> pd.DataFrame:
    methods = ["UPI", "CARD", "NETBANKING"]
    method_weights = [0.60, 0.25, 0.15]

    rows = []
    for i in range(LEDGER_ROWS):
        txn_id = f"TXN-{i:06d}"
        method = rng.choices(methods, weights=method_weights)[0]
        ref = make_reference(method, rng)
        txn_date = BASE_DATE + timedelta(days=rng.randint(0, DATE_RANGE_DAYS - 1))
        amount_inr = rng.randint(ORDER_MIN_INR * 2, ORDER_MAX_INR * 2) / 2
        amount_paise = int(round(amount_inr * 100))
        cust_id = "CUS-" + "".join(rng.choices(string.digits, k=6))
        order_id = "ORD-" + rand_alphanumeric(8, rng)

        rows.append({
            "txn_id": txn_id,
            "reference": ref,
            "customer_id": cust_id,
            "order_id": order_id,
            "transaction_date": txn_date.strftime("%Y-%m-%d"),
            "expected_amount_paise": amount_paise,
            "payment_method": method,
            "currency": "INR",
        })

    return pd.DataFrame(rows)


def assign_errors(ledger: pd.DataFrame, rng: random.Random) -> dict:
    all_indices = list(range(len(ledger)))
    rng.shuffle(all_indices)

    assignments = {}
    cursor = 0

    def take(n: int, label: str):
        nonlocal cursor
        assignments[label] = all_indices[cursor: cursor + n]
        cursor += n

    take(N_MISSING, "MISSING_SETTLEMENT")
    take(N_DUPLICATE, "DUPLICATE_SETTLEMENT")
    take(N_AMOUNT_MISMATCH, "AMOUNT_MISMATCH")
    take(N_LATE_SETTLEMENT, "LATE_SETTLEMENT")
    take(N_REFERENCE_MISMATCH, "REFERENCE_MISMATCH")
    take(N_PARTIAL_REFUND, "PARTIAL_REFUND_CONFUSION")

    return assignments


def build_settlement_and_answer_key(
    ledger: pd.DataFrame,
    error_map: dict,
    rng: random.Random,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    iloc_to_error: dict[int, str] = {}
    for etype, indices in error_map.items():
        for idx in indices:
            iloc_to_error[idx] = etype

    all_amounts = ledger["expected_amount_paise"].values
    settlement_rows = []
    answer_key_rows = []

    noise_whitespace_rate = 0.15
    noise_case_rate = 0.10

    n = len(ledger)
    ws_flags = np.random.random(n) < noise_whitespace_rate
    case_flags = np.random.random(n) < noise_case_rate
    date_style = np.random.randint(0, 3, size=n)

    for i, row in enumerate(ledger.itertuples(index=False)):
        txn_date = date.fromisoformat(row.transaction_date)
        gross = int(row.expected_amount_paise)
        method = row.payment_method
        ref = row.reference

        normal_delay = SETTLEMENT_DELAY[method]
        normal_settle_date = txn_date + timedelta(days=normal_delay)

        fee, gst = calc_fees(gross)
        net = gross - fee - gst
        error_type = iloc_to_error.get(i)

        if error_type == "MISSING_SETTLEMENT":
            answer_key_rows.append({
                "txn_id": row.txn_id,
                "seeded_error_type": "MISSING_SETTLEMENT",
                "expected_reference": ref,
                "actual_reference": None,
                "expected_amount_paise": gross,
                "actual_gross_paise": None,
                "expected_date": normal_settle_date.strftime("%Y-%m-%d"),
                "actual_settlement_date": None,
                "notes": "Transaction absent from settlement file",
            })
            continue

        if error_type == "LATE_SETTLEMENT":
            settle_date = normal_settle_date + timedelta(days=2)
        else:
            settle_date = normal_settle_date

        if error_type == "AMOUNT_MISMATCH":
            pct = rng.uniform(0.05, 0.15)
            sign = rng.choice([-1, 1])
            actual_gross = int(round(gross * (1 + sign * pct)))
            actual_fee, actual_gst = calc_fees(actual_gross)
            actual_net = actual_gross - actual_fee - actual_gst
        else:
            actual_gross = gross
            actual_fee = fee
            actual_gst = gst
            actual_net = net

        if error_type == "REFERENCE_MISMATCH":
            actual_ref = mutate_reference(ref, rng)
        else:
            actual_ref = ref

        if error_type == "PARTIAL_REFUND_CONFUSION":
            smaller_mask = all_amounts < gross
            if smaller_mask.any():
                candidates = all_amounts[smaller_mask]
                confusing_net = int(rng.choice(candidates))
            else:
                confusing_net = max(0, actual_net - rng.randint(100, 500) * 100)
            actual_net = confusing_net

        raw_ref = actual_ref
        if ws_flags[i]:
            raw_ref = add_whitespace_noise(raw_ref, rng)
        if case_flags[i]:
            raw_ref = mutate_case(raw_ref, rng)

        formatted_settle_date = format_date_varied(settle_date, int(date_style[i]))
        s_id = new_settlement_id()
        settlement_rows.append({
            "settlement_id": s_id,
            "reference": actual_ref,
            "settlement_date": formatted_settle_date,
            "gross_amount_paise": actual_gross,
            "fee_amount_paise": actual_fee,
            "net_amount_paise": actual_net,
            "provider": "Razorpay",
            "raw_reference": raw_ref,
        })

        if error_type == "DUPLICATE_SETTLEMENT":
            dup_id = new_settlement_id()
            dup_style = (int(date_style[i]) + 1) % 3
            settlement_rows.append({
                "settlement_id": dup_id,
                "reference": actual_ref,
                "settlement_date": format_date_varied(settle_date, dup_style),
                "gross_amount_paise": actual_gross,
                "fee_amount_paise": actual_fee,
                "net_amount_paise": actual_net,
                "provider": "Razorpay",
                "raw_reference": raw_ref,
            })

        if error_type and error_type != "MISSING_SETTLEMENT":
            answer_key_rows.append({
                "txn_id": row.txn_id,
                "seeded_error_type": error_type,
                "expected_reference": ref,
                "actual_reference": actual_ref,
                "expected_amount_paise": gross,
                "actual_gross_paise": actual_gross,
                "expected_date": normal_settle_date.strftime("%Y-%m-%d"),
                "actual_settlement_date": settle_date.strftime("%Y-%m-%d"),
                "notes": _error_note(error_type, gross, actual_gross, ref, actual_ref,
                                     normal_settle_date, settle_date, actual_net),
            })

    return pd.DataFrame(settlement_rows), pd.DataFrame(answer_key_rows)


def _error_note(etype, gross, actual_gross, ref, actual_ref,
                expected_date, actual_date, actual_net) -> str:
    if etype == "DUPLICATE_SETTLEMENT":
        return "Second settlement row added with new settlement_id"
    elif etype == "AMOUNT_MISMATCH":
        diff = actual_gross - gross
        pct = abs(diff) / gross * 100
        direction = "over" if diff > 0 else "under"
        return f"Gross {direction}-reported by {pct:.1f}% ({abs(diff)} paise)"
    elif etype == "LATE_SETTLEMENT":
        extra = (actual_date - expected_date).days
        return f"Settlement {extra} days later than expected (normal delay exceeded)"
    elif etype == "REFERENCE_MISMATCH":
        return f"Reference mutated: '{ref}' -> '{actual_ref}'"
    elif etype == "PARTIAL_REFUND_CONFUSION":
        return (f"net_amount_paise={actual_net} matches gross of a different smaller "
                "transaction, creating partial-refund confusion")
    return ""


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    rng = random.Random(SEED)

    ledger = build_ledger(rng)
    error_map = assign_errors(ledger, rng)
    settlement, answer_key = build_settlement_and_answer_key(ledger, error_map, rng)

    ledger_path = DATA_DIR / "expected_ledger.csv"
    settlement_path = DATA_DIR / "settlement_report.csv"
    answer_key_path = DATA_DIR / "answer_key.csv"

    ledger.to_csv(ledger_path, index=False)
    settlement.to_csv(settlement_path, index=False)
    answer_key.to_csv(answer_key_path, index=False)

    return len(ledger), len(settlement), len(answer_key)


generate_all = main

if __name__ == "__main__":
    main()
