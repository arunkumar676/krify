"""The LLM report: every request Krify sends to an AI model, what it sent, what came back, the tokens.

`client._try_model` calls `record_call` for each real request (a retry or a switch to another model
is its own row). Who is asking (task, agent step) comes from a ContextVar set by the agent loop, so
`ask_ai`'s signature stays as it is. `task_report` / `task_report_markdown` read a task's calls back
for the side panel.
"""

import contextvars
import datetime
import json
from collections import defaultdict

from sqlalchemy import delete, update
from sqlmodel import func, select

from app.db import LlmCall, Task, session

KEEP_PAYLOADS = 50  # newest tasks whose full request/response text is kept (numbers are kept always)

_ctx = contextvars.ContextVar("llm_report_ctx", default={"task_id": None, "step": None})


def set_task(task_id):
    """The calls made from here on (in this asyncio task) belong to this task."""
    _ctx.set({"task_id": task_id, "step": None})


def set_step(step):
    _ctx.set({**_ctx.get(), "step": step})


def set_purpose(purpose):
    """Label the calls made from here on ("router" for the gate); None = the default (planner / agent_step)."""
    _ctx.set({**_ctx.get(), "purpose": purpose})


def _where(purpose, step):
    return purpose if purpose in ("planner", "router") else f"step {step}"


def redact(messages):
    """Copy of the chat for storing: screenshots (base64 data) become '[image ~N KB]'."""
    out = []
    for msg in messages:
        msg = dict(msg)
        content = msg.get("content")
        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    url = (part.get("image_url") or {}).get("url", "")
                    part = {"type": "image_url", "image_url": {"url": f"[image ~{len(url) // 1024} KB]"}}
                parts.append(part)
            msg["content"] = parts
        out.append(msg)
    return out


def _chars(value):
    return len(json.dumps(value, ensure_ascii=False, default=str))


def record_call(model, provider, attempt, messages, tools, extra, boost, duration_ms,
                response=None, status="ok", error=""):
    """Save one request. `response` is the provider's reply object (None when the request failed)."""
    ctx = _ctx.get()
    request = {
        "messages": redact(messages),
        "tools": [t["function"]["name"] for t in tools] if tools else [],
        "tools_chars": _chars(tools) if tools else 0,
        "settings": extra,
    }
    row = LlmCall(
        task_id=ctx["task_id"], step=ctx["step"], purpose=ctx.get("purpose") or ("agent_step" if tools else "planner"),
        duration_ms=duration_ms, model=model, provider=provider, attempt=attempt, boost=bool(boost),
        status=status, error=str(error)[:500],
        message_count=len(messages), request_chars=_chars(request["messages"]) + request["tools_chars"],
        request_json=json.dumps(request, ensure_ascii=False, default=str),
    )
    if response is not None:
        u = response.usage
        if u:
            row.prompt_tokens, row.completion_tokens, row.total_tokens = u.prompt_tokens, u.completion_tokens, u.total_tokens
            details = getattr(u, "completion_tokens_details", None)
            row.reasoning_tokens = getattr(details, "reasoning_tokens", None)
        choice = response.choices[0]
        row.response_json = json.dumps(
            {
                "content": choice.message.content,
                "tool_calls": [c.model_dump(exclude_none=True) for c in (choice.message.tool_calls or [])],
                "finish_reason": choice.finish_reason,
            },
            ensure_ascii=False, default=str,
        )
    with session() as s:
        if row.task_id:
            row.seq = (s.exec(select(func.count()).where(LlmCall.task_id == row.task_id)).one() or 0) + 1
        s.add(row)
        s.commit()


def totals(task_id):
    """(calls, prompt_tokens, completion_tokens) of a task, for its history row."""
    with session() as s:
        calls = s.exec(select(LlmCall).where(LlmCall.task_id == task_id)).all()
    return len(calls), sum(c.prompt_tokens or 0 for c in calls), sum(c.completion_tokens or 0 for c in calls)


def prune(keep=KEEP_PAYLOADS):
    """Drop the request/response text of all but the newest `keep` tasks; the numbers stay."""
    with session() as s:
        old = s.exec(select(Task.id).order_by(Task.id.desc()).offset(keep)).all()
        if old:
            s.exec(update(LlmCall).where(LlmCall.task_id.in_(old), LlmCall.request_json != "")
                   .values(request_json="", response_json=""))
            s.commit()


def clear(client, keep_task_id=None):
    """Delete this client's recorded LLM calls (the 📊 Report data). Tasks and their History rows stay,
    and so do their saved totals; the task running right now (`keep_task_id`) keeps its calls, because
    its History row is filled from them when it ends. Returns how many calls were deleted."""
    with session() as s:
        ids = [i for i in s.exec(select(Task.id).where(Task.client == client)).all() if i != keep_task_id]
        if not ids:
            return 0
        n = s.exec(select(func.count()).select_from(LlmCall).where(LlmCall.task_id.in_(ids))).one()
        s.exec(delete(LlmCall).where(LlmCall.task_id.in_(ids)))
        s.commit()
    return n


def latest_task_id(client):
    with session() as s:
        return s.exec(select(Task.id).where(Task.client == client).order_by(Task.id.desc())).first()


def _call_dict(c):
    request = json.loads(c.request_json) if c.request_json else None
    sent = defaultdict(lambda: {"messages": 0, "chars": 0})
    for m in (request or {}).get("messages", []):
        sent[m.get("role", "?")]["messages"] += 1
        sent[m.get("role", "?")]["chars"] += _chars(m)
    return {
        "seq": c.seq, "step": c.step, "purpose": c.purpose, "model": c.model, "attempt": c.attempt,
        "boost": c.boost, "status": c.status, "error": c.error,
        "duration_ms": c.duration_ms, "created_at": c.created_at.replace(tzinfo=datetime.timezone.utc).isoformat(),
        "prompt_tokens": c.prompt_tokens, "completion_tokens": c.completion_tokens,
        "total_tokens": c.total_tokens, "reasoning_tokens": c.reasoning_tokens,
        "message_count": c.message_count, "request_chars": c.request_chars, "sent_by_role": dict(sent),
        "request": request,  # None once pruned
        "response": json.loads(c.response_json) if c.response_json else None,
    }


def _sum(rows, field):
    return sum(getattr(r, field) or 0 for r in rows)


def _group(rows, key):
    groups = defaultdict(list)
    for r in rows:
        groups[key(r)].append(r)
    return {
        k: {"calls": len(v), "ok": sum(r.status == "ok" for r in v), "failed": sum(r.status != "ok" for r in v),
            "prompt_tokens": _sum(v, "prompt_tokens"), "completion_tokens": _sum(v, "completion_tokens"),
            "total_tokens": _sum(v, "total_tokens")}
        for k, v in groups.items()
    }


def task_report(task_id, client=None):
    """The whole report of one task as plain data, or None (no such task / not this client's)."""
    with session() as s:
        task = s.get(Task, task_id)
        if not task or (client is not None and task.client != client):
            return None
        rows = s.exec(select(LlmCall).where(LlmCall.task_id == task_id).order_by(LlmCall.seq)).all()
    return {
        "task": {"id": task.id, "text": task.text, "status": task.status, "model": task.model,
                 "created_at": task.created_at.replace(tzinfo=datetime.timezone.utc).isoformat()},
        "totals": {
            "calls": len(rows), "ok": sum(r.status == "ok" for r in rows), "failed": sum(r.status != "ok" for r in rows),
            "prompt_tokens": _sum(rows, "prompt_tokens"), "completion_tokens": _sum(rows, "completion_tokens"),
            "total_tokens": _sum(rows, "total_tokens"), "reasoning_tokens": _sum(rows, "reasoning_tokens"),
            "tokens_unknown": sum(r.status == "ok" and r.total_tokens is None for r in rows),
            "duration_ms": _sum(rows, "duration_ms"),
        },
        "by_model": _group(rows, lambda r: r.model),
        "by_purpose": _group(rows, lambda r: r.purpose),
        "calls": [_call_dict(r) for r in rows],
    }


def _n(value):
    return "n/a" if value is None else f"{value:,}"


def _short(text, limit=1500):
    text = text or ""
    return text if len(text) <= limit else f"{text[:limit]}… [{len(text) - limit} more chars]"


def task_report_markdown(report):
    """The report as a readable Markdown file (the side panel's download)."""
    t, tot = report["task"], report["totals"]
    lines = [
        f"# LLM report – task {t['id']}", "", f"**Task:** {t['text']}", f"**Status:** {t['status']} · "
        f"**Started:** {t['created_at']}", "",
        "## Totals", "",
        f"- LLM calls: {tot['calls']} ({tot['ok']} ok, {tot['failed']} failed)",
        f"- Tokens: {_n(tot['total_tokens'])} total = {_n(tot['prompt_tokens'])} sent + {_n(tot['completion_tokens'])} received"
        + (f" ({_n(tot['reasoning_tokens'])} of the received were reasoning)" if tot["reasoning_tokens"] else ""),
        f"- Time spent waiting for models: {tot['duration_ms'] / 1000:.1f}s", "",
        "## By model", "", "| Model | Calls | Failed | Sent | Received | Total |", "|---|---|---|---|---|---|",
    ]
    for m, g in report["by_model"].items():
        lines.append(f"| {m} | {g['calls']} | {g['failed']} | {_n(g['prompt_tokens'])} | {_n(g['completion_tokens'])} | {_n(g['total_tokens'])} |")
    lines += ["", "## Calls"]
    for c in report["calls"]:
        where = _where(c["purpose"], c["step"])
        lines += [
            "", f"### #{c['seq']} · {where} · {c['model']} · {c['status']}"
            + (f" (retry {c['attempt']})" if c["attempt"] else ""),
            f"Sent {_n(c['prompt_tokens'])} tokens · received {_n(c['completion_tokens'])} · {c['duration_ms']} ms"
            + (f" · error: {c['error']}" if c["error"] else ""),
        ]
        if not c["request"]:
            lines.append("_(text no longer stored – older than the newest %d tasks)_" % KEEP_PAYLOADS)
            continue
        lines += ["", "**Sent**", ""]
        for m in c["request"]["messages"]:
            body = m.get("content")
            if isinstance(body, list):
                body = "\n".join(p.get("text") or p["image_url"]["url"] for p in body)
            if m.get("tool_calls"):
                body = f"{body or ''}\ntool_calls: {json.dumps(m['tool_calls'], ensure_ascii=False)}"
            lines.append(f"- *{m.get('role')}*:\n\n  ```\n{_short(body).replace(chr(10), chr(10) + '  ')}\n  ```")
        lines += ["", f"Tools offered: {', '.join(c['request']['tools']) or 'none'}", "", "**Received**", ""]
        r = c["response"] or {}
        lines += [f"```\n{_short(r.get('content'))}\n```"] if r.get("content") else []
        for call in r.get("tool_calls", []):
            f = call.get("function", {})
            lines.append(f"- tool call `{f.get('name')}`: `{f.get('arguments')}`")
    return "\n".join(lines) + "\n"
