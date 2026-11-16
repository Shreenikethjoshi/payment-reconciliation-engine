#!/usr/bin/env python3
"""
inspect_db.py — Quick CLI tool to view reconciliation.db tables and sample data.
Usage:
    python inspect_db.py
    python inspect_db.py <table_name> [limit]
"""

import sys
from pathlib import Path
import sqlite3
import pandas as pd
from tabulate import tabulate

DB_PATH = Path(__file__).resolve().parent / "reconciliation.db"

def main():
    if not DB_PATH.exists():
        print(f"Error: {DB_PATH} does not exist yet. Run 'python run_pipeline.py' first.")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # List all tables and row counts
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;")
    tables = [r[0] for r in cursor.fetchall() if not r[0].startswith("sqlite_")]

    table_arg = sys.argv[1] if len(sys.argv) > 1 else None
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 5

    if not table_arg:
        print("\n========================================================")
        print("📁 RECONCILIATION DATABASE OVERVIEW")
        print("========================================================")
        summary = []
        for t in tables:
            cursor.execute(f"SELECT COUNT(*) FROM {t}")
            cnt = cursor.fetchone()[0]
            summary.append({"Table Name": t, "Total Rows": f"{cnt:,}"})

        print(tabulate(summary, headers="keys", tablefmt="fancy_grid"))
        print("\n💡 Tip: To inspect records in a specific table, run:")
        print("   python inspect_db.py <table_name> [limit]")
        print("   Example: python inspect_db.py reconciliation_results 10\n")
    else:
        if table_arg not in tables:
            print(f"Error: Table '{table_arg}' not found. Available tables: {', '.join(tables)}")
            sys.exit(1)

        print(f"\n📋 Previewing top {limit} rows from '{table_arg}':\n")
        df = pd.read_sql_query(f"SELECT * FROM {table_arg} LIMIT {limit}", conn)
        print(tabulate(df, headers="keys", tablefmt="psql", showindex=False))
        print(f"\n(Showing up to {limit} rows from {table_arg})\n")

    conn.close()

if __name__ == "__main__":
    main()
