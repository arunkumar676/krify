"""Counts tokens and requests per model per day, saved in backend/usage.json.

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

log = logging.getLogger("krify")

FILE = Path(__file__).resolve().parents[2] / "usage.json"


def _today():
    return datetime.date.today().isoformat()


def _load():
    try:
        data = json.loads(FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data.setdefault("limits", {})  # kept across days
    if data.get("date") != _today():
        data["date"] = _today()
        data["models"] = {}
    data.setdefault("models", {})
    return data


_data = _load()


def _save():
    try:
        FILE.write_text(json.dumps(_data, indent=1), encoding="utf-8")
    except OSError as err:
        log.warning("Couldn't save usage: %s", err)


def _entry(model):
    if _data["date"] != _today():  # new day while the server is running
        _data.update(date=_today(), models={})
    return _data["models"].setdefault(model, {"tokens": 0, "requests": 0})


def record(model, tokens):
    entry = _entry(model)
    entry["tokens"] += tokens
    entry["requests"] += 1
    _save()


def learn_from_limit_error(model, err):
    """Read the daily limit (and usage so far) out of a 'limit reached' error."""
    text = f"{err} {getattr(err, 'body', '')}"
    limits = _data["limits"].setdefault(model, {})
    entry = _entry(model)

    if m := re.search(r"tokens per day \(TPD\): Limit (\d+), Used (\d+)", text):  # Groq
        limits["tokens"] = int(m.group(1))
        entry["tokens"] = max(entry["tokens"], int(m.group(2)))
    if m := re.search(r"requests per day \(RPD\): Limit (\d+), Used (\d+)", text):  # Groq
        limits["requests"] = int(m.group(1))
        entry["requests"] = max(entry["requests"], int(m.group(2)))
    # Gemini: ... 'quotaId': 'GenerateRequestsPerDayPerProjectPerModel-FreeTier', ... 'quotaValue': '250'
    if m := re.search(r"quotaId['\"]?\s*:\s*['\"]([^'\"]*PerDay[^'\"]*)['\"].*?quotaValue['\"]?\s*:\s*['\"]?(\d+)",
                      text, re.DOTALL):
        limits["requests"] = int(m.group(2))
    if not limits:
        _data["limits"].pop(model, None)
    _save()


def status(model):
    """Short text for the model picker, e.g. '64% used' or '18k tokens today'."""
    entry = _data["models"].get(model, {"tokens": 0, "requests": 0}) if _data["date"] == _today() else {}
    tokens, requests = entry.get("tokens", 0), entry.get("requests", 0)
    limits = _data["limits"].get(model, {})
    shares = []
    if limits.get("tokens"):
        shares.append(tokens / limits["tokens"])
    if limits.get("requests"):
        shares.append(requests / limits["requests"])
    if shares:
        return f"{min(100, round(100 * max(shares)))}% used"
    if tokens:
        return f"{tokens / 1000:.0f}k tokens today" if tokens >= 1000 else f"{tokens} tokens today"
    return ""
