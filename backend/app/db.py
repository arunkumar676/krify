"""Krify's database: one SQLite file (backend/krify.db) on this PC.

Tables:
  task   – every task you gave Krify (text, result, summary, model, tokens)
  step   – every step of every task (actions, notes, safety questions) – useful for debugging
  llmcall – every request sent to an AI model (what was sent, what came back, tokens) – the LLM report
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
    prompt_tokens: int = 0  # of the tokens above: what was sent to the models
    completion_tokens: int = 0  # ... and what they sent back
    calls: int = 0  # requests made to AI models (retries and model switches count)
    created_at: datetime.datetime = Field(default_factory=_now, index=True)
    finished_at: datetime.datetime | None = None


class Step(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    task_id: int = Field(foreign_key="task.id", index=True)
    kind: str  # step | confirm | done | ask | stopped | error
    text: str
    created_at: datetime.datetime = Field(default_factory=_now)


class LlmCall(SQLModel, table=True):
    """One request to an AI model (a retry or a switch to another model is its own row)."""

    id: int | None = Field(default=None, primary_key=True)
    task_id: int | None = Field(default=None, foreign_key="task.id", index=True)
    seq: int = 0  # 1.. within the task
    step: int | None = None  # agent loop step (None = the planner)
    purpose: str = "agent_step"  # planner | agent_step
    created_at: datetime.datetime = Field(default_factory=_now)
    duration_ms: int = 0
    model: str = ""
    provider: str = ""
    attempt: int = 0  # in-place retry number on this model (0-2)
    boost: bool = False
    need_vision: bool = False
    status: str = "ok"  # ok | rate_limit | broken_reply | rejected | unavailable | error
    error: str = ""
    prompt_tokens: int | None = None  # None = the provider didn't say
    completion_tokens: int | None = None
    total_tokens: int | None = None
    reasoning_tokens: int | None = None
    message_count: int = 0
    request_chars: int = 0
    request_json: str = ""  # messages + tools + extra settings, screenshots redacted
    response_json: str = ""  # text, tool calls, finish reason


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
        for name in ("prompt_tokens", "completion_tokens", "calls"):
            if name not in columns:
                conn.exec_driver_sql(f"ALTER TABLE task ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0")


_add_missing_columns()


def session():
    return Session(engine)
