"""Fact check: numbers and names in an answer must have been seen on a page read this task (or be shown
arithmetic over numbers that were), and a priced answer must name the product asked for."""

import ast
import re

WRONG_PRODUCT = (
    "NOT saved: the user asked for {} but your note doesn't say it, so this may be a different product "
    "(iPhone 16 is not iPhone 16e; Sponsored results are often other models). Check the product name on "
    "the page. Find the right product, or if this really is it, call note again with its full name."
)
PRODUCT_REJECTED = (
    "REJECTED – not sent to the user. The user asked for {} but your answer doesn't say it. Check each "
    "price is for exactly that product (e.g. iPhone 17e or 16 is not 16e), then call done again naming "
    "the exact product (model + storage) for each price."
)
WRONG_PAGE = ("\n⚠️ This page's title doesn't mention {} – it may be a different product than the user "
              "asked for. Check the product name before using any price from it.")

_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def missing_words(task, text):
    """Model words from the task (letters + digits: 16e, s24, 256gb) that the text doesn't mention."""
    flat = re.sub(r"[^a-z0-9]", "", text.lower())  # "256 GB", "Apple-iPhone-17e" -> "256gb", "appleiphone17e"
    words = {w for w in re.findall(r"[a-z0-9]+", task.lower()) if re.search(r"\d", w) and re.search(r"[a-z]", w)}
    return sorted(w for w in words if w not in flat)


def missing_models(task, note):
    """Model words missing from a note or answer that has a price – a sign the price is for a
    different product."""
    return missing_words(task, note) if re.search(r"\d{3}|₹|\$", note) else []


def joined_pages(seen):
    """Everything read this task, as one string, thousands separators stripped (so ₹69,900
    matches 69900)."""
    return re.sub(r"(?<=\d),(?=\d)", "", "\n".join(seen))


def is_seen(number, pages):
    """Is `number` (plain digits, no thousands separators) really in `pages` – not just a
    fragment of a bigger number (so "900" doesn't match inside "69900")."""
    return bool(re.search(rf"(?<![\d.]){re.escape(str(number))}(?![\d])", pages))


def unverified_numbers(text, seen):
    """Numbers with 3+ digits in `text` (prices, years…) that appear nowhere in `seen`.
    Thousands separators are ignored, so ₹69,900 matches 69900."""
    pages = joined_pages(seen)
    missing = []
    for raw in _NUMBER.findall(text or ""):
        number = raw.replace(",", "").rstrip(".")
        if sum(c.isdigit() for c in number) < 3 or raw in missing:
            continue
        if not is_seen(number, pages):
            missing.append(raw.rstrip(".,"))
    return missing


# A derived number (a count/total/difference computed from numbers actually read) is let through
# the fact check if the summary SHOWS the arithmetic and every number it used was really seen –
# instead of requiring the result itself to appear verbatim, which a calculation never will.
_TOKEN = re.compile(r"\d[\d,]*(?:\.\d+)?|[+\-*/()=×]")
_SAFE_OPS = {ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b,
             ast.Mult: lambda a, b: a * b, ast.Div: lambda a, b: a / b if b else None}


def _tokens(chunk):
    """`chunk` reduced to just its numbers and + - * / ( ) = × (dropping words between them, e.g.
    "12 pages × 20 rows + 17 on the last page = 257" reads as "12 × 20 + 17 = 257") – joined with
    spaces, not glued together, so two numbers with only a dropped word between them (no real
    operator) fail to parse as arithmetic instead of silently becoming one wrong number."""
    return " ".join(_TOKEN.findall(chunk))


def _candidate_expressions(text):
    """Where the summary most likely shows its working: inside parentheses (the shape the prompt
    asks for, and naturally self-contained even next to an earlier bare mention of the same
    number, e.g. "There are 257 users (12 × 20 + 17 = 257)."), or – with no parentheses – a whole
    sentence, so "12 × 20 + 17 = 257 users total." still works on its own."""
    text = text or ""
    for group in re.findall(r"\(([^()]*)\)", text):
        tokens = _tokens(group)
        if tokens:
            yield tokens
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", re.sub(r"\([^()]*\)", " ", text)):
        tokens = _tokens(sentence)
        if tokens:
            yield tokens


def _safe_eval(expr):
    """Evaluate a plain arithmetic expression (digits, + - * / and parens only; no eval()/exec())
    and return (value, operands), or None if it isn't valid simple arithmetic."""
    expr = expr.replace(",", "").replace("×", "*").strip()
    if not expr or not re.fullmatch(r"[\d.\s+\-*/()]+", expr):
        return None
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError:
        return None
    operands = []

    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            operands.append(node.value)
            return node.value
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            value = ev(node.operand)
            return None if value is None else -value
        if isinstance(node, ast.BinOp) and type(node.op) in _SAFE_OPS:
            left, right = ev(node.left), ev(node.right)
            return None if left is None or right is None else _SAFE_OPS[type(node.op)](left, right)
        return None  # anything else (a name, a call, …) – not plain arithmetic, reject

    value = ev(tree)
    return (value, operands) if value is not None else None


def explained_by_calculation(raw_number, text, seen):
    """True if `raw_number` is the result of arithmetic shown in `text`, using only numbers that
    were actually seen this task."""
    target = float(raw_number.replace(",", ""))
    pages = joined_pages(seen)
    for expr in _candidate_expressions(text):
        result = _safe_eval(expr.split("=")[0])
        if not result:
            continue
        value, operands = result
        if abs(value - target) > 0.01:
            continue
        if all(is_seen(int(o) if float(o).is_integer() else o, pages) for o in operands):
            return True
    return False


def unexplained_numbers(text, seen):
    """unverified_numbers(), minus any that the summary itself derives (and shows the working for)
    from numbers that were really seen – e.g. a page-count × page-size total."""
    return [n for n in unverified_numbers(text, seen) if not explained_by_calculation(n, text, seen)]


# Proper-noun-ish phrases: quoted text, or 2+ consecutive Capitalized Words (product/brand names).
_PHRASE = re.compile(r'"([^"]{3,60})"|\b[A-Z][\w&-]*(?:\s+[A-Z][\w&-]*)+\b')
PHRASE_STOPWORDS = {
    "the", "and", "for", "with", "your", "you", "this", "that", "add", "cart", "page", "shop",
    "task", "plan", "notes", "current", "elements", "click", "done", "yes", "no",
}


def unverified_phrases(text, seen):
    """Proper-noun-ish phrases in `text` whose significant words don't all appear anywhere in
    `seen`. A token-level match (not exact substring, unlike unverified_numbers) so rewording
    doesn't false-positive, but an outright invented product/brand name still gets caught."""
    pages = "\n".join(seen).lower()
    missing = []
    for match in _PHRASE.finditer(text or ""):
        phrase = (match.group(1) or match.group(0)).strip()
        words = [w for w in re.findall(r"[A-Za-z]+", phrase.lower()) if len(w) > 2 and w not in PHRASE_STOPWORDS]
        if words and not all(w in pages for w in words) and phrase not in missing:
            missing.append(phrase)
    return missing


def check_facts(state, summary):
    """Fact check before 'done': every number (3+ digits) and proper-noun-ish name in the
    answer must have appeared on a page Krify read this task, or (for a number) be arithmetic
    shown in the summary over numbers that were; a priced answer must also name the product
    asked for (16e, 256gb). Returns a rejection message, or None if the answer may go out."""
    if (models := missing_models(state.product_text, summary)) and not state.product_check_failed:
        state.product_check_failed = True
        return PRODUCT_REJECTED.format(", ".join(models))
    numbers = unexplained_numbers(summary, state.seen)
    phrases = unverified_phrases(summary, state.seen)
    if not numbers and not phrases:
        return None
    if state.fact_check_failed:  # second time: let it through, but tell the user honestly
        return None
    state.fact_check_failed = True
    parts = []
    if numbers:
        parts.append(f"numbers ({', '.join(numbers)})")
    if phrases:
        parts.append(f"names ({', '.join(phrases)})")
    return (
        f"REJECTED – not sent to the user. These {' and '.join(parts)} in your answer were not seen on any "
        "page you read (and aren't arithmetic shown over numbers that were). Use read_text to find the real "
        "values, report only what you actually saw or correctly computed from it (no guesses or made-up "
        "names), then call done again."
    )
