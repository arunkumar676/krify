"""Krify's database: one SQLite file (backend/krify.db) on this PC.

Tables:
  task   – every task you gave Krify (text, result, summary, model, tokens)
  step   – every step of every task (actions, notes, safety questions) – useful for debugging
  usage  – tokens and requests per model per day
  limit  – daily limits learned from providers' "limit reached" messages

Switching to PostgreSQL later = changing DATABASE_URL.
"""

import datetime
import os
from pathlib import Path

from sqlmodel import Field, Session, SQLModel, create_engine

DB_FILE = Path(__file__).resolve().parents[1] / "krify.db"
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{DB_FILE}")


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


class Task(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    text: str = Field(index=True)
    client: str = Field(default="", index=True)  # extension install that ran it ("" = before per-person history)
    model: str = ""
    status: str = "running"  # running | done | ask | stopped | error
    summary: str = ""
    tokens: int = 0
    created_at: datetime.datetime = Field(default_factory=_now, index=True)
    finished_at: datetime.datetime | None = None


class Step(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    task_id: int = Field(foreign_key="task.id", index=True)
    kind: str  # step | confirm | done | ask | stopped | error
    text: str
    created_at: datetime.datetime = Field(default_factory=_now)


class Usage(SQLModel, table=True):
    day: str = Field(primary_key=True)  # YYYY-MM-DD (this PC's date)
    model: str = Field(primary_key=True)
    tokens: int = 0
    requests: int = 0


class Limit(SQLModel, table=True):
    __tablename__ = "model_limit"
    model: str = Field(primary_key=True)
    tokens: int | None = None  # tokens per day
    requests: int | None = None  # requests per day


engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {})
SQLModel.metadata.create_all(engine)


def _add_missing_columns():
    """create_all() makes new tables but never adds columns to old ones: add them by hand (SQLite)."""
    if not DATABASE_URL.startswith("sqlite"):
        return
    with engine.begin() as conn:
        columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(task)")}
        if "client" not in columns:
            conn.exec_driver_sql("ALTER TABLE task ADD COLUMN client VARCHAR NOT NULL DEFAULT ''")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_task_client ON task (client)")


_add_missing_columns()


def session():
    return Session(engine)
