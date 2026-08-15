"""Persists every real (non-stub) extraction result and provides the
aggregation query the PDF report is built from.

SQLite via the stdlib — no extra service to run, the whole "database" is one
file. That's the right amount of infrastructure for a report that queries a
few thousand rows at most; the moment this needs concurrent writers across
processes or millions of rows, it's the first thing to swap for Postgres.
"""
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "extractions.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS extractions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    vendor TEXT,
    date TEXT,
    total_amount REAL,
    currency TEXT,
    confidence REAL,
    needs_review INTEGER NOT NULL,
    source TEXT NOT NULL
);
"""


@contextmanager
def get_connection() -> Iterator[sqlite3.Connection]:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with get_connection() as conn:
        conn.execute(SCHEMA)


def insert_extraction(
    vendor: Optional[str],
    date: Optional[str],
    total_amount: Optional[float],
    currency: Optional[str],
    confidence: float,
    needs_review: bool,
    source: str,
) -> None:
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO extractions
               (created_at, vendor, date, total_amount, currency, confidence, needs_review, source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                datetime.now(timezone.utc).isoformat(),
                vendor,
                date,
                total_amount,
                currency,
                confidence,
                1 if needs_review else 0,
                source,
            ),
        )


def query_summary(start_date: Optional[str] = None, end_date: Optional[str] = None) -> dict:
    """The single aggregation query the report renders. Every number in the
    PDF traces back to a row here — no numbers invented in the report layer.
    """
    where = []
    params: list = []
    if start_date:
        where.append("created_at >= ?")
        params.append(start_date)
    if end_date:
        where.append("created_at <= ?")
        params.append(end_date)
    where_clause = f"WHERE {' AND '.join(where)}" if where else ""

    with get_connection() as conn:
        total_row = conn.execute(
            f"""SELECT COUNT(*) AS n,
                       SUM(needs_review) AS needs_review_n,
                       MIN(created_at) AS earliest,
                       MAX(created_at) AS latest
                FROM extractions {where_clause}""",
            params,
        ).fetchone()

        by_currency = conn.execute(
            f"""SELECT COALESCE(currency, 'unknown') AS currency,
                       SUM(total_amount) AS total,
                       COUNT(*) AS n
                FROM extractions {where_clause}
                GROUP BY currency
                ORDER BY total DESC""",
            params,
        ).fetchall()

        by_vendor = conn.execute(
            f"""SELECT COALESCE(vendor, 'unknown') AS vendor,
                       SUM(total_amount) AS total,
                       COUNT(*) AS n
                FROM extractions {where_clause}
                GROUP BY vendor
                ORDER BY total DESC
                LIMIT 10""",
            params,
        ).fetchall()

    total_n = total_row["n"] or 0
    needs_review_n = total_row["needs_review_n"] or 0

    return {
        "total_records": total_n,
        "needs_review_count": needs_review_n,
        "needs_review_rate": (needs_review_n / total_n) if total_n else 0.0,
        "earliest": total_row["earliest"],
        "latest": total_row["latest"],
        "by_currency": [dict(row) for row in by_currency],
        "top_vendors": [dict(row) for row in by_vendor],
    }