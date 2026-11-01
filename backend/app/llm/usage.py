"""Counts tokens and requests per model per day (in the database).

Daily limits are learned from the providers' "limit reached" messages (Groq says e.g.
"tokens per day (TPD): Limit 200000, Used 199134"; Gemini lists a per-day quotaValue), so the
model picker can show "64% used". Counts start again each day (your PC's date – providers'
own reset times can differ a little).
"""

import datetime
import json
import logging
import re
from pathlib import Path

from app.db import Limit, Usage, session

log = logging.getLogger("krify")

OLD_FILE = Path(__file__).resolve().parents[2] / "usage.json"  # before the database existed


def _today():
    return datetime.date.today().isoformat()


def _usage(s, model):
    return s.get(Usage, (_today(), model)) or Usage(day=_today(), model=model)


def record(model, tokens):
    with session() as s:
        row = _usage(s, model)
        row.tokens += tokens
        row.requests += 1
        s.add(row)
        s.commit()


def learn_from_limit_error(model, err):
    """Read the daily limit (and usage so far) out of a 'limit reached' error."""
    text = f"{err} {getattr(err, 'body', '')}"
    with session() as s:
        limit = s.get(Limit, model) or Limit(model=model)
        row = _usage(s, model)
        if m := re.search(r"tokens per day \(TPD\): Limit (\d+), Used (\d+)", text):  # Groq
            limit.tokens = int(m.group(1))
            row.tokens = max(row.tokens, int(m.group(2)))
        if m := re.search(r"requests per day \(RPD\): Limit (\d+), Used (\d+)", text):  # Groq
            limit.requests = int(m.group(1))
            row.requests = max(row.requests, int(m.group(2)))
        # Gemini: ... 'quotaId': 'GenerateRequestsPerDayPerProjectPerModel-FreeTier', ... 'quotaValue': '250'
        if m := re.search(r"quotaId['\"]?\s*:\s*['\"]([^'\"]*PerDay[^'\"]*)['\"].*?quotaValue['\"]?\s*:\s*['\"]?(\d+)",
                          text, re.DOTALL):
            limit.requests = int(m.group(2))
        if limit.tokens or limit.requests:
            s.add(limit)
        s.add(row)
        s.commit()


def status(model):
    """Short text for the model picker, e.g. '64% used' or '18k tokens today'."""
    with session() as s:
        row = s.get(Usage, (_today(), model))
        limit = s.get(Limit, model)
    tokens, requests = (row.tokens, row.requests) if row else (0, 0)
    shares = []
    if limit and limit.tokens:
        shares.append(tokens / limit.tokens)
    if limit and limit.requests:
        shares.append(requests / limit.requests)
    if shares:
        return f"{min(100, round(100 * max(shares)))}% used"
    if tokens:
        return f"{tokens / 1000:.0f}k tokens today" if tokens >= 1000 else f"{tokens} tokens today"
    return ""


def _import_old_file():
    """One-time move of usage.json (from before the database) into the database."""
    if not OLD_FILE.exists():
        return
    try:
        data = json.loads(OLD_FILE.read_text(encoding="utf-8"))
        with session() as s:
            for model, lim in data.get("limits", {}).items():
                s.merge(Limit(model=model, tokens=lim.get("tokens"), requests=lim.get("requests")))
            if data.get("date") == _today():
                for model, used in data.get("models", {}).items():
                    row = _usage(s, model)  # never lower counts already in the database
                    row.tokens = max(row.tokens, used["tokens"])
                    row.requests = max(row.requests, used["requests"])
                    s.add(row)
            s.commit()
        OLD_FILE.replace(OLD_FILE.with_suffix(".json.imported"))
        log.info("Moved usage.json into the database")
    except (OSError, ValueError, KeyError) as err:
        log.warning("Couldn't import usage.json: %s", err)


_import_old_file()
