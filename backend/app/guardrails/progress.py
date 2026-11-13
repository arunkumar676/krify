"""Is the task still moving forward? Stuck, repeat and going-in-circles detection, and the scroll limits."""

import json
import re

from app.config import STRUCTURED_VIEW
from app.context.element_list import layout

# Stuck detector: these always count as progress; clicks/keys count only if the page changed.
PROGRESS_OPS = {"read_text", "find_text", "sheet_query", "read_cells", "go_to_cell", "type_here", "note", "type", "select_option", "open_url", "new_tab"}

IDLE_LIMIT = 5
# Scrolling: only scrolls that showed nothing new count against the page (a long list keeps showing
# new rows), with a hard cap so an endless feed can't eat every step.
MAX_IDLE_SCROLLS = 3
MAX_SCROLLS_PER_PAGE = 12
SCROLL_REFUSED = (
    "NOT scrolled: {} Decide with what you've found: save it with note, or note that it's not on this "
    "site, then continue with your plan (or finish with done). find_text can still jump to a phrase."
)
WRAP_UP_AT = 5  # steps left
WRAP_UP = (
    "⚠️ Only 5 steps left. Finish now: call done with what you found so far (from your notes), "
    "and say clearly what you couldn't find."
)
REPEAT_NOTE = (
    "\nWarning: you've done this same step 3 times and nothing changed. Don't do it again: try a "
    "different way (another element, find_elements, read_text), or call ask_user."
)
LOOP_TRAIL = 8  # pages remembered (after each action) to spot going back and forth

LOOP_NOTE = (
    "\n⚠️ Going in circles: your last clicks keep switching between the same pages ({}). More clicking "
    "won't help. Check the page open NOW: if it already does what the user asked (the page they named is "
    "open, or its menu item shows (current)/(selected)), call done and say what's there – even if it's not "
    "what you expected. Otherwise call plan with a corrected done_when, or ask_user."
)
LOOP_ASK = ("I kept going back and forth between {} without finishing. The page open now is \"{}\". "
            "Is that what you wanted, or what should I do next?")

SAME_READ = ("You already read this – the text is exactly the same as last time. Don't read it again: "
             "save what you need with note, or if it's not there, note \"not found\" and move on with your plan.")

STUCK_NOTE = (
    "\nWarning: the last 5 steps made no progress (only scrolling, switching or clicking without "
    "the page changing). Stop wandering: find_text(\"...\") to jump straight to specific "
    "information anywhere on the page, read_text to see what's on screen, try a different "
    "element, or call ask_user."
)


def check_repeat(state, name, args, page_changed):
    """Repeat detector: the same action 3 times within the last 6 steps, with the page never
    changing (e.g. clicking one spot over and over). Positions within 20px count as the same."""
    clean = {k: (round(v / 20) if k in ("x", "y", "from_x", "from_y", "to_x", "to_y") and isinstance(v, (int, float))
                 else v) for k, v in args.items() if not k.startswith("_")}
    sig = f"{name} {json.dumps(clean, sort_keys=True, default=str)}"
    if page_changed:
        state.recent_steps = []
    state.recent_steps = (state.recent_steps + [sig])[-6:]
    if state.recent_steps.count(sig) >= 3:
        state.recent_steps = []
        return REPEAT_NOTE
    return ""


def scroll_key(state, args):
    """Scrolls are counted per page (and separately for a scrollable box on it)."""
    return state.current_url + (" box" if args.get("element") is not None else "")


def scrolled(state, args, page):
    """After a scroll: did it show anything not seen on this page before? (At the bottom, or going
    back and forth, it didn't – those count toward the idle-scroll limit.)"""
    done = state.scrolls.get(scroll_key(state, args))
    if done is None:
        return
    view = hash(page)  # the whole view: its scroll position and on/off-screen markers change as it moves
    seen = done.setdefault("views", set())
    done["idle"] = done["idle"] + 1 if view in seen else 0
    seen.add(view)


def page_key(state, page):
    """A page's identity for loop detection: URL + which elements it has (typed values, ticks and
    states left out – so filling a form doesn't count as a different page)."""
    if STRUCTURED_VIEW:  # the whole page's labels: the same wherever it's scrolled to
        return hash((state.current_url, frozenset(re.findall(r'^\[\d+\] ([^"\n]*"[^"\n]*")', state.elements or "", re.M))))
    return hash((state.current_url, layout(page)))


def start_trail(state, page):
    state.trail = [(page_key(state, page), state.current_title)]
    state.loop_warnings = 0
    state.loop_question = ""  # set when it's still looping after a warning: stop and ask the user


def check_loop(state, page, changed, dev_mode, trace):
    """After an action. → (new_page, warning). new_page is False when the action led BACK to a
    page seen a few actions ago – that's not progress (it hid going back and forth from the stuck
    and repeat detectors, which only asked "did the page change?"). The warning is given when the
    recent pages repeat in a cycle (A→B→A→B, or A→B→C→A→B→C); the 2nd time, loop_question is set."""
    key = page_key(state, page)
    if dev_mode and trace and changed:
        trace[-1] += f' → "{state.current_title}"'
    if key == state.trail[-1][0]:  # same page (maybe a ticked box or typed text): not a revisit
        return changed, ""
    revisit = any(k == key for k, _ in state.trail[-LOOP_TRAIL:])
    state.trail = (state.trail + [(key, state.current_title)])[-2 * LOOP_TRAIL:]
    keys = [k for k, _ in state.trail]
    for period in (2, 3):
        if len(keys) >= 2 * period and keys[-period:] == keys[-2 * period:-period]:
            titles = list(dict.fromkeys(t or "untitled" for _, t in state.trail[-period:]))
            where = " ↔ ".join(f'"{t}"' for t in titles) if len(titles) > 1 else f'"{titles[0]}" (the same few screens)'
            state.trail = state.trail[-1:]  # a fresh start: it takes another full cycle to warn again
            state.loop_warnings += 1
            if state.loop_warnings >= 2:
                state.loop_question = LOOP_ASK.format(where, state.current_title or state.current_url)
            return False, LOOP_NOTE.format(where)
    return not revisit, ""


def check_progress(state, name, page_changed):
    """Stuck detector: count steps that don't move the task forward."""
    progress = name in PROGRESS_OPS or (
        name in ("click", "drag", "hover", "press_key", "go_back", "scroll") and page_changed
    )
    state.idle_steps = 0 if progress else state.idle_steps + 1
    if state.idle_steps >= IDLE_LIMIT:
        state.idle_steps = 0
        return STUCK_NOTE
    return ""
