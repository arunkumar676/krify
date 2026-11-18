"""Before "done": the page mustn't still be showing an error message."""

import re

# Words that make a status message a problem: errors, validation prompts, refusals. content.js reports every
# live region on the page (role=alert / status / aria-live), so a "FREE Delivery" banner or a carousel pager
# ("Page 1 of 4") arrives here too – those are information and must not bounce a finished task.
PROBLEM = re.compile(
    r"\b(?:errors?|invalid|valid|required|fail(?:s|ed|ure)?|unsuccessful|couldn't|could not|can't|cannot|unable|"
    r"unavailable|wrong|denied|rejected|declined|blocked|expired|incorrect|missing|must|please|try again|"
    r"not (?:found|allowed|valid|available|match(?:ing)?|supported|accepted|eligible)|doesn't|does not|didn't|did not|"
    r"isn't|is not|aren't|are not|won't|no longer|warning|sorry|oops|problem|out of stock|sold out)\b", re.I)
NON_ENGLISH = 0.9  # below this share of plain-ASCII letters we can't read the message: treat it as a problem


def is_problem(message):
    """True if one status message is (or might be) a problem. Form errors always are; text in a language the
    word list doesn't cover keeps the old behaviour (counts as a problem) rather than being waved through."""
    message = (message or "").replace("’", "'").strip()
    if not message:
        return False
    if message.startswith("Form error") or PROBLEM.search(message):
        return True
    letters = [c for c in message if c.isalpha()]
    return bool(letters) and sum(c.isascii() for c in letters) / len(letters) < NON_ENGLISH


def blocking_status(status):
    """The problems in the page's status text ('message | message …'), joined the same way, or '' if there
    are none: only these stop a "done" (check_ui_state) or the "you opened what was asked" shortcut."""
    return " | ".join(part for part in (status or "").split(" | ") if is_problem(part))


def check_ui_state(state):
    """UI check before 'done': reject once if the page is showing an error/validation message
    (see content.js's [Status] line), so the model can't declare success while the page is
    still complaining. Harmless status text (a banner, a pager) doesn't count. Returns a rejection
    message, or None if the answer may go out."""
    problems = blocking_status(state.page_status)
    if not problems or state.ui_check_failed:
        return None
    state.ui_check_failed = True
    return (
        f"REJECTED – not sent to the user. The page is showing: \"{problems}\". Make sure this "
        "isn't an error before calling done – read it, fix the problem, or call ask_user."
    )


TOAST_MAX = 300  # characters of one toast/popup message kept in the page's status


def toast_problems(changes):
    """Problem messages in the text an action brought (content.js watchDiff "texts"): a toast or popup such
    as Amazon's "Sorry, there was a problem … limit of 1 per customer". They are often not live regions, so
    the page's status never shows them. → list of messages (each cut to TOAST_MAX), maybe empty."""
    texts = (((changes or {}) if isinstance(changes, dict) else {}).get("texts") or {}).get("shown") or []
    return [t if len(t) <= TOAST_MAX else t[:TOAST_MAX] + "…" for t in texts if is_problem(t)]


def add_toasts(status, changes):
    """The page's status with the action's problem toasts added, so check_ui_state sees them before a done."""
    toasts = [t.replace(" | ", " – ") for t in toast_problems(changes)]  # one message, not split at " | "
    toasts = [t for t in toasts if t not in (status or "")]
    return " | ".join(part for part in [status or "", *toasts] if part)

