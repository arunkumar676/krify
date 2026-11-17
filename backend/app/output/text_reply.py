"""A plain-text reply that should have been a done / ask_user call."""

import json
import re

# A plain-text reply that is still narrating – what it will do next, or a question – is not an answer.
_NOT_AN_ANSWER = re.compile(
    r"\b(i'?ll|will|i'?m going to|i am going to|needs? to|i should|i must|i have to|i can now|i'?m about to|"
    r"let me|let'?s|next|now i|then i|first i|going to|about to|trying to|try to|attempt\w*|proceed\w*|continu\w*|"
    r"opening|clicking|typing|searching|selecting|navigating|scrolling|looking for|checking|waiting|loading|"
    r"one moment|please wait|working on|step \d)\b|\?\s*$|[{}\[\]`]|<\|", re.I)
MIN_ANSWER_WORDS = 4
MAX_ANSWER_CHARS = 1500


def reads_as_answer(text):
    """True if a plain-text reply reads as the finished task's answer – a statement, not a plan for the next
    step, a question, JSON or model noise. The loop accepts it as `done` only right after the task's own
    actions (and still runs every check on it); anything else gets the "call an operation" reminder."""
    text = (text or "").strip()
    return (MIN_ANSWER_WORDS <= len(text.split()) and len(text) <= MAX_ANSWER_CHARS
            and not _NOT_AN_ANSWER.search(text))


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


# A sentence that starts like a step the model is about to take, not an answer ("I will scroll down…").
INTENT = re.compile(
    r"(?:^|[.!:\n]\s*)(?:okay|ok|sure|alright)?[,\s]*"
    r"(?:i will|i'll|i am going to|i'm going to|i need to|i should|i must|i can now|let me|let's|"
    r"next|now i|first|then,? i will|then,? i'll|to do this|step \d)\b",
    re.I,
)
# Signs of a tool call or page notes written as text.
CODE_LIKE = re.compile(r"\b(?:done|ask_user|click|type|select_option)\s*\(|\"(?:op|name|element|summary)\"\s*:|\[\d+\]")


def plain_answer(text):
    """Free text that reads as a finished answer ("done") or a question to the user ("ask"); None when it
    may be thinking out loud, code or nonsense (then the model is asked again for a proper call)."""
    text = (text or "").strip()
    if len(text) < 3 or not re.search(r"[^\W\d_]{2}", text) or text.startswith(("{", "[", "```", "<")):
        return None
    if CODE_LIKE.search(text) or INTENT.search(text):
        return None
    return "ask" if text.rstrip(" *_)\"'").endswith("?") else "done"
