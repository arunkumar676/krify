"""Task history: saves each task and its steps in the database."""

import datetime

from sqlmodel import delete, select

from app.db import Step, Task, session

MAX_LISTED = 50


def start_task(text, model):
    with session() as s:
        task = Task(text=text, model=model)
        s.add(task)
        s.commit()
        return task.id


def add_step(task_id, kind, text):
    with session() as s:
        s.add(Step(task_id=task_id, kind=kind, text=text))
        s.commit()


def finish_task(task_id, status, summary, tokens, model=None):
    with session() as s:
        task = s.get(Task, task_id)
        if not task:
            return
        task.status = status
        task.summary = summary[:500]
        task.tokens = tokens
        if model:
            task.model = model
        task.finished_at = datetime.datetime.now(datetime.timezone.utc)
        s.add(task)
        s.commit()


def recent():
    """Latest run of each different task text, newest first (running the same task again moves it up)."""
    with session() as s:
        tasks = s.exec(select(Task).order_by(Task.id.desc()).limit(500)).all()
    seen, items = set(), []
    for t in tasks:
        key = t.text.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        items.append(
            {
                "text": t.text,
                "status": t.status,
                "summary": t.summary,
                "tokens": t.tokens,
                "created_at": t.created_at.replace(tzinfo=datetime.timezone.utc).isoformat(),
            }
        )
        if len(items) >= MAX_LISTED:
            break
    return items


def delete_text(text):
    """Remove every run of this task text (and its steps)."""
    with session() as s:
        ids = [t.id for t in s.exec(select(Task)).all() if t.text.strip().lower() == text.strip().lower()]
        if ids:
            s.exec(delete(Step).where(Step.task_id.in_(ids)))
            s.exec(delete(Task).where(Task.id.in_(ids)))
            s.commit()


def clear():
    with session() as s:
        s.exec(delete(Step))
        s.exec(delete(Task))
        s.commit()
