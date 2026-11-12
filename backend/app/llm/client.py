"""Talks to the AI providers. Retries broken replies, and switches model when one is out of
free quota or unavailable. Prefers waiting a little for a strong model over using a backup one."""

import asyncio
import logging
import os
import re
import time

import openai
from openai import AsyncOpenAI

from app.config import MODELS, PROVIDERS, model_name, provider_of
from app.llm import report, usage

log = logging.getLogger("krify")

_clients = {
    p: AsyncOpenAI(base_url=cfg["base_url"], api_key=os.environ[cfg["key"]], timeout=20, max_retries=0)
    for p, cfg in PROVIDERS.items()
    if os.getenv(cfg["key"])
}
_blocked_until = {}  # model -> time when it can be tried again
_blocked_why = {}  # model -> "limit" | "busy" | "problem"

MAX_WAIT_FOR_STRONG = 60  # seconds: wait this long for a strong model before using a backup one
BROKEN_REPLY_CODES = {"tool_use_failed", "output_parse_failed"}

# Gemini attaches a "thought signature" to each tool call and refuses a chat whose earlier tool
# calls lack one (e.g. calls made by a Groq model before a switch). Google documents this
# placeholder for calls that came from other models.
GEMINI_PLACEHOLDER = {"google": {"thought_signature": "skip_thought_signature_validator"}}


def _label(model):
    return f"{PROVIDERS[provider_of(model)]['name']} {MODELS[model]['label']}"


def _retry_seconds(err):
    """How long the provider asks us to wait. Groq sends a retry-after header; Gemini puts
    'retryDelay': '20s' (or 'retry in 20s') in the error. Unknown = 60s."""
    header = err.response.headers.get("retry-after")
    if header:
        return float(header)
    text = f"{err} {err.body}"
    match = re.search(r"retry(?:Delay[\"']?\s*:\s*[\"']?| in )(\d+(?:\.\d+)?)\s*s", text, re.IGNORECASE)
    return float(match.group(1)) if match else 60.0


def _for_provider(messages, provider):
    """Copy of the chat adjusted for one provider. The stored chat itself is never changed."""
    out = []
    for msg in messages:
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            calls = []
            for call in msg["tool_calls"]:
                call = dict(call)
                if provider == "gemini":
                    call.setdefault("extra_content", GEMINI_PLACEHOLDER)
                else:
                    call.pop("extra_content", None)  # Gemini-only field
                calls.append(call)
            msg = {**msg, "tool_calls": calls}
        out.append(msg)
    return out


def _blocked_for(model):
    return _blocked_until.get(model, 0) - time.time()


def is_blocked(model):
    """True while `model` is cooling down after an error/limit (callers that want only that model skip it)."""
    return _blocked_for(model) > 0


def _record(model, chat, tools, extra, boost, need_vision, attempt, started, response=None, status="ok", error=""):
    """Put the request in the LLM report. The report must never break a task."""
    try:
        report.record_call(model, provider_of(model), attempt, chat, tools, extra, boost, need_vision,
                           round((time.monotonic() - started) * 1000), response, status, error)
    except Exception:
        log.exception("Couldn't save the LLM report entry")


async def _try_model(model, messages, tools, on_note, boost=False, need_vision=False):
    """One model, with in-place retries. Returns (message, tokens) or None (model now blocked)."""
    provider = provider_of(model)
    chat = _for_provider(messages, provider)
    tool_args = {"tools": tools, "tool_choice": "auto"} if tools else {}  # no tools: a plain text reply
    extra = {**MODELS[model]["extra"], **MODELS[model]["boost"]} if boost and MODELS[model].get("boost") else MODELS[model]["extra"]
    for attempt in range(3):
        started = time.monotonic()
        try:
            response = await _clients[provider].chat.completions.create(
                model=model_name(model), messages=chat, **tool_args, **extra
            )
            u = response.usage
            tokens = (u.total_tokens if u else 0) or 0
            if u:
                log.info("%s: in %s / out %s tokens", _label(model), u.prompt_tokens, u.completion_tokens)
            usage.record(model, tokens)
            _record(model, chat, tools, extra, boost, need_vision, attempt, started, response)
            return response.choices[0].message, tokens
        except openai.RateLimitError as err:
            _record(model, chat, tools, extra, boost, need_vision, attempt, started, status="rate_limit", error=err)
            usage.learn_from_limit_error(model, err)
            wait = _retry_seconds(err)
            if wait <= 20 and attempt < 2:  # short per-minute limit: just wait
                if on_note:
                    await on_note(f"⏳ Busy, waiting {wait:.0f}s…")
                await asyncio.sleep(wait)
                continue
            _blocked_until[model] = time.time() + wait
            _blocked_why[model] = "limit"
            reason = "limit reached"
        except openai.BadRequestError as err:
            broken = getattr(err, "code", None) in BROKEN_REPLY_CODES
            _record(model, chat, tools, extra, boost, need_vision, attempt, started,
                    status="broken_reply" if broken else "rejected", error=err)
            # Some models now and then produce a malformed tool call; asking again usually fixes it.
            if getattr(err, "code", None) in BROKEN_REPLY_CODES and attempt < 2:
                log.info("%s sent a broken reply, asking again", model)
                continue
            log.warning("%s rejected the request: %s", model, str(err.body)[:300])
            _blocked_until[model] = time.time() + 30
            _blocked_why[model] = "problem"
            reason = "had a problem"
        except (openai.APITimeoutError, openai.APIConnectionError, openai.InternalServerError,
                openai.PermissionDeniedError, openai.NotFoundError) as err:
            log.warning("%s unavailable: %s", model, str(err)[:200])
            _record(model, chat, tools, extra, boost, need_vision, attempt, started, status="unavailable", error=err)
            _blocked_until[model] = time.time() + 120  # busy/down: skip it for 2 minutes
            _blocked_why[model] = "busy"
            reason = "is busy"
        except openai.APIStatusError as err:
            # e.g. 413 "request too large" (a small per-minute size limit): use another model for now.
            log.warning("%s refused the request (%s): %s", model, err.status_code, str(err)[:200])
            _record(model, chat, tools, extra, boost, need_vision, attempt, started, status="rejected", error=err)
            _blocked_until[model] = time.time() + 60
            _blocked_why[model] = "busy"
            reason = "can't take a request this big" if err.status_code == 413 else "had a problem"
        if on_note:
            await on_note(f"🔁 {_label(model)} {reason} – switching model")
        return None
    return None


MAX_WAIT_FOR_SAME = 60  # seconds worth waiting for the task's own model


def _short_wait(model, usable):
    """Seconds until `model` is free again, if it's only out for a short per-minute limit; else 0."""
    left = _blocked_for(model)
    if model in usable and _blocked_why.get(model) == "limit" and 0 < left <= MAX_WAIT_FOR_SAME:
        return left
    return 0


def skip_model(model, seconds=60):
    """Don't use this model for a while (e.g. it keeps sending empty replies)."""
    _blocked_until[model] = time.time() + seconds
    _blocked_why[model] = "busy"


def has_vision_model():
    return any(info["vision"] for info in MODELS.values())


async def ask_ai(messages, tools, preferred, on_note=None, need_vision=False, boost=False):
    """Return (message, model_used, tokens_used).

    Order: the model you picked, then strong models. If all strong models are busy for less than
    a minute, wait for one. Backup models are used only when the strong ones are out for longer.
    need_vision: the chat contains a screenshot, so only models that can see images are used.
    boost: this step needs more thought than usual (a model-specific "extra" override, e.g. higher
    reasoning_effort) - see MODELS[model]["boost"] in config.py.
    """
    usable = [m for m in MODELS if MODELS[m]["vision"] or not need_vision]
    strong = [m for m in usable if MODELS[m]["tier"] == "strong"]
    backup = [m for m in usable if MODELS[m]["tier"] == "backup"]
    first = ([preferred] if preferred in usable else []) + [m for m in strong if m != preferred]

    for _ in range(6):
        # The task's model hit a short per-minute limit: wait for it rather than switching mid-task
        # (other models, especially backups, tend to lose the thread and use more tokens).
        if wait := _short_wait(preferred, usable):
            if on_note:
                await on_note(f"⏳ {_label(preferred)} is busy – waiting {wait:.0f}s for it")
            await asyncio.sleep(wait)
        waiting_for_preferred = False
        for model in first:
            if _blocked_for(model) <= 0:
                result = await _try_model(model, messages, tools, on_note, boost, need_vision)
                if result:
                    return result[0], model, result[1]
                if model == preferred and _short_wait(preferred, usable):
                    waiting_for_preferred = True
                    break
        if waiting_for_preferred:
            continue

        soonest = min((_blocked_for(m) for m in strong), default=None)
        if soonest is not None and 0 < soonest <= MAX_WAIT_FOR_STRONG:
            if on_note:
                await on_note(f"⏳ Strong models busy, waiting {soonest:.0f}s…")
            await asyncio.sleep(soonest)
            continue

        for model in backup:
            if model not in first and _blocked_for(model) <= 0:
                result = await _try_model(model, messages, tools, on_note, boost, need_vision)
                if result:
                    return result[0], model, result[1]
        break

    soonest = min((_blocked_for(m) for m in usable), default=60)
    minutes = max(1, round(soonest / 60))
    if need_vision:
        raise RuntimeError(
            f"All models that can read screenshots are busy or at their limit. Try again in about {minutes} minute(s)."
        )
    if all(_blocked_why.get(m) == "limit" and _blocked_for(m) > 0 for m in MODELS):
        raise RuntimeError(
            f"⛔ All free models have used up their limits. Try again in about {minutes} minute(s)."
        )
    raise RuntimeError(f"All free models are busy or at their limit. Try again in about {minutes} minute(s).")


def model_status(model):
    """Text shown next to the model in the picker."""
    left = _blocked_for(model)
    if left > 60 and _blocked_why.get(model) == "limit":
        hours, minutes = divmod(round(left / 60), 60)
        return f"⛔ limit reached ({f'{hours}h ' if hours else ''}{minutes}m left)"
    if left > 0 and _blocked_why.get(model) == "busy":
        return "busy right now"
    return usage.status(model)
