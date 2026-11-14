"""Before acting: turn the user's words into a clear goal, a short plan and a "done when" check.

Planned from the AI's own knowledge plus the full view of the page that is open now (the same view
the agent gets). Other sites haven't been seen yet: the agent fixes the plan later, when it sees each one.
"""

import json
import logging
import re

from app.context.prompt import where_line
from app.llm.client import ask_ai

log = logging.getLogger("krify")

MIN_WORDS = 4  # shorter tasks ("open youtube") are simple enough to skip planning

PLANNER_PROMPT = """You plan tasks for a browser agent. Requests may be casual.
Reply with ONLY this JSON, nothing else:
{"goal": "...", "steps": ["...", "..."], "done_when": "..."}

- goal: one clear English sentence saying what the user wants. Keep their exact names, numbers and sites.
  The goal and steps cover ONLY what the user's task asks. The previous task is there only to fill in
  words that point back to it ("it", "that", "the same", "there", "again", "also", "now on flipkart").
  A request that makes sense on its own is a new task: don't carry anything over from the previous one.
  "This page/site/app" means the current page, not the previous task.
- steps: 2-6 short steps (sites to open, what to search, what to read and note). Plan from what you know;
  you haven't seen other sites yet, so don't name their buttons. The current page is shown below the task:
  for a task about that page you may use what it really shows (its fields, buttons, text). Don't invent details the user didn't give
  (price limits, currency, models, shops). If the task is about the current page, don't plan to open it.
  Current page is a Google Sheet: read_text to see the columns, then sheet_query (SQL) for the answer.
- done_when: how to tell the task is finished, as something visible (e.g. "both prices are noted from
  real pages"). Going to / opening a page: just that page being open (its title or URL, or its menu item
  marked current/selected) – don't require content you only assume that page has.
- A product that comes in versions with different prices (storage, size, RAM) and the user didn't say
  which: don't ask now. Plan to look on the first site, note the versions and prices there, then ask
  the user which one (with those real options), and only then check the other sites. Example for
  "compare iphone 16e on amazon and flipkart": ["Search amazon.in for iPhone 16e", "Note each storage
  version with its price", "Ask the user which version", "Search flipkart.com for that exact version
  and note its price", "Compare the two prices"].
- You can't ask the user anything here: plan a step to ask later instead, once the sites show the options."""


def _parse(text):
    """Pull the JSON object out of the reply (models sometimes wrap it in ``` or add words)."""
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    goal = str(data.get("goal") or "").strip()
    steps = [str(s).strip() for s in (data.get("steps") or []) if str(s).strip()][:6]
    if not goal or not steps:
        return None
    return {
        "goal": goal[:300],
        "steps": steps,
        "done_when": str(data.get("done_when") or "").strip()[:200],
    }


async def understand(task, page, last_task, model, location=None, on_note=None):
    """Return ({goal, steps, done_when} or None, tokens used). None = carry on without a plan."""
    if len(task.split()) < MIN_WORDS:
        return None, 0
    context = f"User's task: {task}\n{page}"
    if where := where_line(location):
        context += f"\n{where}"
    if last_task:
        context += f"\nPrevious task (use only if the task points back to it): {last_task}"
    messages = [{"role": "system", "content": PLANNER_PROMPT}, {"role": "user", "content": context}]
    try:
        reply, _, tokens = await ask_ai(messages, None, model, on_note=on_note)
    except Exception as err:  # planning is optional: never fail the task because of it
        log.warning("Planning skipped: %s", err)
        return None, 0
    result = _parse(reply.content)
    if not result:
        log.info("Planning skipped: unusable reply %r", (reply.content or "")[:200])
    return result, tokens
