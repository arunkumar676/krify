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

# A plan this short is only shown to the user, not held for their approval – unless it does something
# with side effects (the words below, plus every RISKY_WORDS category). Reading and navigating are free.
PLAN_FREE_STEPS = 2
PLAN_SIDE_EFFECTS = r"message|e-?mail|comment|edit|update|create|add|save|upload|write|fill|enter|type|talk|chat|ask"


def plan_needs_approval(steps, task=""):
    """Must the user approve this plan before Krify acts? Yes for 3+ steps, or when the task or any
    step could send, change, delete, pay for or sign in to something. A one- or two-step look-up or
    "go to this page" plan doesn't: asking would cost a round trip and add nothing."""
    if len(steps) > PLAN_FREE_STEPS:
        return True
    words = "|".join([*RISKY_WORDS.values(), PLAN_SIDE_EFFECTS])
    return bool(re.search(rf"\b(?:{words})\b", " ".join([task, *steps]), re.I))


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


# Keys that wipe content in Google Sheets/Docs (selected cells, text).
ERASE_KEYS = r"delete|backspace|ctrl\s*\+\s*x|control\s*\+\s*x"
GOOGLE_EDITORS = r"^https://docs\.google\.com/(spreadsheets|document|presentation)/"


def check(name, args, elements, last_typed_line="", url=""):
    """Return a short reason if the action needs the user's OK, else None."""
    key = str(args.get("key", "")).strip()
    in_editor = bool(re.match(GOOGLE_EDITORS, url or ""))
    if name == "press_key" and in_editor and re.fullmatch(ERASE_KEYS, key, re.IGNORECASE):
        return "Deleting"
    # Typing into a Google Sheet/Doc changes it: asked once per task (see Agent.do_action).
    if in_editor and (name == "type_here" or (name == "type" and "name box" not in
                                               element_line(elements, args.get("element")).lower())):
        return "Editing"
    if in_editor and name == "drag":
        return "Editing"  # dragging in a sheet/doc moves its content
    if name in ("click", "select_option", "click_at"):
        # click_at has no element number: the loop looks up what's under the spot ("_under").
        line = args.get("_under", "") if name == "click_at" else element_line(elements, args.get("element"))
        # A clickable table row only opens its record; its label is the row's cell text ("Payment
        # pending", "Deleted", "Sent" are statuses, not what the click does). The row's own buttons
        # ("Delete") are separate elements and are still checked.
        if name != "select_option" and line.startswith("row link "):
            return None
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
        if args.get("submit"):  # type + Enter in one step: same checks as pressing Enter
            return _enter_risk(elements, line, args.get("_enter"))

    elif name == "press_key" and key.lower() == "enter":
        return _enter_risk(elements, last_typed_line, args.get("_enter"))

    return None


def _enter_risk(elements, typed_line, target=None):
    """Enter sends the field's form. `target` (from the page, if it could tell): the field, its form's
    default button and whether that form has a password box – judged like clicking that button."""
    if isinstance(target, dict) and target.get("inForm"):
        button = visible_text(target.get("submitter", ""))
        for category, words in RISKY_WORDS.items():
            if button and _find(words, button):
                return category  # Enter in a checkout field = clicking "Place order"
        if target.get("password"):
            return "Accounts"  # Enter on a login form signs in
        if _find(MESSAGE_FIELDS, typed_line or target.get("field", "")):
            return "Sending"
        return None
    # No form info: be careful – any password box on the page may mean a login form.
    if "input password" in (elements or ""):
        return "Accounts"  # Enter on a login form signs in
    if _find(MESSAGE_FIELDS, typed_line):
        return "Sending"  # Enter in a message box usually sends it
    return None
