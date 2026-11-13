"""The gate in front of the agent: a cheap model decides whether a message needs the browser agent.

One small request (no tools, no page, no chat history) to a cheap model. It answers one of:
  chat   – greetings, thanks, "who are you" …            → the cheap model's reply is the answer
  answer – timeless general knowledge / maths / writing  → the cheap model's reply is the answer
  task   – anything else (browser, page, live data, unsure) → the message goes to the agent, unchanged

Fail-open: any problem (no cheap model, it is cooling down, error, timeout, unreadable or oversized
reply) means "task", so the gate can only save tokens, never swallow or delay a real task.
"""

import asyncio
import json
import logging
import re

from app.config import CHEAP_MODELS, MODELS, ROUTER, ROUTER_MODEL
from app.llm import client
from app.llm.client import ask_ai

log = logging.getLogger("krify")

GATE_TIMEOUT = 8  # seconds: a slow cheap model must never hold up a real task
MAX_REPLY = 1200  # characters: a longer reply is not small talk / a short answer → let the agent handle it
TAIL_CHARS = 300  # how much of the previous gate exchange is shown, so "explain more" works

KINDS = ("chat", "answer", "task")

GATE_PROMPT = """You are the front desk of Krify, a browser agent that opens sites, searches, reads pages, fills
forms and works with Google Sheets in the user's own browser. Decide whether you can reply yourself or
whether the browser agent must handle the message. Users may write casually. Always reply in English.
Reply with ONLY this JSON, nothing else:
{"kind": "chat" | "answer" | "task", "reply": "...", "groups": []}

- chat: greetings, thanks, goodbyes, "who are you", "what can you do" (answer from the description above).
  Reply briefly and warmly, in English.
- answer: timeless general knowledge, maths, explanations, translation or writing help that needs no
  browser, no web and no personal data. Reply in 6 lines or fewer, in English.
- task: EVERYTHING else, with an empty reply. That includes: anything about a page, tab, site, app or
  sheet ("this page", "here"); any action (open, click, search, find, fill, buy, send, download, log in,
  scroll, compare); live or current facts (prices, weather, news, scores, "today", "latest"); the user's
  own accounts, files or data; a follow-up to the previous browser task ("it", "that", "again", "now on
  flipkart", "the same"); and any message you are not sure about.
When in doubt, choose task. Never claim to have done something in the browser.
For a task, "groups" lists the extra tool groups it will need (basic browsing is always there; [] if none):
- forms: fill in a form – sign up, add a contact, checkout, apply
- tabs: compare sites or open something in a new tab
- sheets: work in Google Sheets or Google Docs
- mouse: hover menus, drag and drop, sliders, clicking on pictures or maps
- look: looking at images, charts or the screen itself
- lists: long lists or tables – count, search or go through many records"""


def cheap_model():
    """The model for the gate, or None (gate off, no cheap model has an API key, or it is cooling down)."""
    if not ROUTER:
        return None
    for model in [ROUTER_MODEL] if ROUTER_MODEL else CHEAP_MODELS:
        if model in MODELS and not client.is_blocked(model):
            return model
    return None


def parse(text):
    """(kind, reply) from the model's text, or ("task", "") when it isn't a clean, usable answer."""
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not match:
        return "task", ""
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return "task", ""
    if not isinstance(data, dict):
        return "task", ""
    kind = str(data.get("kind") or "").strip().lower()
    reply = data.get("reply")
    reply = reply.strip() if isinstance(reply, str) else ""
    if kind not in ("chat", "answer") or not reply or len(reply) > MAX_REPLY or reply.lstrip().startswith(("{", "[", "```")):
        return "task", ""
    return kind, reply


GROUPS = ("forms", "tabs", "sheets", "mouse", "look", "lists")


def parse_groups(text):
    """The tool groups the gate asked for, or None when its reply has no usable list (then all are sent)."""
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    try:
        data = json.loads(match.group(0)) if match else None
    except json.JSONDecodeError:
        return None
    groups = data.get("groups") if isinstance(data, dict) else None
    if not isinstance(groups, list):
        return None
    return {str(g).strip().lower() for g in groups} & set(GROUPS)


def tail_of(question, reply):
    """What the next gate call is shown about this exchange (kept short)."""
    return f"User: {question}\nYou: {reply}"[-TAIL_CHARS:]


async def gate(text, last_task, tail, model, on_note=None):
    """Return (kind, reply, tokens, groups). kind "task" (reply "") = hand the message to the agent; groups =
    the extra tool groups it needs (None = unknown: send them all)."""
    context = f"Message: {text}"
    if last_task:
        context += f"\nPrevious browser task: {last_task[:200]}"
    if tail:
        context += f"\nPrevious exchange with you:\n{tail}"
    messages = [{"role": "system", "content": GATE_PROMPT}, {"role": "user", "content": context}]
    try:
        reply, _, tokens = await asyncio.wait_for(ask_ai(messages, None, model, on_note=on_note), GATE_TIMEOUT)
    except Exception as err:  # includes the timeout: the gate is optional
        log.warning("Gate skipped: %s", err or type(err).__name__)
        return "task", "", 0, None
    kind, answer = parse(reply.content)
    groups = parse_groups(reply.content)
    log.info("Gate: %s %s", kind, sorted(groups) if groups is not None else "(all tools)")
    return kind, answer, tokens, groups
