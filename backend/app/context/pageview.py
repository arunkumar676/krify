"""Structured page view: turns the extension's page model (get_page_model, built by content.js
buildPageModel) into what the AI reads, plus the old flat element list the code itself relies on.

The extension always collects the WHOLE page; this module decides how much of it the AI sees:
- Restricted (default): what's on screen, an open dialog, the form being filled; long nav/sidebar
  lists cut short, the footer collapsed; off-screen parts summarised as counts.
- Full (the side panel's Unrestricted switch): everything, with off-screen parts marked.

The flat list (`View.flat`) always holds EVERY collected element in the old `[n] role "label"`
format, so safety.check, the fact check and merge_elements see the whole page, never just the
shortened view the AI got.

Try it on a saved page model:  uv run python -m app.context.pageview fixtures/amazon_search.json [--full]
"""

import json
import re
import sys
from dataclasses import dataclass

MAX_SHOWN = 100  # elements shown in restricted mode (the old flat list's cap)
SECTION_LIMITS = {"nav": 8, "aside": 6, "toolbar": 12, "menubar": 12}  # on-screen elements shown per section
COLLAPSED = {"footer"}  # sections shown only as a count in restricted mode
# Which on-screen elements win when there are more than MAX_SHOWN (lower = first).
PRIORITY = {"dialog": 0, "menu": 0, "form": 1, "search": 1, "main": 2, "page": 2, "region": 2, "header": 3,
            "aside": 4, "nav": 5, "toolbar": 5, "menubar": 5, "footer": 6}
POPUP_KINDS = {"dialog", "menu"}
# Controls worth naming even off screen or past a section's cap: paging and form submit buttons –
# on a long page they're what moves the task on, and a count ("+40 below") hides them.
KEY_CONTROL = re.compile(r'^(button submit|input submit|input image)\b|"\s*(next( page)?|previous( page)?|prev|load more|'
                         r'show more|see more|view more|more results|›|»|‹|«)\s*"', re.I)
MAX_KEY_CONTROLS = 6
MAX_AMBIGUITY_GROUPS = 3
MAX_FORM_REQUIRED = 12  # names listed in the [Form] checklist
MAX_FORM_OPTIONAL = 10
POSITION = {-1: "above the screen", 1: "below the screen", 2: "off to the side"}


@dataclass
class View:
    text: str  # what the AI reads
    flat: str  # every collected element, old format – for safety/fact checks
    status: str  # the page's own status/error message ("" if none)
    hint: str  # warning about identical elements the view can't tell apart ("" if none)
    # An open custom dropdown list: (its field's name, [option labels]), or None. Lets the loop tell
    # the AI when a click closed the list without choosing anything (changes.click_result).
    open_list: tuple | None = None
    # The form being filled (content.js formSummary): {name, marks, fields, submits}, or None.
    form: dict | None = None
    full: bool = False  # Unrestricted mode: nothing was cut (the loop then lifts its own caps too)
    inline_text: bool = False  # the page's own text is already in the view, among the elements (Unrestricted)


@dataclass
class _Entry:
    el: dict
    page: int
    section: tuple  # (page index, section index)
    card: tuple | None  # (page index, card index)
    order: int


def dupes_warning(dupes):
    """[(label, [ids])] → the warning line(s) asking the AI to look closer before clicking."""
    return "\n".join(
        f"⚠️ Same label on {','.join(f'[{i}]' for i in ids)} (\"{label}\") – call inspect ref=<one of "
        "them> to see which is which before clicking."
        for label, ids in dupes[:MAX_AMBIGUITY_GROUPS]
    )


def _scroll(page):
    y, top = page.get("scrollY", 0), page.get("scrollMax", 0)
    if top <= 4:
        return "whole page fits on screen"
    if y <= 4:
        return "at the top, more below"
    if y >= top - 50:
        return "at the bottom"
    return f"{round(100 * y / top)}% down, more above and below"


def _field(f):
    """'Contact [16] ✗ empty' / 'Subject [2] ✓ "Dummy Activity"' for the [Form] checklist."""
    name = f"{f.get('label') or '(no label)'} [{f['id']}]"
    if f.get("kind") == "dropdown":
        name += " (dropdown)"
    if f.get("filled"):
        state = f'✓ "{f["value"]}"' if f.get("value") else "✓"
    else:
        state = "✗ empty"
    if f.get("disabled") and not f.get("filled"):
        state += " (disabled – opens after an earlier field is chosen)"
    if f.get("error"):
        state += f' (error: "{f["error"]}")'
    return f"{name} {state}"


def form_lines(form, full=False):
    """The [Form] checklist: which fields are required (filled ✓ / empty ✗) and which are optional,
    so the AI fills the right ones and knows what's still missing. [] if there's no form."""
    if not form or not form.get("fields"):
        return []
    name = f' "{form["name"]}"' if form.get("name") else ""
    fields = form["fields"]
    cap_required, cap_optional = (None, None) if full else (MAX_FORM_REQUIRED, MAX_FORM_OPTIONAL)
    if not form.get("marks"):
        filled = [f for f in fields if f.get("filled")]
        done = f" Filled: {', '.join(_field(f) for f in filled[:cap_required])}." if filled else ""
        return [f"[Form{name}] It marks no fields as required: fill what the user gave; after saving, "
                f"read ⚠ ALERTS for anything missing.{done}"]
    required = [f for f in fields if f.get("required")]
    optional = [f for f in fields if not f.get("required")]
    more = f" (+{len(required) - cap_required} more)" if cap_required and len(required) > cap_required else ""
    lines = [f"[Form{name}] Required: " + " · ".join(_field(f) for f in required[:cap_required]) + more]
    if optional:
        names = ", ".join(f"{f.get('label') or '(no label)'} [{f['id']}]" + (" ✓" if f.get("filled") else "")
                          for f in optional[:cap_optional])
        extra = f" (+{len(optional) - cap_optional} more)" if cap_optional and len(optional) > cap_optional else ""
        lines.append(f"Optional (fill only if the user gave a value; don't ask about these): {names}{extra}")
    return lines


def missing_required(form):
    """Labels of required fields that are still empty ([] if none, or the form marks nothing)."""
    if not form or not form.get("marks"):
        return []
    return [f.get("label") or f"[{f['id']}]" for f in form.get("fields") or [] if f.get("required") and not f.get("filled")]


def _label(text):
    """'clickable "Amit Verma"' → 'Amit Verma' ('' without a quoted label)."""
    quoted = re.search(r'"([^"]*)"', text or "")
    return quoted.group(1) if quoted else ""


# ---------- one fixed way to write every element ----------
# The extension describes elements in many shapes ('input text "Email" (value: "x")', 'select "Sort"
# = "Featured" (…)', 'div "Agents"', 'clickable "Select a contact"'). The AI gets ONE shape:
#   [n] role "label" = "value" (states)
# with a small fixed set of role words. Only the AI's text changes: View.flat (safety, fact check,
# merge_elements) keeps the extension's own wording.
ROLES = {"link", "button", "input", "textarea", "checkbox", "radio", "toggle", "dropdown", "tab", "menuitem",
         "option", "slider", "row", "group", "clickable", "image", "heading"}
ROLE_MAP = {"select": "dropdown", "combobox": "dropdown", "listbox": "dropdown", "switch": "toggle",
            "searchbox": "input", "spinbutton": "input", "menuitemcheckbox": "menuitem", "menuitemradio": "menuitem",
            "img": "image", "a": "link"}
BUTTON_INPUTS = {"submit", "button", "reset", "image"}
PLAIN_INPUTS = {"text", "search", ""}  # no need to say the type of an ordinary text box
KEEP_AS_IS = ("scrollable box ", "this embedded page")  # Krify's own lines: the AI is told how to use them
_DESCRIPTION = re.compile(r'^(?P<head>[^"\n]*?)\s*"(?P<label>[^"\n]*)"(?P<rest>.*)$', re.S)
_VALUE = re.compile(r'^\(value: "(?P<value>.*)"\)(?P<rest>.*)$', re.S)
HOVER = " (shows on hover)"
LONG_LABEL = 100  # a label longer than this (a clickable card's whole text) is cut to SHORT_LABEL –
SHORT_LABEL = 80  # in Unrestricted mode its full text is shown as a text block next to it (content.js)


def _tidy(text):
    """'input text "Search" (value: "mouse")' → ('input "Search" = "mouse"', []), plus the states the
    description itself carried ('submit', 'email', 'hover', 'checked'). Anything unexpected is
    returned unchanged."""
    extra = []
    if HOVER in text:
        text = text.replace(HOVER, "")
        extra.append("hover")
    match = _DESCRIPTION.match(text)
    if not match or text.startswith(KEEP_AS_IS):
        return text, extra
    words, label, rest = match.group("head").split(), match.group("label"), match.group("rest").strip()
    if not words:
        return text, extra
    role = words[0]
    kind = words[1] if len(words) > 1 else ""
    if role == "input":
        if kind in ("checkbox", "radio"):
            role = kind
        elif kind in BUTTON_INPUTS:
            role = "button"
            if kind == "submit":
                extra.append("submit")
        elif kind not in PLAIN_INPUTS:
            extra.append(kind)  # email, password, number, date…: tells the AI what to type
    elif role == "button" and kind in ("submit", "reset"):
        extra.append(kind)
    elif role not in ROLES:
        role = ROLE_MAP.get(role, "clickable")  # div, span, li…: something that reacts to a click
    value = ""
    if found := _VALUE.match(rest):
        value, rest = found.group("value"), found.group("rest").strip()
    for word in ("(empty)", "(unchecked)", "(checked)"):
        if rest.startswith(word):
            rest = rest[len(word):].strip()
            if word == "(checked)":
                extra.append("checked")
    if len(label) > LONG_LABEL:
        label = label[:SHORT_LABEL].rstrip() + "…"
    line = f'{role} "{label}"'
    if value:
        line += f' = "{value}"'
    if rest:
        line += f" {rest}"  # a dropdown's  = "choice" (options: …)
    return line, extra


def _states(states):
    """The extension's state words → short fixed ones: 'collapsed, opens menu' → 'menu▸',
    'expanded' → 'open', 'unchecked' dropped (only 'checked' is shown)."""
    states = list(states or [])
    opener = next((s[len("opens "):] for s in states if s.startswith("opens ")), "")
    is_open = any(s == "expanded" or s.startswith("expanded –") for s in states)
    out = []
    for s in states:
        if s in ("expanded", "collapsed", "unchecked") or s.startswith(("opens ", "expanded –")):
            continue
        out.append("read-only" if s == "readonly" else s)
    if opener and opener != "list":
        out.insert(0, f"{opener} open" if is_open else f"{opener}▸")
    elif is_open:
        out.insert(0, "list open" if opener == "list" else "open")
    elif "collapsed" in states and not opener:
        out.insert(0, "closed")
    return out


_ALWAYS_SAY = ("call ", "email ", "downloads ", "WhatsApp", "jumps ")
_URL_WORDS = {"www", "com", "html", "htm", "php", "aspx", "page", "the", "and", "new", "tab"}


def _destination(text, go):
    """Where a link goes ('→ call +1 212…', '→ linkedin.com', '→ /services/') – only when its label
    doesn't already say it ("Blog" → /blog/ needs nothing; "call-us" → a phone number does)."""
    if not go:
        return ""
    if go.startswith(_ALWAYS_SAY):
        return go
    label = re.sub(r"[^a-z0-9]+", " ", _label(text).lower())
    if go.startswith("home page"):
        return "" if "home" in label.split() else go
    words = [w for w in re.split(r"[^a-z0-9]+", go.lower()) if len(w) > 2 and w not in _URL_WORDS]
    return "" if words and all(w in label for w in words) else go


def _element_line(el, in_card=False):
    text, extra = _tidy(el["text"])
    states = []
    for s in extra + _states(el.get("states")):
        if s not in states:
            states.append(s)
    if text.startswith("clickable ") and ({"open", "closed"} & set(states)):
        text = "group " + text[len("clickable "):]  # opens/closes a part of the page (sidebar group, accordion)
    elif text.startswith("tab "):
        states = [s for s in states if s not in ("open", "closed")]  # "selected" says which tab is showing
    elif text.startswith("link ") and ({"open", "closed"} & set(states)):
        # A menu link that also opens a sub-menu ("Our Services ▾"): say so, not "closed".
        states = ["submenu open" if s == "open" else "submenu▸" if s == "closed" else s for s in states]
    if el.get("of") and not in_card:  # a card's ▸ line already says what its buttons belong to
        states.append(f"of: {el['of']}")
    line = f"[{el['id']}] {text}"
    if states:
        line += f" ({', '.join(states)})"
    if go := _destination(text, el.get("go")):
        line += f" → {go}"
    if el.get("col"):
        line += f' · column "{el["col"]}"'
    if el.get("hint"):
        line += f' · hint: "{el["hint"]}"'
    return line


def _card_line(card, pos):
    if not card or not card.get("summarised"):
        return f"▸ (item {POSITION.get(pos, 'off screen')})"
    badges = list(card.get("badges") or [])
    parts = []
    if "Sponsored" in badges:
        badges.remove("Sponsored")
        parts.append("Sponsored")
    parts.append(f'"{card.get("title", "")}"')
    parts += [card[key] for key in ("price", "rating") if card.get(key)]
    return "▸ " + " · ".join(parts + badges)


def flat_lines(model):
    """Every collected element in the old get_elements format: `[n] role "label" (...)`."""
    parts = []
    for i, page in enumerate(model.get("pages") or []):
        lines = []
        if page.get("status"):
            lines.append(f"[Status] {page['status']}")
        if page.get("popup"):
            lines.append("(an open menu/dialog is listed first)")
        lines += [f"[{el['id']}] {el['text']}" for el in page.get("elements") or []]
        if page.get("partial"):
            lines.append("(more elements not listed – the page is very large)")
        info = page.get("page") or {}
        if i == 0 and info.get("scrollMax", 0) - info.get("scrollY", 0) > 50:
            lines.append("(more content below – scroll down to see it)")
        if not lines:
            continue
        text = "\n".join(lines)
        parts.append(f'--- inside embedded page "{page.get("frame")}" ---\n{text}' if i else text)
    return "\n".join(parts) or "(no interactive elements visible)"


def _entries(pages):
    entries = []
    for p, page in enumerate(pages):
        for el in page.get("elements") or []:
            card = (p, el["c"]) if el.get("c", -1) >= 0 else None
            entries.append(_Entry(el, p, (p, el["s"]), card, len(entries)))
    return entries


def _choose(entries, sections, focused, full):
    """Which elements the AI sees. Full: all. Restricted: popups, the focused form, then on-screen
    elements by section priority, up to MAX_SHOWN."""
    if full:
        return {e.order for e in entries}
    focus_sections = {e.section for e in entries if e.el["id"] in focused
                      and "form" in sections[e.section]["kinds"]}
    must, candidates, per_section, key_controls = [], [], {}, []
    for e in entries:
        sec = sections[e.section]
        key = bool(KEY_CONTROL.search(e.el["text"]))
        if POPUP_KINDS & set(sec["kinds"]) or e.el["id"] in focused or e.section in focus_sections:
            must.append(e)
        elif e.el.get("pos", 0) == 0 and not COLLAPSED & set(sec["kinds"]):
            per_section[e.section] = per_section.get(e.section, 0) + 1
            limit = SECTION_LIMITS.get(sec["kind"])
            if limit is None or per_section[e.section] <= limit or key:
                candidates.append(e)
        elif key:
            key_controls.append(e)  # off screen, or in the footer: named anyway (marked where it is)
    shown = must[:MAX_SHOWN]
    shown += key_controls[:MAX_KEY_CONTROLS]
    candidates.sort(key=lambda e: (PRIORITY.get(sections[e.section]["kind"], 2), e.order))
    shown += candidates[:max(0, MAX_SHOWN - len(shown))]
    return {e.order for e in shown}


def _ambiguity(entries, shown):
    """Shown elements with the same description and no item line to tell them apart: twice inside
    one item, or several with no item at all. (Identical buttons in different items are fine –
    the ▸ line above each says which is which – and so are ones whose states differ, e.g. a
    dropdown field "expanded" and its list's "placeholder" option with the same text.)"""
    groups = {}
    for e in entries:
        if e.order in shown and '"' in e.el["text"]:
            key = (e.el["text"], tuple(e.el.get("states") or ()), e.card, e.el.get("of"), e.el.get("go"))
            groups.setdefault(key, []).append(e.el["id"])
    dupes = [(key[0].split('"')[1], ids) for key, ids in groups.items() if len(ids) > 1]
    return dupes_warning(dupes) if dupes else ""


# ---------- the page in design order ----------
# The page is shown the way a person sees it: an open pop-up first (it sits on top of the page),
# then top → bottom, left → right: top bar, nav, left sidebar, main, right column, embedded pages,
# footer. Each region is written whole (column by column), never mixed row by row.
REGION_ORDER = ["TOP BAR", "NAV", "LEFT SIDEBAR", "SIDEBAR", "MAIN", "RIGHT COLUMN", "FLOATING", "EMBEDDED", "FOOTER"]
SIDE_SHARE = 0.35  # a section within the left/right 35% of the window is a sidebar/column
TOP_BAR_Y = 150  # a nav/search ending this close to the top of the page belongs to the top bar
MAX_ALERTS = 3
LANDMARKS = {"Header", "Main", "Nav", "Sidebar", "Footer", "Section", "Page"}  # region names say these already


def _region(sec, page_index, width):
    """Which part of the page layout a section belongs to (see REGION_ORDER). Uses the page's own
    landmarks (header, nav, main, aside, footer) and, when the extension sent it, where the section
    sits; without a position nothing is guessed (NAV / SIDEBAR instead of left/right)."""
    if page_index:
        return "EMBEDDED"
    kinds = sec.get("kinds") or ["page"]
    outer = kinds[0]
    box = sec.get("box")
    side = ""
    if box and width:
        if box[2] <= width * SIDE_SHARE:
            side = "LEFT SIDEBAR"
        elif box[0] >= width * (1 - SIDE_SHARE):
            side = "RIGHT COLUMN"
    if outer == "header":
        return "TOP BAR"
    if outer == "footer":
        return "FOOTER"
    if outer in ("nav", "toolbar", "menubar", "aside"):
        if side:
            return side
        if box and box[3] <= TOP_BAR_Y and outer != "aside":
            return "TOP BAR"
        return "SIDEBAR" if outer == "aside" else "NAV"
    if outer == "search" and box and box[3] <= TOP_BAR_Y:
        return "TOP BAR"
    return "MAIN"


def _sub_name(name):
    """A section's name without what its region already says: 'Header › Nav "Shortcuts"' → 'Shortcuts',
    'Main › "Results"' → 'Results', 'Embedded page "X" › Form "Sign in"' → 'Form Sign in'."""
    parts = []
    for part in name.split(" › "):
        if part.startswith('Embedded page "') or part in LANDMARKS:
            continue
        word, _, rest = part.partition(' "')
        parts.append(rest if rest and word in LANDMARKS else part)
    return " › ".join(parts).replace('"', "")


def _counts(hidden):
    """'2 below the screen, 1 above the screen' for elements left out of the view."""
    cut = sum(1 for e in hidden if e.el.get("pos", 0) == 0)
    parts = [f"{cut} more on screen"] if cut else []
    parts += [f"{n} {POSITION[pos]}" for pos in POSITION if (n := sum(1 for e in hidden if e.el.get("pos") == pos))]
    return ", ".join(parts)


# Text that says nothing new: screen-reader helpers, a strip of small numbers (slider dots "1 2 3 4",
# animated counters), or a line made only of element labels ("Facebook Linkedin-in X-twitter").
_NUMBERS_ONLY = re.compile(r"^(\d{1,2}\s+){2,}\d{1,2}$")


def _only_labels(text, labels):
    """True if `text` (lower case) is nothing but element labels (longest first) and punctuation."""
    rest = text
    for label in labels:
        if label in rest:
            rest = rest.replace(label, " ")
    return not re.search(r"\w", rest)


def _junk(text, said, labels):
    plain = text.strip()
    low = plain.lower()
    return (not plain or low in said or bool(_SCREEN_READER.match(plain)) or bool(_NUMBERS_ONLY.match(plain))
            or _only_labels(low, labels))


def _section_lines(sec, group, shown, cards, sub, texts=()):
    """One section, in place: its heading line, then its elements and (Unrestricted mode) its text
    blocks together in page order – cards with their own elements and text indented – or a one-line
    note when nothing of it is shown."""
    visible = [e for e in group if e.order in shown]
    hidden = [e for e in group if e.order not in shown]
    if not visible and not texts:
        where = _counts(hidden)
        how = "find_elements" if all(e.el.get("pos", 0) == 0 for e in hidden) else "scroll there or use find_elements"
        return [f"## {sub} ({where} – {how})" if sub else f"({where} – {how})"]
    lines = []
    header = f"## {sub}" if sub else ""
    items = len({e.card for e in group if e.card})
    if items >= 3:
        header = f"{header or '##'} ({items} items)"
    if header:
        lines.append(header)
    state = {"card": None, "pos": 0, "card_line": ""}
    # A clickable table row is its own card: its `row "…"` line is the ▸ line, with the row's own
    # buttons/links indented under it (a card summary would repeat the same cells).
    row_ids = {cards[e.card]["row"] for e in visible if e.card and cards.get(e.card, {}).get("row")}
    rows = {e.card: e for e in visible if e.el["id"] in row_ids}

    def enter(card, pos):
        if pos != state["pos"]:
            lines.append(f"-- {POSITION.get(pos, 'on screen')} --")
            state["pos"] = pos
        if card != state["card"]:
            state["card"] = card
            state["card_line"] = ""
            if card in rows:
                state["card_line"] = "▸ " + _element_line(rows[card].el)
            elif card:
                state["card_line"] = _card_line(cards.get(card), pos)
            if state["card_line"]:
                lines.append(state["card_line"])

    def say(t):
        enter(t["card"], t.get("pos", 0))
        if t["t"].lower() in state["card_line"].lower():  # the ▸ line already has it (title, price)
            return
        lines.append(("  " if t["card"] else "") + t["t"])

    def show(e):
        enter(e.card, e.el.get("pos", 0))
        if not (e.card in rows and e is rows[e.card]):
            lines.append(("  " if e.card else "") + _element_line(e.el, bool(e.card)))

    if texts and all(t.get("k") for t in texts) and all(e.el.get("k") for e in visible):
        # Box by box: side-by-side boxes (columns, stat boxes, cards in a row) one at a time, each
        # top to bottom – so "20+" stays next to "years of experience", not next to "1500+".
        items = [(t["k"], 1, n, t) for n, t in enumerate(texts)] + [(e.el["k"], 0, n, e) for n, e in enumerate(visible)]
        for _, is_text, _, item in sorted(items, key=lambda it: (it[0], it[1], it[2])):
            if is_text:
                say(item)
            else:
                show(item)
    else:
        texts = sorted(texts, key=lambda t: (t["y"], t["x"]))
        i = 0
        for e in visible:
            if (y := e.el.get("y")) is not None:  # the page's text above this element comes first
                while i < len(texts) and (texts[i]["y"], texts[i]["x"]) < (y, e.el.get("x", 0)):
                    say(texts[i])
                    i += 1
            show(e)
        for t in texts[i:]:
            say(t)
    if hidden:
        how = "use find_elements" if all(e.el.get("pos", 0) == 0 for e in hidden) else "scroll there or use find_elements"
        lines.append(f"(+{len(hidden)} more here: {_counts(hidden)} – {how})")
    return lines


def _row_counts(page):
    """The site's repeated rows, counted exactly – only for real lists (results, table rows), not
    form fields or footer lines that merely look alike."""
    info = page.get("page") or {}
    listy = info.get("type") in ("search results", "list")
    real = [c for c in page.get("counts") or []
            if (listy or " table row" in f" {c}") and (m := _ROW_COUNT.match(c))
            and len(re.sub(r"\W", "", m.group(1))) > 2]
    if not real:
        return ""
    return (f"[Exact counts of repeated rows loaded on this page so far: {'; '.join(real)}. Use these, don't count by "
            "eye. Rows on other pages, behind 'Load more', or not loaded yet (lists that fill in as you scroll) are "
            "not included.]")


def render(model):
    """→ View: the outline the AI reads, the flat list, the status line and an ambiguity hint."""
    pages = model.get("pages") or []
    full = bool(model.get("full"))
    if not pages:
        return View("(no interactive elements visible)", "(no interactive elements visible)", "", "", None)
    sections, cards = {}, {}
    for p, page in enumerate(pages):
        prefix = f'Embedded page "{page.get("frame")}" › ' if p else ""
        for s, sec in enumerate(page.get("sections") or []):
            sections[(p, s)] = {**sec, "name": prefix + sec["name"]}
        for c, card in enumerate(page.get("cards") or []):
            cards[(p, c)] = card
    entries = _entries(pages)
    focused = {page["focused"] for page in pages if page.get("focused")}
    shown = _choose(entries, sections, focused, full)

    info = pages[0].get("page") or {}
    head = [x for x in (info.get("type"), f'"{info["h1"]}"' if info.get("h1") else "", info.get("results"),
                        _scroll(info)) if x]
    if focused:
        head.append("keyboard in " + " ".join(f"[{i}]" for i in sorted(focused)))
    lines = ["PAGE  " + " · ".join(head)]
    statuses = [page["status"] for page in pages if page.get("status")]
    if statuses:  # one line at the top, so the AI sees at once whether its last action worked
        alerts = [a for status in statuses for a in status.split(" | ") if a]
        more = f" | +{len(alerts) - MAX_ALERTS} more" if len(alerts) > MAX_ALERTS else ""
        lines.append("⚠ ALERTS  " + " | ".join(alerts[:MAX_ALERTS]) + more)
    form = next((page["form"] for page in pages if page.get("form")), None)
    lines += form_lines(form, full)

    # Unrestricted mode: the page's own text blocks, each in its section, minus what's already said
    # (element labels, headings, the main heading, alerts) and junk.
    # (A label longer than LONG_LABEL is cut in its element line, so its full text still counts as new.)
    labels = sorted({lab for e in entries if 1 < len(lab := _label(e.el["text"]).strip().lower()) <= LONG_LABEL},
                    key=len, reverse=True)
    said = set(labels) | {_sub_name(sec["name"]).lower() for sec in sections.values()} | {info.get("h1", "").lower()}
    said |= {a.lower() for status in statuses for a in status.split(" | ")}
    texts_by_section = {}
    for p, page in enumerate(pages):
        for t in page.get("texts") or []:
            if not _junk(t.get("t", ""), said, labels):
                card = (p, t["c"]) if t.get("c", -1) >= 0 else None
                texts_by_section.setdefault((p, t["s"]), []).append({**t, "card": card})

    by_section, floating = {}, []
    for e in entries:
        if e.el.get("float") and not POPUP_KINDS & set(sections[e.section].get("kinds") or ()):
            floating.append(e)  # a chat bubble / "Request a quote" button that stays put: shown on its own
        else:
            by_section.setdefault(e.section, []).append(e)

    def top_of(key):
        ys = [e.el["y"] for e in by_section.get(key, []) if e.el.get("y") is not None]
        ys += [t["y"] for t in texts_by_section.get(key, [])]
        return (key[0], not ys, min(ys) if ys else 0, key[1])

    # An open dropdown list goes first: its options are numbered after everything else (so opening
    # it doesn't shift the form's numbers) but they're what the AI should pick from next. The rest:
    # in page order (top to bottom), from where their elements and text sit.
    keys = sorted(set(by_section) | set(texts_by_section), key=lambda k: (not sections[k].get("list"), top_of(k)))
    ordered = [(key, by_section.get(key, [])) for key in keys]
    open_list = next(((sections[key]["name"], [_label(e.el["text"]) for e in group  # options, not its search box
                                                if not e.el["text"].startswith(("input", "textarea"))])
                      for key, group in ordered if sections[key].get("list")), None)

    # Pop-ups sit on top of the page, so they come first; then the regions in design order.
    width = info.get("width") or 0
    popups, regions = [], {}
    for key, group in ordered:
        sec = sections[key]
        if POPUP_KINDS & set(sec.get("kinds") or ()):
            popups.append((key, group))
        else:
            region = _region(sec, key[0], width)
            regions.setdefault((REGION_ORDER.index(region), key[0] if region == "EMBEDDED" else 0, region), []).append(
                (key, group))
    if floating:
        regions[(REGION_ORDER.index("FLOATING"), 0, "FLOATING")] = [(None, floating)]
    modal = any(sections[key].get("modal") for key, _ in popups)

    behind = set()  # elements shown in no detail: behind a pop-up that blocks the page
    last_popup = None
    for key, group in popups:
        sec = sections[key]
        root, _, inner = sec["name"].partition(" › ")  # 'Dialog "Sign in" › Form': one title per pop-up
        if root != last_popup:
            last_popup = root
            kind = next(k for k in reversed(sec["kinds"]) if k in POPUP_KINDS)
            if sec.get("list"):
                title = f"🔲 DROPDOWN LIST (open) – {root}: click one of these options, or select_option on its field"
            elif sec.get("modal"):
                title = f"🔲 POP-UP (covers the page – deal with it first) – {root}"
            else:
                title = f"🔲 OPEN {kind.upper()} – {root}"
            lines.append(title)
        lines += _section_lines(sec, group, shown, cards, _sub_name(inner) if inner else "",
                                texts_by_section.get(key, ()))
    for (_, page_index, region), members in sorted(regions.items(), key=lambda item: item[0]):
        name = f'EMBEDDED PAGE "{pages[page_index].get("frame")}"' if region == "EMBEDDED" else region
        group_all = [e for _, group in members for e in group]
        has_text = any(texts_by_section.get(key) for key, _ in members if key)
        # Behind a blocking pop-up: one line (the AI can't use it until the pop-up is closed) – except
        # embedded pages (often the pop-up's own content) and the part where the keyboard is.
        if modal and region != "EMBEDDED" and not any(e.el["id"] in focused for e in group_all):
            behind |= {e.order for e in group_all}
            headings = [s for key, _ in members if key and (s := _sub_name(sections[key]["name"]))]
            about = f" · {', '.join(headings[:4])}" if headings else ""
            count = f"{len(group_all)} element{'s' if len(group_all) != 1 else ''}"
            lines.append(f"┌ {name} (behind the pop-up – {count}{about})")
            continue
        if not has_text and not any(e.order in shown for e in group_all):  # nothing shown: one line, in its place
            lines.append(f"┌ {name} ({_counts(group_all)} – scroll there or use find_elements)")
            continue
        if region == "FLOATING":
            lines.append("┌ FLOATING (buttons that stay on screen while you scroll)")
            lines += _section_lines({}, floating, shown, cards, "")
            continue
        lines.append(f"┌ {name}")
        for key, group in members:
            lines += _section_lines(sections[key], group, shown, cards, _sub_name(sections[key]["name"]),
                                    texts_by_section.get(key, ()))
    if full and (counts := _row_counts(pages[0])):
        lines.append(counts)
    if any(page.get("partial") for page in pages):
        lines.append("(very large page: far-off parts weren't listed – scroll there to see them)")
    return View("\n".join(lines), flat_lines(model), statuses[0] if statuses else "",
                _ambiguity(entries, shown - behind), open_list, form, full,
                inline_text=any(page.get("texts") for page in pages))


# ---------- Unrestricted mode: the page's text, without what the view already says ----------
_QUOTED = re.compile(r'"([^"\n]+)"')
_SCREEN_READER = re.compile(r"^(skip to (the )?(main )?content|skip to main|skip navigation|jump to (main )?content|"
                            r"keyboard shortcut:.*|press .* to (open|search).*)$", re.I)
_SKIP_LINK = re.compile(r"^(skip to (the )?(main )?content|skip navigation|jump to (main )?content)\b", re.I)
_DIGIT = re.compile(r"^\d$")
_ROW_COUNTS = re.compile(r"^\[Exact counts of repeated rows loaded on this page so far: (.*?)\. (Use these.*)$")
_ROW_COUNT = re.compile(r'\d+ (?:table )?rows? like "([^"]*)"')
DIGIT_RUN = 3  # 3+ lines of one digit each in a row: an animated counter ("0 1 2 … 9"), not content


def clean_page_text(text, view_text):
    """The [Page text] for Unrestricted mode, without what's already in the view or means nothing:
    lines that are just an element's label ("Home", "Inbox", "Create"), screen-reader helpers
    ("Skip to main content"), animated digit strips, and "rows like" counts of junk ("10 rows like "0"")."""
    said = {q.strip().lower() for q in _QUOTED.findall(view_text or "")}
    labels = sorted((q for q in said if len(q) > 1), key=len, reverse=True)
    lines = (text or "").split("\n")
    digits = [bool(_DIGIT.match(line.strip())) for line in lines]
    keep = []
    i = 0
    while i < len(lines):
        if digits[i]:  # a run of single digits: keep it only if it's short (a real "3")
            j = i
            while j < len(lines) and digits[j]:
                j += 1
            if j - i < DIGIT_RUN:
                keep += lines[i:j]
            i = j
            continue
        line = lines[i]
        i += 1
        plain = line.strip()
        if helper := _SKIP_LINK.match(plain):  # "Skip to main content Create Search": drop the helper part
            plain = line = plain[helper.end():].strip()
        if _junk(plain, said, labels):
            continue
        if counts := _ROW_COUNTS.match(plain):
            real = [c for c in counts.group(1).split("; ")
                    if (m := _ROW_COUNT.match(c)) and len(re.sub(r"\W", "", m.group(1))) > 2
                    and m.group(1).strip().lower() not in said]
            if real:
                keep.append(f"[Exact counts of repeated rows loaded on this page so far: {'; '.join(real)}. "
                            f"{counts.group(2)}")
            continue
        keep.append(line)
    return "\n".join(keep).strip()


if __name__ == "__main__":
    with open(sys.argv[1], encoding="utf-8") as f:
        data = json.load(f)
    if "--full" in sys.argv:
        data["full"] = True
    view = render(data)
    print(view.text)
    if view.hint:
        print(view.hint)
    if "--flat" in sys.argv:
        print("\n--- flat (for safety/fact checks) ---\n" + view.flat)
