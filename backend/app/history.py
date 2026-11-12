"""Task history: saves each task and its steps in the database.

History is kept per extension install (`client`), so people sharing one server over the network
each see and clear only their own tasks."""

import datetime

from sqlalchemy import update
from sqlmodel import delete, select

from app.db import LlmCall, Step, Task, session

MAX_LISTED = 50


def start_task(text, model, client=""):
    with session() as s:
        task = Task(text=text, model=model, client=client)
        s.add(task)
        s.commit()
        return task.id


def add_step(task_id, kind, text):
    with session() as s:
        s.add(Step(task_id=task_id, kind=kind, text=text))
        s.commit()


def finish_task(task_id, status, summary, tokens, model=None, calls=0, prompt_tokens=0, completion_tokens=0):
    with session() as s:
        task = s.get(Task, task_id)
        if not task:
            return
        task.status = status
        task.summary = summary[:500]
        task.tokens = tokens
        task.calls, task.prompt_tokens, task.completion_tokens = calls, prompt_tokens, completion_tokens
        if model:
            task.model = model
        task.finished_at = datetime.datetime.now(datetime.timezone.utc)
        s.add(task)
        s.commit()


def claim_legacy(client):
    """Tasks saved before history was per person belong to whoever uses this PC itself."""
    with session() as s:
        s.exec(update(Task).where(Task.client == "").values(client=client))
        s.commit()


def recent(client):
    """Latest run of each different task text, newest first (running the same task again moves it up)."""
    with session() as s:
        tasks = s.exec(select(Task).where(Task.client == client).order_by(Task.id.desc()).limit(500)).all()
    seen, items = set(), []
    for t in tasks:
        key = t.text.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        items.append(
            {
                "id": t.id,
                "text": t.text,
                "status": t.status,
                "summary": t.summary,
                "tokens": t.tokens,
                "calls": t.calls,
                "prompt_tokens": t.prompt_tokens,
                "completion_tokens": t.completion_tokens,
                "created_at": t.created_at.replace(tzinfo=datetime.timezone.utc).isoformat(),
            }
        )
        if len(items) >= MAX_LISTED:
            break
    return items


def _delete_tasks(s, ids):
    if ids:
        s.exec(delete(Step).where(Step.task_id.in_(ids)))
        s.exec(delete(LlmCall).where(LlmCall.task_id.in_(ids)))
        s.exec(delete(Task).where(Task.id.in_(ids)))
        s.commit()


def delete_text(text, client):
    """Remove every run of this task text (and its steps) – this client's only."""
    with session() as s:
        tasks = s.exec(select(Task).where(Task.client == client)).all()
        _delete_tasks(s, [t.id for t in tasks if t.text.strip().lower() == text.strip().lower()])


def clear(client):
    """Remove all of this client's tasks (and their steps)."""
    with session() as s:
        _delete_tasks(s, list(s.exec(select(Task.id).where(Task.client == client)).all()))
