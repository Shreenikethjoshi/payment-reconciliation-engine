#!/usr/bin/env python3
"""
Payment Reconciliation Engine — Pipeline Entry Point
=====================================================
Usage:
    python run_pipeline.py
    python run_pipeline.py --date 2026-11-16
    python run_pipeline.py --date 2026-11-16 --db reconciliation.db --reset

Options:
    --date  YYYY-MM-DD  Run date (default: today)
    --db    PATH        SQLite database path (default: reconciliation.db)
    --reset             Delete DB and regenerate data before running
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
from datetime import datetime, date
from pathlib import Path
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from rich.console import Console
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeElapsedColumn
from rich.panel import Panel
from rich import box

console = Console()

DATA_DIR = PROJECT_ROOT / "data"
LEDGER_CSV = DATA_DIR / "expected_ledger.csv"
SETTLEMENT_CSV = DATA_DIR / "settlement_report.csv"
ANSWER_KEY_CSV = DATA_DIR / "answer_key.csv"
LOGS_DIR = PROJECT_ROOT / "logs"
REPORTS_DIR = PROJECT_ROOT / "reports"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Payment Reconciliation Engine — Pipeline Entry Point",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        "--date",
        default=date.today().strftime("%Y-%m-%d"),
        help="Run date in YYYY-MM-DD format (default: today)",
    )
    parser.add_argument(
        "--db",
        default="reconciliation.db",
        help="Path to SQLite database (default: reconciliation.db)",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Delete DB and regenerate synthetic data before running",
    )
    return parser.parse_args()


def run_generator() -> None:
    console.print("[bold cyan]Running data generator…[/bold cyan]")
    from src.generator import main as gen_main
    gen_main()


from src.evaluator import compute_metrics, compare_runs
from src.reporting import generate_daily_report


def _file_sha256(path: Path, chunk: int = 65_536) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while blk := fh.read(chunk):
            h.update(blk)
    return h.hexdigest()


def run_pipeline(
    date_str: str,
    db_path: str,
    *,
    console: Console,
) -> dict:
    """
    Execute one full pipeline pass and return a stats dict.

    Returns
    -------
    dict with keys:
        run_id, ledger_rows, settlement_rows, rejected_rows,
        exact_matches, fuzzy_matches, needs_review, unmatched,
        money_at_risk_paise, report_path, result_hash
    """
    from src.database import get_engine, init_db, get_session, file_hash as db_file_hash
    from src.database import RunLog, ReconciliationResult
    from src.ingestion import run_ingestion
    from src.matcher import run_matching
    from sqlalchemy import text, select, func

    engine = get_engine(db_path)
    init_db(engine)

    # -- run_id --
    run_id = f"RUN-{date_str.replace('-', '')}-{uuid4().hex[:6].upper()}"

    # -- file hashes --
    ledger_hash = db_file_hash(str(LEDGER_CSV))
    settlement_hash = db_file_hash(str(SETTLEMENT_CSV))

    steps = [
        "Inserting run log entry",
        "Ingesting ledger",
        "Ingesting settlements",
        "Running matcher",
        "Generating report",
        "Computing evaluation",
        "Finalising run",
    ]

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task(f"[cyan]Pipeline {run_id}", total=len(steps))

        # Step 1: Insert run_log
        progress.update(task, description=f"[cyan]{steps[0]}")
        with get_session(engine) as session:
            run_log = RunLog(
                run_id=run_id,
                started_at=datetime.utcnow(),
                ledger_file_hash=ledger_hash,
                settlement_file_hash=settlement_hash,
                status="RUNNING",
            )
            session.add(run_log)
        progress.advance(task)

        # Step 2–3: Ingestion
        progress.update(task, description=f"[cyan]{steps[1]}")
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        ingestion_stats = run_ingestion(
            ledger_path=str(LEDGER_CSV),
            settlement_path=str(SETTLEMENT_CSV),
            run_id=run_id,
            engine=engine,
            log_dir=str(LOGS_DIR),
        )
        progress.advance(task)
        progress.advance(task)  # settle step counted separately above

        # Step 4: Matcher
        progress.update(task, description=f"[cyan]{steps[3]}")
        match_stats = run_matching(run_id, engine)
        progress.advance(task)

        # Step 5: Reporting
        progress.update(task, description=f"[cyan]{steps[4]}")
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        report_path = generate_daily_report(
            run_id=run_id,
            engine=engine,
            output_dir=str(REPORTS_DIR),
            date_str=date_str,
        )
        progress.advance(task)

        # Step 6: Evaluation
        progress.update(task, description=f"[cyan]{steps[5]}")
        eval_stats = compute_metrics(run_id=run_id, engine=engine)
        progress.advance(task)

        # Step 7: Finalise run_log
        progress.update(task, description=f"[cyan]{steps[6]}")
        with get_session(engine) as session:
            rl = session.get(RunLog, run_id)
            if rl:
                rl.status = "COMPLETED"
                rl.finished_at = datetime.utcnow()
        progress.advance(task)

    # -- Gather DB counts for summary table --
    with engine.connect() as conn:
        ledger_rows = conn.execute(
            text("SELECT COUNT(*) FROM expected_transactions")
        ).scalar() or 0
        settlement_rows = conn.execute(
            text("SELECT COUNT(*) FROM settlements")
        ).scalar() or 0
        rejected_rows = conn.execute(
            text("SELECT COUNT(*) FROM rejected_rows WHERE run_id = :rid"),
            {"rid": run_id},
        ).scalar() or 0

        exact = conn.execute(
            text(
                "SELECT COUNT(*) FROM reconciliation_results "
                "WHERE run_id=:rid AND match_type='MATCHED_EXACT'"
            ),
            {"rid": run_id},
        ).scalar() or 0
        fuzzy = conn.execute(
            text(
                "SELECT COUNT(*) FROM reconciliation_results "
                "WHERE run_id=:rid AND match_type='MATCHED_FUZZY'"
            ),
            {"rid": run_id},
        ).scalar() or 0
        needs_review = conn.execute(
            text(
                "SELECT COUNT(*) FROM reconciliation_results "
                "WHERE run_id=:rid AND match_type='NEEDS_REVIEW'"
            ),
            {"rid": run_id},
        ).scalar() or 0
        unmatched = conn.execute(
            text(
                "SELECT COUNT(*) FROM reconciliation_results "
                "WHERE run_id=:rid AND match_type='UNMATCHED'"
            ),
            {"rid": run_id},
        ).scalar() or 0

        # Money at risk = sum of expected amounts for unmatched txns
        money_at_risk = conn.execute(
            text(
                """
                SELECT COALESCE(SUM(et.expected_amount_paise), 0)
                FROM reconciliation_results rr
                JOIN expected_transactions et ON et.txn_id = rr.txn_id
                WHERE rr.run_id = :rid
                  AND rr.match_type IN ('UNMATCHED', 'NEEDS_REVIEW')
                """
            ),
            {"rid": run_id},
        ).scalar() or 0

    # -- Result hash (for idempotency comparison) --
    result_hash = _compute_result_hash(engine, run_id)

    return {
        "run_id": run_id,
        "ledger_rows": ledger_rows,
        "settlement_rows": settlement_rows,
        "rejected_rows": rejected_rows,
        "exact_matches": exact,
        "fuzzy_matches": fuzzy,
        "needs_review": needs_review,
        "unmatched": unmatched,
        "money_at_risk_paise": money_at_risk,
        "report_path": (report_path.get("html_path") if isinstance(report_path, dict) else report_path) or "(not generated)",
        "result_hash": result_hash,
        **match_stats,
        **eval_stats,
    }


def _compute_result_hash(engine, run_id: str) -> str:
    """Hash the reconciliation results for deterministic comparison across runs."""
    from sqlalchemy import text

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT txn_id, match_type, COALESCE(exception_type, '')
                FROM reconciliation_results
                WHERE run_id = :rid
                  AND txn_id IS NOT NULL
                ORDER BY txn_id
                """
            ),
            {"rid": run_id},
        ).fetchall()

    h = hashlib.sha256()
    for row in rows:
        h.update("|".join(str(x) for x in row).encode())
    return h.hexdigest()


def _fmt_inr(paise: int) -> str:
    inr = paise / 100
    return f"₹{inr:,.2f}"


def print_pipeline_summary(
    stats: dict,
    date_str: str,
    run_number: int = 1,
) -> None:
    title = f"[bold green]Run #{run_number} Summary[/bold green]"
    table = Table(
        title=title,
        box=box.ROUNDED,
        border_style="green",
        show_header=True,
        header_style="bold magenta",
    )
    table.add_column("Metric", style="cyan", no_wrap=True)
    table.add_column("Value", justify="right", style="white")

    table.add_row("Run ID", f"[bold]{stats['run_id']}[/bold]")
    table.add_row("Date", date_str)
    table.add_row("", "")

    table.add_row("Ledger Rows", f"{stats['ledger_rows']:,}")
    table.add_row("Settlement Rows", f"{stats['settlement_rows']:,}")
    table.add_row("Rejected Rows", f"[red]{stats['rejected_rows']:,}[/red]")
    table.add_row("", "")

    table.add_row("Exact Matches", f"[green]{stats['exact_matches']:,}[/green]")
    table.add_row("Fuzzy Matches", f"[yellow]{stats['fuzzy_matches']:,}[/yellow]")
    table.add_row("Needs Review", f"[magenta]{stats['needs_review']:,}[/magenta]")
    table.add_row("Unmatched", f"[red]{stats['unmatched']:,}[/red]")
    table.add_row("", "")

    table.add_row(
        "Money at Risk",
        f"[bold red]{_fmt_inr(stats['money_at_risk_paise'])}[/bold red]",
    )
    table.add_row("Result Hash", stats["result_hash"][:16] + "…")
    table.add_row("Report saved to", str(stats["report_path"]))

    console.print(table)


def print_idempotency_table(stats1: dict, stats2: dict) -> None:
    identical = stats1["result_hash"] == stats2["result_hash"]
    verdict = "IDENTICAL" if identical else "MISMATCH"
    verdict_style = "bold green" if identical else "bold red"

    table = Table(
        title="[bold]Idempotency Check[/bold]",
        box=box.DOUBLE_EDGE,
        border_style="blue",
        header_style="bold blue",
    )
    table.add_column("", style="cyan")
    table.add_column("Run 1", justify="center")
    table.add_column("Run 2", justify="center")
    table.add_column("Status", justify="center")

    h1 = stats1["result_hash"]
    h2 = stats2["result_hash"]

    table.add_row(
        "Result Hash",
        h1[:16] + "…",
        h2[:16] + "…",
        f"[{verdict_style}]{verdict}[/{verdict_style}]",
    )
    table.add_row(
        "Exact Matches",
        str(stats1["exact_matches"]),
        str(stats2["exact_matches"]),
        "[green]MATCH[/green]" if stats1["exact_matches"] == stats2["exact_matches"] else "[red]DIFF[/red]",
    )
    table.add_row(
        "Unmatched",
        str(stats1["unmatched"]),
        str(stats2["unmatched"]),
        "[green]MATCH[/green]" if stats1["unmatched"] == stats2["unmatched"] else "[red]DIFF[/red]",
    )
    table.add_row(
        "Needs Review",
        str(stats1["needs_review"]),
        str(stats2["needs_review"]),
        "[green]MATCH[/green]" if stats1["needs_review"] == stats2["needs_review"] else "[red]DIFF[/red]",
    )

    console.print(table)
    if identical:
        console.print(Panel(
            "[bold green]Both runs produced identical reconciliation results.[/bold green]\n"
            "The pipeline is fully idempotent — safe to rerun without duplicating data.",
            border_style="green",
        ))
    else:
        console.print(Panel(
            "[bold red]The two runs produced different results.[/bold red]\n"
            "Investigate the reconciliation_results table for discrepancies.",
            border_style="red",
        ))


def main() -> None:
    args = parse_args()
    date_str: str = args.date
    db_path: str = args.db

    if not os.path.isabs(db_path):
        db_path = str(PROJECT_ROOT / db_path)

    console.print(Panel(
        f"[bold cyan]Payment Reconciliation Engine[/bold cyan]\n"
        f"Date: [yellow]{date_str}[/yellow]  |  DB: [yellow]{db_path}[/yellow]",
        border_style="cyan",
    ))

    if args.reset:
        if os.path.exists(db_path):
            console.print(f"[yellow]--reset: removing {db_path}[/yellow]")
            os.remove(db_path)
        for f in [LEDGER_CSV, SETTLEMENT_CSV, ANSWER_KEY_CSV]:
            if f.exists():
                f.unlink()
        console.print("[yellow]Data files removed.[/yellow]")

    if not LEDGER_CSV.exists() or not SETTLEMENT_CSV.exists():
        run_generator()

    console.print("\n[bold]Run 1[/bold]")
    stats1 = run_pipeline(date_str=date_str, db_path=db_path, console=console)
    print_pipeline_summary(stats1, date_str, run_number=1)

    console.print("\n[bold yellow]Second run test (idempotency verification)[/bold yellow]")
    console.print("[dim]Running the pipeline a second time with identical data.[/dim]\n")
    stats2 = run_pipeline(date_str=date_str, db_path=db_path, console=console)
    print_pipeline_summary(stats2, date_str, run_number=2)

    console.print()
    print_idempotency_table(stats1, stats2)

    # -- Evaluator compare_runs --
    try:
        from src.database import get_engine
        engine = get_engine(db_path)
        comparison = compare_runs(stats1["run_id"], stats2["run_id"], engine)
        if comparison.get("idempotent"):
            console.print("[dim]Evaluator confirmed exact match of status distributions across both runs.[/dim]")
    except Exception as exc:
        console.print(f"[yellow]compare_runs note: {exc}[/yellow]")

    console.print("\n[bold green]Pipeline run complete.[/bold green]")


if __name__ == "__main__":
    main()
