"""
database.py — SQLAlchemy ORM models, engine setup, and idempotent init
for the Payment Reconciliation Engine.
"""

from __future__ import annotations

import hashlib
import os
from contextlib import contextmanager
from datetime import datetime, date
from typing import Generator, Optional

from sqlalchemy import (
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import DeclarativeBase, Session, relationship, sessionmaker


class Base(DeclarativeBase):
    pass


class RunLog(Base):
    __tablename__ = "run_log"

    run_id: str = Column(String, primary_key=True)
    started_at: datetime = Column(DateTime, nullable=False, default=datetime.utcnow)
    finished_at: Optional[datetime] = Column(DateTime, nullable=True)
    ledger_file_hash: Optional[str] = Column(String(64), nullable=True)
    settlement_file_hash: Optional[str] = Column(String(64), nullable=True)
    status: str = Column(String(32), nullable=False, default="RUNNING")
    notes: Optional[str] = Column(Text, nullable=True)

    rejected_rows = relationship("RejectedRow", back_populates="run", cascade="all, delete-orphan")
    reconciliation_results = relationship("ReconciliationResult", back_populates="run", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<RunLog run_id={self.run_id!r} status={self.status!r}>"


class ExpectedTransaction(Base):
    __tablename__ = "expected_transactions"

    txn_id: str = Column(String, primary_key=True)
    reference: str = Column(String, nullable=False, index=True)
    customer_id: Optional[str] = Column(String, nullable=True)
    order_id: Optional[str] = Column(String, nullable=True)
    transaction_date: date = Column(Date, nullable=False)
    expected_amount_paise: int = Column(Integer, nullable=False)
    payment_method: str = Column(String(20), nullable=False)
    currency: str = Column(String(3), nullable=False, default="INR")
    created_at: datetime = Column(DateTime, nullable=False, default=datetime.utcnow)

    reconciliation_results = relationship("ReconciliationResult", back_populates="transaction")

    def __repr__(self) -> str:
        return f"<ExpectedTransaction txn_id={self.txn_id!r} amount_paise={self.expected_amount_paise}>"


class Settlement(Base):
    __tablename__ = "settlements"

    settlement_id: str = Column(String, primary_key=True)
    reference: str = Column(String, nullable=False, index=True)
    settlement_date: date = Column(Date, nullable=False)
    gross_amount_paise: int = Column(Integer, nullable=False)
    fee_amount_paise: int = Column(Integer, nullable=False, default=0)
    net_amount_paise: int = Column(Integer, nullable=False)
    provider: str = Column(String(64), nullable=False)
    raw_reference: Optional[str] = Column(Text, nullable=True)
    created_at: datetime = Column(DateTime, nullable=False, default=datetime.utcnow)

    reconciliation_results = relationship("ReconciliationResult", back_populates="settlement")

    def __repr__(self) -> str:
        return f"<Settlement settlement_id={self.settlement_id!r} net_paise={self.net_amount_paise}>"


class RejectedRow(Base):
    __tablename__ = "rejected_rows"

    id: int = Column(Integer, primary_key=True, autoincrement=True)
    run_id: str = Column(String, ForeignKey("run_log.run_id", ondelete="CASCADE"), nullable=False)
    source_file: str = Column(String(512), nullable=False)
    row_number: int = Column(Integer, nullable=False)
    raw_data: Optional[str] = Column(Text, nullable=True)
    rejection_reason: str = Column(Text, nullable=False)
    rejected_at: datetime = Column(DateTime, nullable=False, default=datetime.utcnow)

    run = relationship("RunLog", back_populates="rejected_rows")

    def __repr__(self) -> str:
        return f"<RejectedRow id={self.id} run_id={self.run_id!r} row_number={self.row_number}>"


class ReconciliationResult(Base):
    __tablename__ = "reconciliation_results"
    __table_args__ = (
        UniqueConstraint("run_id", "txn_id", name="uq_run_txn"),
    )

    id: int = Column(Integer, primary_key=True, autoincrement=True)
    run_id: str = Column(String, ForeignKey("run_log.run_id", ondelete="CASCADE"), nullable=False)
    txn_id: Optional[str] = Column(String, ForeignKey("expected_transactions.txn_id"), nullable=True)
    settlement_id: Optional[str] = Column(String, ForeignKey("settlements.settlement_id"), nullable=True)
    match_type: str = Column(String(32), nullable=False)
    match_score: float = Column(Float, nullable=False, default=0.0)
    exception_type: Optional[str] = Column(String(64), nullable=True)
    days_late: Optional[int] = Column(Integer, nullable=True)
    amount_diff_paise: Optional[int] = Column(Integer, nullable=True)
    reference_similarity: Optional[float] = Column(Float, nullable=True)
    created_at: datetime = Column(DateTime, nullable=False, default=datetime.utcnow)

    run = relationship("RunLog", back_populates="reconciliation_results")
    transaction = relationship("ExpectedTransaction", back_populates="reconciliation_results")
    settlement = relationship("Settlement", back_populates="reconciliation_results")
    resolutions = relationship("ExceptionResolution", back_populates="result", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<ReconciliationResult id={self.id} run_id={self.run_id!r} txn_id={self.txn_id!r} match_type={self.match_type!r}>"


class ExceptionResolution(Base):
    __tablename__ = "exception_resolutions"

    id: int = Column(Integer, primary_key=True, autoincrement=True)
    result_id: int = Column(Integer, ForeignKey("reconciliation_results.id", ondelete="CASCADE"), nullable=False)
    resolved_by: str = Column(String(128), nullable=False, default="admin")
    resolution_reason: str = Column(Text, nullable=False)
    resolved_at: datetime = Column(DateTime, nullable=False, default=datetime.utcnow)
    previous_status: str = Column(String(64), nullable=False)
    new_status: str = Column(String(64), nullable=False)

    result = relationship("ReconciliationResult", back_populates="resolutions")

    def __repr__(self) -> str:
        return f"<ExceptionResolution id={self.id} result_id={self.result_id} resolved_by={self.resolved_by!r}>"


def get_engine(db_path: str = "reconciliation.db"):
    """Return a SQLAlchemy engine configured for SQLite with WAL mode."""
    is_memory = db_path in (":memory:", "sqlite:///:memory:") or db_path.endswith(":memory:")
    if is_memory:
        url = "sqlite:///:memory:"
        poolclass = StaticPool
    else:
        abs_path = os.path.abspath(db_path)
        url = f"sqlite:///{abs_path}"
        poolclass = None

    engine = create_engine(
        url,
        echo=False,
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=poolclass if poolclass else None,
    ) if poolclass else create_engine(
        url,
        echo=False,
        future=True,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn, _connection_record):
        cursor = dbapi_conn.cursor()
        if not is_memory:
            cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA foreign_keys=ON;")
        cursor.close()

    return engine


def init_db(engine) -> None:
    """Create tables idempotently if they do not exist."""
    Base.metadata.create_all(bind=engine, checkfirst=True)


@contextmanager
def get_session(engine) -> Generator[Session, None, None]:
    """Context manager yielding a transactional SQLAlchemy session."""
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session: Session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def file_hash(filepath: str, chunk_size: int = 65_536) -> str:
    """Compute the SHA-256 hex digest of a file in streaming chunks."""
    h = hashlib.sha256()
    with open(filepath, "rb") as fh:
        while chunk := fh.read(chunk_size):
            h.update(chunk)
    return h.hexdigest()
