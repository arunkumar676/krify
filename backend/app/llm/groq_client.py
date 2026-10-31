"""Talks to Groq. Retries broken replies and waits out rate limits."""

import asyncio

from groq import AsyncGroq, BadRequestError, RateLimitError

from app.config import GROQ_MODEL

_client = AsyncGroq()


async def ask_ai(messages, tools, on_wait=None):
    """Return the AI's next message (text and/or tool calls)."""
    for attempt in range(4):
        try:
            response = await _client.chat.completions.create(
                model=GROQ_MODEL, messages=messages, tools=tools, tool_choice="auto"
            )
            return response.choices[0].message
        except BadRequestError as err:
            # The model sometimes produces a malformed tool call; asking again usually fixes it.
            broken = "tool_use_failed" in str(err) or "output_parse_failed" in str(err)
            if not broken or attempt == 3:
                raise
        except RateLimitError as err:
            if attempt == 3:
                raise
            wait = float(err.response.headers.get("retry-after", 10))
            if on_wait:
                await on_wait(f"Groq free limit reached, waiting {wait:.0f}s…")
            await asyncio.sleep(min(wait, 60))
