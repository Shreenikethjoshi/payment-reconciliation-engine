"""
matcher.py — Two-pass Payment Reconciliation Matching Engine.
Pass 1: Exact matching on normalized reference and gross amount.
Pass 2: Fuzzy matching with candidate blocking (±5 days) and composite scoring.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Set, Tuple

from rapidfuzz import fuzz
from sqlalchemy import text

try:
    from src.database import ExpectedTransaction, ReconciliationResult, Settlement, get_session
except ImportError:
    from database import ExpectedTransaction, ReconciliationResult, Settlement, get_session

logger = logging.getLogger(__name__)

NORMAL_T: Dict[str, int] = {
    "UPI": 1,
    "CARD": 2,
    "NETBANKING": 2,
}

MAX_AMOUNT_PCT_DIFF = 0.20
MAX_DATE_DIFF_DAYS = 3
BLOCKING_WINDOW_DAYS = 5

FUZZY_AUTO_THRESHOLD = 90.0
FUZZY_REVIEW_THRESHOLD = 70.0
REF_SIM_HIGH = 85.0

NUMERIC_SUFFIX_PREFIX_LEN = 8
NUMERIC_SUFFIX_PENALTY = 15.0

_TRAILING_DIGITS_RE = re.compile(r"\d+$")


def _normalise_ref(ref: str) -> str:
    return (ref or "").strip().upper()


normalize_reference = _normalise_ref


def _has_numeric_suffix_only_diff(ref_a: str, ref_b: str) -> bool:
    stem_a = _TRAILING_DIGITS_RE.sub("", ref_a)
    stem_b = _TRAILING_DIGITS_RE.sub("", ref_b)
    if stem_a != stem_b or len(stem_a) < NUMERIC_SUFFIX_PREFIX_LEN:
        return False
    return ref_a != ref_b


def calc_similarity_score(ref_a: str, ref_b: str):
    norm_a = _normalise_ref(ref_a)
    norm_b = _normalise_ref(ref_b)
    penalized = _has_numeric_suffix_only_diff(norm_a, norm_b)
    score = fuzz.token_sort_ratio(norm_a, norm_b)
    if penalized:
        score = max(0.0, score - 15.0)
    return score, penalized


def _composite_score(
    norm_ref_txn: str,
    norm_ref_stl: str,
    txn_date: date,
    stl_date: date,
    expected_paise: int,
    gross_paise: int,
) -> Tuple[float, float, float, float, float]:
    days_diff = abs((txn_date - stl_date).days)
    pct_diff = abs(expected_paise - gross_paise) / max(expected_paise, 1)

    ref_score: float = fuzz.token_sort_ratio(norm_ref_txn, norm_ref_stl)
    date_score: float = max(0.0, 100.0 - days_diff * 25.0)
    amount_score: float = max(0.0, 100.0 - pct_diff * 100.0)

    composite = 0.5 * ref_score + 0.2 * date_score + 0.3 * amount_score
    if _has_numeric_suffix_only_diff(norm_ref_txn, norm_ref_stl):
        composite -= NUMERIC_SUFFIX_PENALTY

    return composite, ref_score, date_score, amount_score, float(days_diff)


def _insert_result(session, row: dict) -> None:
    if row.get("txn_id") is not None:
        stmt = text(
            """
            INSERT OR IGNORE INTO reconciliation_results
                (run_id, txn_id, settlement_id, match_type, match_score,
                 exception_type, days_late, amount_diff_paise,
                 reference_similarity, created_at)
            VALUES
                (:run_id, :txn_id, :settlement_id, :match_type, :match_score,
                 :exception_type, :days_late, :amount_diff_paise,
                 :reference_similarity, :created_at)
            """
        )
    else:
        stmt = text(
            """
            INSERT INTO reconciliation_results
                (run_id, txn_id, settlement_id, match_type, match_score,
                 exception_type, days_late, amount_diff_paise,
                 reference_similarity, created_at)
            VALUES
                (:run_id, :txn_id, :settlement_id, :match_type, :match_score,
                 :exception_type, :days_late, :amount_diff_paise,
                 :reference_similarity, :created_at)
            """
        )
    session.execute(stmt, {**row, "created_at": datetime.utcnow().isoformat()})


def run_matching(run_id: str, engine) -> dict:
    """Execute two-pass reconciliation and persist results."""
    logger.info("run_matching: starting run_id=%s", run_id)

    with get_session(engine) as session:
        txns: List[ExpectedTransaction] = session.query(ExpectedTransaction).all()
        settlements: List[Settlement] = session.query(Settlement).all()

        txn_norm: Dict[str, str] = {t.txn_id: _normalise_ref(t.reference) for t in txns}
        stl_norm: Dict[str, str] = {s.settlement_id: _normalise_ref(s.reference) for s in settlements}

        stl_by_norm_ref: Dict[str, List[Settlement]] = defaultdict(list)
        for s in settlements:
            stl_by_norm_ref[stl_norm[s.settlement_id]].append(s)

        stl_by_date: Dict[date, List[Settlement]] = defaultdict(list)
        for s in settlements:
            stl_by_date[s.settlement_date].append(s)

        used_settlement_ids: Set[str] = set()
        unmatched_txn_ids: Set[str] = set()

        exact_matches = 0
        fuzzy_matches = 0
        needs_review = 0
        ambiguous = 0

        rows_to_insert: List[dict] = []

        # Pass 1: Exact matching
        for txn in txns:
            norm_ref = txn_norm[txn.txn_id]
            candidates = stl_by_norm_ref.get(norm_ref, [])
            available_exact = [
                s for s in candidates
                if s.settlement_id not in used_settlement_ids and s.gross_amount_paise == txn.expected_amount_paise
            ]

            expected_fee = round(txn.expected_amount_paise * 0.018)
            expected_gst = round(expected_fee * 0.18)
            normal_net = txn.expected_amount_paise - expected_fee - expected_gst

            if available_exact:
                matched_stl = available_exact[0]
                used_settlement_ids.add(matched_stl.settlement_id)

                days_diff = (matched_stl.settlement_date - txn.transaction_date).days
                normal_t = NORMAL_T.get(txn.payment_method.upper(), 2)
                late = days_diff - normal_t if days_diff > normal_t else None

                if len(available_exact) > 1:
                    exc = "DUPLICATE_SETTLEMENT"
                    for dup in available_exact[1:]:
                        used_settlement_ids.add(dup.settlement_id)
                elif matched_stl.net_amount_paise != normal_net and abs(matched_stl.net_amount_paise - normal_net) > 50:
                    exc = "PARTIAL_REFUND_CONFUSION"
                elif late and late > 0:
                    exc = "LATE_SETTLEMENT"
                else:
                    exc = None

                rows_to_insert.append({
                    "run_id": run_id,
                    "txn_id": txn.txn_id,
                    "settlement_id": matched_stl.settlement_id,
                    "match_type": "MATCHED_EXACT",
                    "match_score": 100.0,
                    "exception_type": exc,
                    "days_late": late if late and late > 0 else None,
                    "amount_diff_paise": 0,
                    "reference_similarity": 100.0,
                })
                exact_matches += 1
            else:
                unmatched_txn_ids.add(txn.txn_id)

        # Pass 2: Fuzzy matching
        txn_map: Dict[str, ExpectedTransaction] = {t.txn_id: t for t in txns}
        best_candidate: Dict[str, Tuple[float, float, float, float, Settlement]] = {}

        for txn_id in list(unmatched_txn_ids):
            txn = txn_map[txn_id]
            norm_ref_txn = txn_norm[txn_id]

            candidates: List[Settlement] = []
            for delta in range(-BLOCKING_WINDOW_DAYS, BLOCKING_WINDOW_DAYS + 1):
                day = txn.transaction_date + timedelta(days=delta)
                for stl in stl_by_date.get(day, []):
                    if stl.settlement_id not in used_settlement_ids:
                        candidates.append(stl)

            seen_in_pass: Set[str] = set()
            unique_candidates: List[Settlement] = []
            for stl in candidates:
                if stl.settlement_id not in seen_in_pass:
                    seen_in_pass.add(stl.settlement_id)
                    unique_candidates.append(stl)

            qualifying: List[Tuple[float, float, float, Settlement]] = []

            for stl in unique_candidates:
                days_diff = abs((txn.transaction_date - stl.settlement_date).days)
                if days_diff > MAX_DATE_DIFF_DAYS:
                    continue
                pct_diff = abs(txn.expected_amount_paise - stl.gross_amount_paise) / max(txn.expected_amount_paise, 1)
                if pct_diff >= MAX_AMOUNT_PCT_DIFF:
                    continue

                composite, ref_score, _, _, days_f = _composite_score(
                    norm_ref_txn,
                    stl_norm[stl.settlement_id],
                    txn.transaction_date,
                    stl.settlement_date,
                    txn.expected_amount_paise,
                    stl.gross_amount_paise,
                )

                if ref_score < 60.0:
                    continue

                if txn_id not in best_candidate or composite > best_candidate[txn_id][0]:
                    best_candidate[txn_id] = (composite, ref_score, days_f, pct_diff, stl)

                if norm_ref_txn == stl_norm[best_stl_id := stl.settlement_id] and txn.expected_amount_paise != stl.gross_amount_paise:
                    continue

                if composite >= FUZZY_REVIEW_THRESHOLD:
                    qualifying.append((composite, ref_score, days_f, stl))

            if len(qualifying) >= 2:
                qualifying.sort(key=lambda x: x[0], reverse=True)
                best_q = qualifying[0]
                rows_to_insert.append({
                    "run_id": run_id,
                    "txn_id": txn_id,
                    "settlement_id": None,
                    "match_type": "UNMATCHED",
                    "match_score": best_q[0],
                    "exception_type": "AMBIGUOUS_MATCH",
                    "days_late": None,
                    "amount_diff_paise": abs(txn.expected_amount_paise - best_q[3].gross_amount_paise),
                    "reference_similarity": best_q[1],
                })
                ambiguous += 1
                unmatched_txn_ids.discard(txn_id)
                continue

            if len(qualifying) == 1:
                composite, ref_score, days_f, stl = qualifying[0]
                used_settlement_ids.add(stl.settlement_id)
                unmatched_txn_ids.discard(txn_id)

                days_signed = (stl.settlement_date - txn.transaction_date).days
                normal_t = NORMAL_T.get(txn.payment_method.upper(), 2)
                late_days = days_signed - normal_t if days_signed > normal_t else None

                if composite >= FUZZY_AUTO_THRESHOLD:
                    match_type = "MATCHED_FUZZY"
                    if late_days and late_days > 0:
                        exception = "LATE_SETTLEMENT"
                    elif ref_score < 99.0:
                        exception = "REFERENCE_MISMATCH"
                    else:
                        exception = None
                    fuzzy_matches += 1
                else:
                    match_type = "NEEDS_REVIEW"
                    if ref_score < 99.0:
                        exception = "REFERENCE_MISMATCH"
                    else:
                        exception = None
                    needs_review += 1

                rows_to_insert.append({
                    "run_id": run_id,
                    "txn_id": txn_id,
                    "settlement_id": stl.settlement_id,
                    "match_type": match_type,
                    "match_score": composite,
                    "exception_type": exception,
                    "days_late": late_days if late_days and late_days > 0 else None,
                    "amount_diff_paise": abs(txn.expected_amount_paise - stl.gross_amount_paise),
                    "reference_similarity": ref_score,
                })
                continue

        # Exception classification for remaining unmatched transactions
        stl_by_norm_ref_all = stl_by_norm_ref
        unmatched_expected_paise: Dict[int, List[str]] = defaultdict(list)
        for tid in unmatched_txn_ids:
            unmatched_expected_paise[txn_map[tid].expected_amount_paise].append(tid)

        partial_refund_confusion_txns: Set[str] = set()
        partial_refund_confusion_stls: Set[str] = set()

        for stl in settlements:
            if stl.settlement_id in used_settlement_ids:
                continue
            net = stl.net_amount_paise
            if net in unmatched_expected_paise:
                for tid in unmatched_expected_paise[net]:
                    partial_refund_confusion_txns.add(tid)
                partial_refund_confusion_stls.add(stl.settlement_id)

        for txn_id in unmatched_txn_ids:
            txn = txn_map[txn_id]
            norm_ref_txn = txn_norm[txn_id]

            if txn_id in partial_refund_confusion_txns:
                rows_to_insert.append({
                    "run_id": run_id,
                    "txn_id": txn_id,
                    "settlement_id": None,
                    "match_type": "UNMATCHED",
                    "match_score": 0.0,
                    "exception_type": "PARTIAL_REFUND_CONFUSION",
                    "days_late": None,
                    "amount_diff_paise": None,
                    "reference_similarity": None,
                })
                continue

            same_ref_stls = [
                s for s in stl_by_norm_ref_all.get(norm_ref_txn, [])
                if s.settlement_id not in used_settlement_ids
            ]
            if len(same_ref_stls) >= 2:
                rows_to_insert.append({
                    "run_id": run_id,
                    "txn_id": txn_id,
                    "settlement_id": same_ref_stls[0].settlement_id,
                    "match_type": "UNMATCHED",
                    "match_score": 0.0,
                    "exception_type": "DUPLICATE_SETTLEMENT",
                    "days_late": None,
                    "amount_diff_paise": abs(txn.expected_amount_paise - same_ref_stls[0].gross_amount_paise),
                    "reference_similarity": 100.0,
                })
                continue

            if txn_id in best_candidate:
                best_comp, ref_score, days_f, pct_diff, best_stl = best_candidate[txn_id]
                amt_diff = abs(txn.expected_amount_paise - best_stl.gross_amount_paise)

                if norm_ref_txn == stl_norm[best_stl.settlement_id] or ref_score >= 85.0:
                    exception_type = "AMOUNT_MISMATCH"
                elif ref_score >= 60.0:
                    exception_type = "REFERENCE_MISMATCH"
                else:
                    exception_type = "MISSING_SETTLEMENT"

                rows_to_insert.append({
                    "run_id": run_id,
                    "txn_id": txn_id,
                    "settlement_id": best_stl.settlement_id if exception_type != "MISSING_SETTLEMENT" else None,
                    "match_type": "UNMATCHED",
                    "match_score": best_comp,
                    "exception_type": exception_type,
                    "days_late": None,
                    "amount_diff_paise": amt_diff if exception_type != "MISSING_SETTLEMENT" else None,
                    "reference_similarity": ref_score,
                })
            else:
                rows_to_insert.append({
                    "run_id": run_id,
                    "txn_id": txn_id,
                    "settlement_id": None,
                    "match_type": "UNMATCHED",
                    "match_score": 0.0,
                    "exception_type": "MISSING_SETTLEMENT",
                    "days_late": None,
                    "amount_diff_paise": None,
                    "reference_similarity": None,
                })

        # Unmatched settlements with no ledger counterpart
        unmatched_settlements = 0
        for stl in settlements:
            if stl.settlement_id in used_settlement_ids:
                continue
            if stl.settlement_id in partial_refund_confusion_stls:
                exception_type = "PARTIAL_REFUND_CONFUSION"
            else:
                exception_type = "UNMATCHED_SETTLEMENT"

            rows_to_insert.append({
                "run_id": run_id,
                "txn_id": None,
                "settlement_id": stl.settlement_id,
                "match_type": "UNMATCHED",
                "match_score": 0.0,
                "exception_type": exception_type,
                "days_late": None,
                "amount_diff_paise": None,
                "reference_similarity": None,
            })
            unmatched_settlements += 1

        for row in rows_to_insert:
            _insert_result(session, row)

    return {
        "exact_matches": exact_matches,
        "fuzzy_matches": fuzzy_matches,
        "needs_review": needs_review,
        "unmatched": len(unmatched_txn_ids),
        "ambiguous": ambiguous,
        "unmatched_settlements": unmatched_settlements,
    }
