"""Decides which actions are risky. Risky actions always need the user's OK – this runs in code,
so it works even if the AI forgets the rules."""

import re

# Category -> words that make a click risky (matched against the element's label).
RISKY_WORDS = {
    "Money": r"buy|pay|payment|place (your )?order|check ?out|purchase|proceed to (buy|pay|checkout)|"
    r"add (a )?(card|payment)|donate|book now|confirm (order|booking|payment)|transfer|upi",
    "Sending": r"send|submit|post|publish|reply|tweet",
    "Deleting": r"delete|remove|cancel (order|subscription|booking)|unsubscribe|deactivate|discard|"
    r"empty (cart|trash|bin)|clear (history|cart)",
    "Accounts": r"sign ?in|log ?in|sign ?out|log ?out|sign ?up|register|(change|reset) password",
}

# Labels of boxes that hold private data.
PRIVATE_FIELDS = (
    r"password|passcode|card number|credit card|debit card|cvv|cvc|otp|one[- ]time|\bpin\b|"
    r"security code|expiry|expiration|aadhaar|pan number|bank account|ifsc"
)

# Typing into these and pressing Enter usually sends something.
MESSAGE_FIELDS = r"message|comment|reply|post|chat|compose|tweet|write"


def _find(pattern, text):
    return re.search(rf"\b({pattern})\b", text, re.IGNORECASE)


def element_line(elements, element_id):
    """The element's line from the page list, e.g. 'button "Place your order"', or ''."""
    match = re.search(rf"^\[{element_id}\] (.*)$", elements or "", re.MULTILINE)
    return match.group(1) if match else ""


def visible_text(line):
    """Only the quoted labels/values of an element line, not its type.
    'input submit "Go" (value: "Go")' -> 'Go Go' (so a search box's "submit" type isn't risky)."""
    return " ".join(re.findall(r'"([^"]*)"', line))


def check(name, args, elements, last_typed_line=""):
    """Return a short reason if the action needs the user's OK, else None."""
    if name in ("click", "select_option"):
        line = element_line(elements, args.get("element"))
        text = f"{visible_text(line)} {args.get('option', '')}"
        for category, words in RISKY_WORDS.items():
            if _find(words, text):
                return category
        if _find(PRIVATE_FIELDS, text):
            return "Private data"

    elif name == "type":
        line = element_line(elements, args.get("element"))
        if "input password" in line or _find(PRIVATE_FIELDS, line):
            return "Private data"

    elif name == "press_key" and args.get("key") == "Enter":
        if "input password" in (elements or ""):
            return "Accounts"  # Enter on a login form signs in
        if _find(MESSAGE_FIELDS, last_typed_line):
            return "Sending"  # Enter in a message box usually sends it

    return None
