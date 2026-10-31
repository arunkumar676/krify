"""Talks to the AI providers. Retries broken replies, and switches model when one is out of
free quota or unavailable."""

import asyncio
import logging
import os
import re
import time

import openai
from openai import AsyncOpenAI

from app.config import MODELS, PROVIDERS, model_name, provider_of

log = logging.getLogger("krify")

_clients = {
    p: AsyncOpenAI(base_url=cfg["base_url"], api_key=os.environ[cfg["key"]], timeout=45, max_retries=0)
    for p, cfg in PROVIDERS.items()
    if os.getenv(cfg["key"])
}
_blocked_until = {}  # model -> time when it can be tried again


def _label(model):
    return f"{PROVIDERS[provider_of(model)]['name']} {MODELS[model]['label']}"


# Gemini attaches a "thought signature" to each tool call and refuses a chat whose earlier tool
# calls lack one (e.g. calls made by a Groq model before a switch). Google documents this
# placeholder for calls that came from other models.
GEMINI_PLACEHOLDER = {"google": {"thought_signature": "skip_thought_signature_validator"}}


def _retry_seconds(err):
    """How long the provider asks us to wait. Groq sends a retry-after header; Gemini puts
    'retryDelay': '20s' (or 'retry in 20s') in the error text. Unknown = 60s."""
    header = err.response.headers.get("retry-after")
    if header:
        return float(header)
    match = re.search(r"retry(?:Delay[\"']?\s*:\s*[\"']?| in )(\d+(?:\.\d+)?)\s*s", str(err), re.IGNORECASE)
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


async def ask_ai(messages, tools, preferred, on_note=None):
    """Return (message, model_used, tokens_used). Tries the preferred model first, then the others."""
    order = [preferred] + [m for m in MODELS if m != preferred]

    for model in order:
        if _blocked_until.get(model, 0) > time.time():
            continue
        provider = provider_of(model)
        client = _clients[provider]
        chat = _for_provider(messages, provider)
        for attempt in range(3):
            try:
                response = await client.chat.completions.create(
                    model=model_name(model), messages=chat, tools=tools, tool_choice="auto",
                    **MODELS[model]["extra"],
                )
                usage = response.usage
                return response.choices[0].message, model, (usage.total_tokens if usage else 0) or 0
            except openai.RateLimitError as err:
                wait = _retry_seconds(err)
                if wait <= 30 and attempt < 2:  # per-minute limit: wait for the better model
                    if on_note:
                        await on_note(f"⏳ Busy, waiting {wait:.0f}s…")
                    await asyncio.sleep(wait)
                    continue
                _blocked_until[model] = time.time() + wait  # daily limit: park it, use the next model
                reason = "limit reached"
            except openai.BadRequestError as err:
                # Some models now and then produce a malformed tool call; asking again usually fixes it.
                broken = "tool_use_failed" in str(err) or "output_parse_failed" in str(err)
                if broken and attempt < 2:
                    continue
                log.warning("%s rejected the request: %s", model, str(err.body)[:300])
                reason = "had a problem"
            except (openai.APITimeoutError, openai.APIConnectionError, openai.InternalServerError,
                    openai.PermissionDeniedError, openai.NotFoundError) as err:
                log.warning("%s unavailable: %s", model, str(err)[:200])
                _blocked_until[model] = time.time() + 120  # busy/down: skip it for 2 minutes
                reason = "is busy"
            if on_note:
                await on_note(f"🔁 {_label(model)} {reason} – switching model")
            break

    soonest = min((_blocked_until.get(m, 0) for m in order), default=0) - time.time()
    minutes = max(1, round(soonest / 60))
    raise RuntimeError(f"All free models are busy or at their limit. Try again in about {minutes} minute(s).")
