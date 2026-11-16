"""
evaluator.py — Accuracy evaluation and idempotency verification.
"""

from __future__ import annotations

import csv
import os
from typing import Dict, Any

import pandas as pd
from sqlalchemy import text

ERROR_TYPES = [
    "MISSING_SETTLEMENT",
    "DUPLICATE_SETTLEMENT",
    "AMOUNT_MISMATCH",
    "LATE_SETTLEMENT",
    "REFERENCE_MISMATCH",
    "PARTIAL_REFUND_CONFUSION",
]

MATCH_TYPES_CONSIDERED_CORRECT = {"MATCHED_EXACT", "MATCHED_FUZZY"}


def _safe_div(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator else 0.0


def _f1(precision: float, recall: float) -> float:
    denom = precision + recall
    return 2 * precision * recall / denom if denom else 0.0


def compute_metrics(run_id: str, engine, answer_key_path: str = "data/answer_key.csv") -> Dict[str, Any]:
    return evaluate(answer_key_path=answer_key_path, run_id=run_id, engine=engine)


def evaluate(answer_key_path: str, run_id: str, engine) -> Dict[str, Any]:
    """Compare matcher results from the database against the ground-truth answer key."""
    answer_key_df = pd.read_csv(answer_key_path, dtype=str)
    answer_key_df.columns = answer_key_df.columns.str.strip()

    ak_map: Dict[str, str] = {}
    for _, row in answer_key_df.iterrows():
        etype = str(row.get("seeded_error_type", "")).strip()
        if etype:
            ak_map[str(row["txn_id"]).strip()] = etype

    query = text(
        """
        SELECT txn_id, match_type, exception_type
        FROM reconciliation_results
        WHERE run_id = :run_id
        """
    )
    with engine.connect() as conn:
        rows = conn.execute(query, {"run_id": run_id}).fetchall()

    results_map: Dict[str, tuple] = {}
    for row in rows:
        txn_id = str(row[0]).strip() if row[0] else None
        if txn_id:
            results_map[txn_id] = (
                str(row[1]).strip() if row[1] else "",
                str(row[2]).strip() if row[2] else "",
            )

    per_error: Dict[str, Dict[str, Any]] = {}

    for etype in ERROR_TYPES:
        gt_positive_ids = {tid for tid, et in ak_map.items() if et == etype}
        pred_positive_ids = {
            tid for tid, (mt, et) in results_map.items() if et == etype
        }

        tp = len(gt_positive_ids & pred_positive_ids)
        fp = len(pred_positive_ids - gt_positive_ids)
        fn = len(gt_positive_ids - pred_positive_ids)

        precision = _safe_div(tp, tp + fp)
        recall = _safe_div(tp, tp + fn)
        f1 = _f1(precision, recall)

        per_error[etype] = {
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(f1, 4),
        }

    all_seeded_ids = set(ak_map.keys())
    total_false_matches = sum(
        1
        for tid in all_seeded_ids
        if tid in results_map and results_map[tid][0] in MATCH_TYPES_CONSIDERED_CORRECT
    )
    total_missed_exceptions = sum(
        1
        for tid in all_seeded_ids
        if tid not in results_map or results_map[tid][1] == ""
    )

    os.makedirs("reports", exist_ok=True)
    report_path = os.path.join("reports", "evaluation_report.csv")

    with open(report_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=["error_type", "tp", "fp", "fn", "precision", "recall", "f1"],
        )
        writer.writeheader()
        for etype, metrics in per_error.items():
            writer.writerow({"error_type": etype, **metrics})

    return {
        "per_error_type": per_error,
        "total_false_matches": total_false_matches,
        "total_missed_exceptions": total_missed_exceptions,
        "report_path": report_path,
    }


def compare_runs(run_id_1: str, run_id_2: str, engine) -> Dict[str, Any]:
    """Compare two pipeline runs to verify deterministic idempotency."""
    query = text(
        """
        SELECT txn_id, settlement_id, match_type, exception_type,
               days_late, amount_diff_paise
        FROM reconciliation_results
        WHERE run_id = :run_id
        ORDER BY txn_id
        """
    )

    def _load(run_id: str) -> Dict[str, dict]:
        with engine.connect() as conn:
            rows = conn.execute(query, {"run_id": run_id}).fetchall()
        result = {}
        for row in rows:
            txn_id = str(row[0]) if row[0] else None
            if txn_id:
                result[txn_id] = {
                    "settlement_id": row[1],
                    "match_type": row[2],
                    "exception_type": row[3],
                    "days_late": row[4],
                    "amount_diff_paise": row[5],
                }
        return result

    map1 = _load(run_id_1)
    map2 = _load(run_id_2)

    ids1 = set(map1.keys())
    ids2 = set(map2.keys())

    only_in_run1 = sorted(ids1 - ids2)
    only_in_run2 = sorted(ids2 - ids1)
    common_ids = ids1 & ids2

    differing_txns = []
    for tid in sorted(common_ids):
        r1 = map1[tid]
        r2 = map2[tid]
        if r1 != r2:
            differing_txns.append(
                {
                    "txn_id": tid,
                    "run1": r1,
                    "run2": r2,
                }
            )

    are_identical = (
        len(only_in_run1) == 0
        and len(only_in_run2) == 0
        and len(differing_txns) == 0
    )

    return {
        "are_identical": are_identical,
        "idempotent": are_identical,
        "run_id_1_count": len(map1),
        "run_id_2_count": len(map2),
        "differing_txns": differing_txns,
        "only_in_run1": only_in_run1,
        "only_in_run2": only_in_run2,
    }
