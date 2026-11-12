"""Before an answer or question goes out: "I have entered …" claims must match actions that really ran,
and a question mustn't hand the user a browser step Krify can do itself."""

import re

# "I have entered / clicked / …" in an answer or question: what kind of action must back it up.
_CLAIMS = [
    ("type", re.compile(r"\b(entered|typed|filled( in| out)?|input|keyed in|put in|written|wrote)\b", re.I)),
    ("click", re.compile(r"\b(clicked|pressed|tapped|selected|chose|chosen|submitted|added|sent|posted|placed|saved|"
                         r"signed (in|up|out)|logged (in|out)|opened|applied|confirmed|uploaded|deleted|removed)\b", re.I)),
]
# "I entered", "I have entered", "I've just entered", "I have now successfully entered" – the claim's subject.
_FIRST_PERSON = re.compile(r"\bi('ve| have| had)?(\s+(now|already|successfully|just|also))*\s+$", re.I)
_TYPE_OPS = {"type", "type_here", "go_to_cell"}
_CLICK_OPS = {"click", "click_at", "press_key", "select_option", "type", "drag", "open_url", "new_tab", "go_back"}
CLAIM_REJECTED = (
    "REJECTED – not sent to the user: {}. Don't say you did something you didn't. Do it now with the right "
    "tool (e.g. type the value into the field, then click its button) and check the page – or, if you can't, "
    "say honestly what's still needed."
)

# A question that hands a browser step to the user ("Please sign in…", "click X", "go to the page")
# or asks permission for something the Allow/Block prompt already covers.
HANDOFF = re.compile(r"\b(please|kindly|could you|can you|you (need|have) to|you'll need to)\b[^.?!]{0,40}?\b"
                     r"(sign ?in|log ?in|click|tap|go to|open|navigate|select|scroll)\b"
                     r"|\b(do you approve|may i|shall i|should i|can i|is it ok(ay)?( if i| to)?|do i have your permission)\b"
                     r"[^.?!]{0,40}?\b(sign ?in|log ?in|post|submit|continue|proceed|go ahead)\b", re.I)
# Things only the user can give: a question about these is fine.
USER_ONLY = re.compile(r"password|passcode|\botp\b|one[- ]time|verification code|captcha|security code|\bpin\b|"
                       r"which (one|version|option|account)|choose|prefer", re.I)
HANDOFF_REJECTED = (
    "REJECTED – not sent to the user: you're asking them to do a browser step (or to approve one) that you can "
    "do yourself. Do it now: risky steps like signing in get an Allow/Block prompt automatically.{} Ask the "
    "user only for what you can't know (a password, an OTP, a choice)."
)


def unbacked_claims(text, actions):
    """First-person claims ("I have entered the number 938…") with no matching action this task."""
    problems = []
    for sentence in re.split(r"(?<=[.!?])\s+", text or ""):
        for kind, verbs in _CLAIMS:
            for match in verbs.finditer(sentence):
                if not _FIRST_PERSON.search(sentence[:match.start()][-40:]):
                    continue  # not "I (have) …" – e.g. "Amazon has sent an OTP"
                ops = _TYPE_OPS if kind == "type" else _CLICK_OPS
                # Typing only counts as a click/submit when it pressed Enter too (type with submit=true).
                done = [(op, args) for op, args, _ in actions
                        if op in ops and (kind == "type" or op != "type" or args.get("submit"))]
                verb = match.group(0)
                if not done:
                    what = "typing" if kind == "type" else "click or key"
                    problems.append(f'you wrote "I {verb}", but no {what} action ran in this task')
                elif kind == "type":
                    # A value named in the claim (a long number, a quoted text) must be what was typed.
                    values = [v for pair in re.findall(r"(\d{4,})|\"([^\"]{2,})\"", sentence) for v in pair if v]
                    typed = " ".join(f"{args.get('text', '')} {args.get('cell', '')}" for _, args in done)
                    missing = [v for v in values if v not in typed]
                    if missing:
                        problems.append(f'you wrote "I {verb}" {", ".join(missing)}, but that was never typed')
    return list(dict.fromkeys(problems))


def check_claims(state, text):
    """Before an answer or question goes out: every "I have entered / clicked / submitted …" in it
    must match an action that really ran in this task (a named value – a number, a quoted text –
    must be what was typed). Rejects once; the second time it goes out. → message or None."""
    if state.claim_check_failed:
        return None
    problems = unbacked_claims(text, state.actions)
    if not problems:
        return None
    state.claim_check_failed = True
    return CLAIM_REJECTED.format("; ".join(problems))


def check_handoff(state, question):
    """Before a question goes out: is Krify telling the user to do a browser step ("please sign in",
    "click …", "go to …") it could do itself – without having done anything since their last message?
    Asking for a password / OTP / captcha is fine: only the user knows those. Rejects once."""
    if state.handoff_check_failed or len(state.actions) > state.turn_start:
        return None
    if not HANDOFF.search(question) or USER_ONLY.search(question):
        return None
    state.handoff_check_failed = True
    sign_in = next((line for line in (state.elements or "").splitlines()
                    if re.match(r"^\[\d+\] ", line) and re.search(r"sign ?in|log ?in", line, re.I)), "")
    hint = f" The sign-in link is on this page: {sign_in} – click it." if sign_in else ""
    return HANDOFF_REJECTED.format(hint)
