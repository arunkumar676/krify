"""Talks to Groq. Retries broken replies, and switches model when one hits its free limit."""

import asyncio
import time

from groq import AsyncGroq, BadRequestError, RateLimitError

from app.config import MODELS

_client = AsyncGroq()
_blocked_until = {}  # model -> time when its limit resets


def _short(model):
    return model.split("/")[-1]


async def ask_ai(messages, tools, preferred, on_note=None):
    """Return (message, model_used, tokens_used). Tries the preferred model first, then the others."""
    order = [preferred] + [m for m in MODELS if m != preferred]

    for model in order:
        if _blocked_until.get(model, 0) > time.time():
            continue
        for attempt in range(3):
            try:
                response = await _client.chat.completions.create(
                    model=model, messages=messages, tools=tools, tool_choice="auto", **MODELS[model]["extra"]
                )
                return response.choices[0].message, model, response.usage.total_tokens
            except BadRequestError as err:
                # The model sometimes produces a malformed tool call; asking again usually fixes it.
                broken = "tool_use_failed" in str(err) or "output_parse_failed" in str(err)
                if not broken or attempt == 2:
                    raise
            except RateLimitError as err:
                wait = float(err.response.headers.get("retry-after", 10))
                if wait <= 15 and attempt < 2:  # per-minute limit: a short pause is enough
                    if on_note:
                        await on_note(f"⏳ Busy, waiting {wait:.0f}s…")
                    await asyncio.sleep(wait)
                    continue
                # Daily limit (or long wait): park this model and try the next one.
                _blocked_until[model] = time.time() + wait
                if on_note:
                    await on_note(f"🔁 {_short(model)} limit reached – switching model")
                break

    soonest = min(_blocked_until.get(m, 0) for m in order) - time.time()
    minutes = max(1, round(soonest / 60))
    raise RuntimeError(f"All free models are at their limit for now. Try again in about {minutes} minute(s).")
