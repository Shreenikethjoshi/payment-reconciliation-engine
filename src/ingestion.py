"""
ingestion.py — Queue-based CSV ingestion for the Payment Reconciliation Engine.
Validates and normalizes ledger and settlement rows, batch-inserts valid rows into SQLite,
and writes rejected rows to a structured JSONL audit log.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
from datetime import datetime, date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
from dateutil import parser as dateutil_parser
from sqlalchemy import insert, select, text

from src.database import ExpectedTransaction, Settlement, RejectedRow, get_session

logger = logging.getLogger(__name__)

_SENTINEL = object()
_BATCH_SIZE = 500
_MAX_AMOUNT_PAISE = 10_000_000
_MIN_AMOUNT_PAISE = 1
_VALID_YEAR = 2026

_LEDGER_REQUIRED = [
    "txn_id", "reference", "transaction_date", "expected_amount_paise", "payment_method",
]
_SETTLEMENT_REQUIRED = [
    "settlement_id", "reference", "settlement_date",
    "gross_amount_paise", "net_amount_paise",
]


def _get_reject_logger(log_dir: str) -> logging.Logger:
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    log_path = os.path.join(log_dir, "rejected_rows.jsonl")
    rl = logging.getLogger(f"rejected_rows.{log_dir}")
    if not rl.handlers:
        handler = logging.FileHandler(log_path, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(message)s"))
        rl.addHandler(handler)
        rl.setLevel(logging.DEBUG)
        rl.propagate = False
    return rl


def _log_reject(
    reject_logger: logging.Logger,
    *,
    run_id: str,
    source: str,
    row_num: int,
    reason: str,
    raw: Any,
) -> None:
    entry = {
        "ts": datetime.utcnow().isoformat() + "Z",
        "run_id": run_id,
        "source": source,
        "row": row_num,
        "reason": reason,
        "raw": str(raw),
    }
    reject_logger.info(json.dumps(entry, ensure_ascii=False))


def _normalise_reference(ref: Any) -> str:
    if ref is None or (isinstance(ref, float) and pd.isna(ref)):
        return ""
    return "".join(str(ref).split()).upper()


def _parse_date(value: Any) -> Optional[date]:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, (date, datetime)):
        return value.date() if isinstance(value, datetime) else value

    s = str(value).strip()
    if not s:
        return None

    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass

    try:
        return dateutil_parser.parse(s, dayfirst=True).date()
    except (ValueError, OverflowError):
        return None


def _to_paise(value: Any) -> Optional[int]:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    try:
        f = float(value)
    except (ValueError, TypeError):
        return None

    if f != int(f):
        return int(round(f * 100))

    i = int(f)
    if i >= 100:
        return i
    return int(round(f * 100))


def _validate_amount(paise: Optional[int]) -> Optional[str]:
    if paise is None:
        return "invalid_amount"
    if paise <= 0:
        return "amount_not_positive"
    if paise >= _MAX_AMOUNT_PAISE:
        return "amount_too_large"
    return None


def _validate_date(d: Optional[date]) -> Optional[str]:
    if d is None:
        return "invalid_date"
    if d.year != _VALID_YEAR:
        return "date_out_of_range"
    return None


def _load_existing_txn_ids(engine) -> set:
    with engine.connect() as conn:
        rows = conn.execute(select(text("txn_id")).select_from(
            ExpectedTransaction.__table__
        )).fetchall()
    return {r[0] for r in rows}


def _load_existing_settlement_ids(engine) -> set:
    with engine.connect() as conn:
        rows = conn.execute(select(text("settlement_id")).select_from(
            Settlement.__table__
        )).fetchall()
    return {r[0] for r in rows}


def _validate_ledger_row(
    raw_row: Dict[str, Any],
    row_num: int,
    existing_ids: set,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    for field in _LEDGER_REQUIRED:
        v = raw_row.get(field)
        if v is None or (isinstance(v, float) and pd.isna(v)) or str(v).strip() == "":
            return None, f"missing_required_field:{field}"

    txn_id = str(raw_row["txn_id"]).strip()
    if txn_id in existing_ids:
        return None, "duplicate_id"

    reference = _normalise_reference(raw_row.get("reference"))
    if not reference:
        return None, "missing_required_field:reference"

    parsed_date = _parse_date(raw_row.get("transaction_date"))
    date_err = _validate_date(parsed_date)
    if date_err:
        return None, date_err

    paise = _to_paise(raw_row.get("expected_amount_paise"))
    amt_err = _validate_amount(paise)
    if amt_err:
        return None, amt_err

    payment_method = str(raw_row.get("payment_method", "")).strip().upper()
    if not payment_method:
        return None, "missing_required_field:payment_method"

    normalised = {
        "txn_id": txn_id,
        "reference": reference,
        "customer_id": str(raw_row["customer_id"]).strip() if raw_row.get("customer_id") else None,
        "order_id": str(raw_row["order_id"]).strip() if raw_row.get("order_id") else None,
        "transaction_date": parsed_date,
        "expected_amount_paise": paise,
        "payment_method": payment_method,
        "currency": str(raw_row.get("currency", "INR")).strip().upper() or "INR",
        "created_at": datetime.utcnow(),
    }

    existing_ids.add(txn_id)
    return normalised, None


def _validate_settlement_row(
    raw_row: Dict[str, Any],
    row_num: int,
    existing_ids: set,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    for field in _SETTLEMENT_REQUIRED:
        v = raw_row.get(field)
        if v is None or (isinstance(v, float) and pd.isna(v)) or str(v).strip() == "":
            return None, f"missing_required_field:{field}"

    settlement_id = str(raw_row["settlement_id"]).strip()
    if settlement_id in existing_ids:
        return None, "duplicate_id"

    raw_reference = raw_row.get("reference") or raw_row.get("raw_reference")
    reference = _normalise_reference(raw_reference)
    if not reference:
        return None, "missing_required_field:reference"

    parsed_date = _parse_date(raw_row.get("settlement_date"))
    date_err = _validate_date(parsed_date)
    if date_err:
        return None, date_err

    gross = _to_paise(raw_row.get("gross_amount_paise"))
    gross_err = _validate_amount(gross)
    if gross_err:
        return None, f"gross_{gross_err}"

    net = _to_paise(raw_row.get("net_amount_paise"))
    net_err = _validate_amount(net)
    if net_err:
        return None, f"net_{net_err}"

    fee_raw = raw_row.get("fee_amount_paise", 0)
    fee = _to_paise(fee_raw) if fee_raw not in (None, "", "nan") else 0
    if fee is None:
        fee = 0

    provider = str(raw_row.get("provider", "Razorpay")).strip() or "Razorpay"

    normalised = {
        "settlement_id": settlement_id,
        "reference": reference,
        "settlement_date": parsed_date,
        "gross_amount_paise": gross,
        "fee_amount_paise": fee,
        "net_amount_paise": net,
        "provider": provider,
        "raw_reference": str(raw_row.get("raw_reference") or raw_reference),
        "created_at": datetime.utcnow(),
    }

    existing_ids.add(settlement_id)
    return normalised, None


def _run_ingestion_queue(
    csv_path: str,
    run_id: str,
    engine,
    log_dir: str,
    source: str,
    validator,
    table,
    id_loader,
) -> dict:
    if not os.path.exists(csv_path):
        raise FileNotFoundError(f"CSV file not found: {csv_path}")

    reject_logger = _get_reject_logger(log_dir)
    existing_ids: set = id_loader(engine)
    q: queue.Queue = queue.Queue(maxsize=2000)

    stats = {
        "accepted": 0,
        "rejected": 0,
        "rejected_reasons": {},
        "consumer_error": None,
    }

    def consumer():
        batch: List[dict] = []
        reject_batch: List[dict] = []

        def flush_batches():
            if not batch and not reject_batch:
                return

            with engine.begin() as conn:
                if batch:
                    stmt = insert(table).prefix_with("OR IGNORE")
                    conn.execute(stmt, batch)
                    batch.clear()

                if reject_batch:
                    stmt = insert(RejectedRow.__table__)
                    conn.execute(stmt, reject_batch)
                    reject_batch.clear()

        while True:
            item = q.get()
            if item is _SENTINEL:
                q.task_done()
                break

            row_num, raw_row = item
            normalised, reason = validator(raw_row, row_num, existing_ids)

            if normalised is not None:
                batch.append(normalised)
                stats["accepted"] += 1
            else:
                stats["rejected"] += 1
                stats["rejected_reasons"][reason] = (
                    stats["rejected_reasons"].get(reason, 0) + 1
                )
                _log_reject(
                    reject_logger,
                    run_id=run_id,
                    source=source,
                    row_num=row_num,
                    reason=reason,
                    raw=raw_row,
                )
                reject_batch.append({
                    "run_id": run_id,
                    "source_file": os.path.basename(csv_path),
                    "row_number": row_num,
                    "raw_data": json.dumps(raw_row, ensure_ascii=False, default=str),
                    "rejection_reason": reason,
                    "rejected_at": datetime.utcnow(),
                })

            if len(batch) >= _BATCH_SIZE or len(reject_batch) >= _BATCH_SIZE:
                try:
                    flush_batches()
                except Exception as exc:
                    logger.error("Error flushing batches: %s", exc)
                    stats["consumer_error"] = str(exc)

            q.task_done()

        try:
            flush_batches()
        except Exception as exc:
            logger.error("Error in final batch flush: %s", exc)
            stats["consumer_error"] = str(exc)

    consumer_thread = threading.Thread(target=consumer, daemon=True, name=f"{source}-consumer")
    consumer_thread.start()

    try:
        chunk_iter = pd.read_csv(csv_path, chunksize=1000, dtype=str, keep_default_na=False)
        row_num = 1
        for chunk in chunk_iter:
            chunk = chunk.replace("", float("nan"))
            for _, raw_row in chunk.iterrows():
                q.put((row_num, raw_row.to_dict()))
                row_num += 1
    except Exception as exc:
        logger.error("Error reading CSV %s: %s", csv_path, exc)
        stats["consumer_error"] = str(exc)
    finally:
        q.put(_SENTINEL)

    consumer_thread.join()

    result = {
        "accepted": stats["accepted"],
        "rejected": stats["rejected"],
        "rejected_reasons": stats["rejected_reasons"],
    }
    if stats["consumer_error"]:
        result["consumer_error"] = stats["consumer_error"]

    return result


def ingest_ledger(csv_path: str, run_id: str, engine, log_dir: str = "logs") -> dict:
    return _run_ingestion_queue(
        csv_path=csv_path,
        run_id=run_id,
        engine=engine,
        log_dir=log_dir,
        source="ledger",
        validator=_validate_ledger_row,
        table=ExpectedTransaction.__table__,
        id_loader=_load_existing_txn_ids,
    )


def ingest_settlements(csv_path: str, run_id: str, engine, log_dir: str = "logs") -> dict:
    return _run_ingestion_queue(
        csv_path=csv_path,
        run_id=run_id,
        engine=engine,
        log_dir=log_dir,
        source="settlement",
        validator=_validate_settlement_row,
        table=Settlement.__table__,
        id_loader=_load_existing_settlement_ids,
    )


def run_ingestion(
    ledger_path: str,
    settlement_path: str,
    run_id: str,
    engine,
    log_dir: str = "logs",
) -> dict:
    ledger_stats = ingest_ledger(ledger_path, run_id, engine, log_dir)
    settlement_stats = ingest_settlements(settlement_path, run_id, engine, log_dir)

    return {
        "ledger": ledger_stats,
        "settlements": settlement_stats,
        "total_accepted": ledger_stats["accepted"] + settlement_stats["accepted"],
        "total_rejected": ledger_stats["rejected"] + settlement_stats["rejected"],
    }
