"""A plain-text reply that should have been a done / ask_user call."""

import json
import re


def clean_text_reply(text):
    """The model sometimes writes done({...}) / ask_user({...}) as plain text. Pull out the message.

    Returns (kind, message, clear) where kind is "done" or "ask", and clear is False when the text
    is just free text (could be nonsense from a weak model).
    """
    text = (text or "").strip()
    # Forms seen: done({"summary": ...}), ask_user({"question": ...}), a bare {"summary": ...},
    # {"done": {"summary": ...}}, {"name": "done", "arguments": {...}} – sometimes cut off before the
    # last brace, or inside ```json fences.
    match = re.search(r"\b(?:done|ask_user)\s*\(\s*(\{.*\})\s*\)", text, re.DOTALL)
    candidate = match.group(1) if match else re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    if candidate.startswith("{"):
        try:
            args = json.loads(candidate)
        except json.JSONDecodeError:
            args = None
        while isinstance(args, dict) and "summary" not in args and "question" not in args:
            inner = args.get("done") or args.get("ask_user") or args.get("arguments") or args.get("parameters")
            if isinstance(inner, str):
                try:
                    inner = json.loads(inner)
                except json.JSONDecodeError:
                    inner = None
            args = inner
        if isinstance(args, dict):
            if "question" in args:
                return "ask", str(args["question"]), True
            if "summary" in args:
                return "done", str(args["summary"]), True
        # Broken JSON (e.g. a missing closing brace): pull the field out directly.
        for field, kind in (("question", "ask"), ("summary", "done")):
            if found := re.search(rf'"{field}"\s*:\s*"((?:[^"\\]|\\.)*)"', candidate):
                try:
                    return kind, json.loads(f'"{found.group(1)}"'), True
                except json.JSONDecodeError:
                    return kind, found.group(1), True
    return "done", text or "Done.", False
