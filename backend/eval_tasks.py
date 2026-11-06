"""Scripted evals for the full agent loop (not just raw NLU like try_nlu.py) - a real Agent, a
scripted fake browser (no real browser, no real page), but the real LLM. Catches regressions in
element disambiguation, fact-checking and multi-step memory.

Run against the default model:  uv run python eval_tasks.py
One model:                      uv run python eval_tasks.py --model groq:openai/gpt-oss-120b
Every configured strong model:  uv run python eval_tasks.py --all-models
"""

import argparse
import asyncio
import os
import re

# Eval runs must never touch the real backend/krify.db (task history, learned usage limits) -
# this has to happen before anything imports app.db (app.agent.loop -> app.history -> app.db).
os.environ.setdefault("DATABASE_URL", "sqlite://")

from app.agent.loop import Agent  # noqa: E402
from app.config import MODELS  # noqa: E402

NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _has_number(text):
    return [n for n in NUMBER.findall(text or "") if sum(c.isdigit() for c in n.replace(",", "")) >= 3]


def make_browser(script):
    """script: {op_name: value | callable(args, state) -> value}. Logs every call it answers."""
    calls = []
    state = {}

    async def browser(name, args):
        calls.append((name, dict(args)))
        value = script.get(name)
        if callable(value):
            return value(args, state)
        return value if value is not None else "OK."

    return browser, calls


def shop_script(products, with_buttons=True):
    """A simple listing page: one link per product, optionally followed by an identically
    labelled 'Add to cart' button on every row - the exact ambiguity the duplicate-label hint
    targets. Returns (script_dict, {product_name: (link_id, button_id)})."""
    ids, lines, next_id = {}, ['[1] link "Shop"', '[3] link "Cart (0)"'], 20
    for name, price in products:
        link_id = next_id
        lines.append(f'[{link_id}] link "{name} - ₹{price}"')
        next_id += 1
        btn_id = None
        if with_buttons:
            btn_id = next_id
            lines.append(f'[{btn_id}] button "Add to cart"')
            next_id += 1
        ids[name] = (link_id, btn_id)
    elements = "\n".join(lines)
    read_text = ". ".join(f"{name} ₹{price}" for name, price in products) + "."
    prices = dict(products)

    def describe_element(args, state):
        eid = args.get("element")
        for name, (link_id, btn_id) in ids.items():
            if eid == link_id:
                return f'link "{name} - ₹{prices[name]}"'
            if eid == btn_id:
                return 'button "Add to cart"'
        return ""

    def inspect(args, state):
        ref = args.get("ref")
        for name, (link_id, btn_id) in ids.items():
            if ref in (link_id, btn_id):
                row = f'  [{link_id}] link "{name} - ₹{prices[name]}"'
                if btn_id:
                    row += f'\n  [{btn_id}] button "Add to cart"'
                return f"region\n{row}"
        return "Nothing there."

    def find_elements(args, state):
        q = str(args.get("query", "")).lower()
        hits = [f'[{btn_id}] button "Add to cart" (near {name})'
                for name, (_, btn_id) in ids.items()
                if btn_id and (q in name.lower() or q in ("add to cart", "cart", "buy"))]
        return "\n".join(hits) or "No matches."

    def click(args, state):
        return "Clicked."

    script = {
        "get_page_info": {"url": "https://shop.example/s", "title": "Shop", "tabs": ""},
        "get_elements": elements,
        "read_text": read_text,
        "describe_element": describe_element,
        "inspect": inspect,
        "find_elements": find_elements,
        "click": click,
    }
    return script, ids


def clicked_elements(calls):
    return [args.get("element") for name, args in calls if name == "click"]


# ---------- scenarios ----------

MOUSE_PRODUCTS = [
    ("Logitech M235 Wireless Mouse", 699),
    ("HP X200 Wireless Mouse", 499),
    ("Zebronics Zeb-Transformer Gaming Mouse", 349),
]


def scenario_cheapest_report():
    script, _ = shop_script(MOUSE_PRODUCTS, with_buttons=False)

    def expect(terminal, calls):
        kind, text = terminal
        if kind != "done":
            return False, f"expected done, got {kind!r}"
        if "349" not in (text or ""):
            return False, "didn't name the cheapest price (349)"
        return True, "named the cheapest price"

    return {
        "name": "cheapest_report",
        "task": "Which wireless mouse here is the cheapest, and how much is it?",
        "script": script,
        "expect": expect,
    }


def scenario_duplicate_add_to_cart():
    script, ids = shop_script(MOUSE_PRODUCTS, with_buttons=True)
    correct_btn = ids["Zebronics Zeb-Transformer Gaming Mouse"][1]

    def expect(terminal, calls):
        kind, text = terminal
        clicks = clicked_elements(calls)
        if not clicks:
            return False, "never clicked anything"
        if clicks[-1] != correct_btn:
            return False, f"clicked [{clicks[-1]}], expected the cheapest product's button [{correct_btn}]"
        return True, "clicked the cheapest product's own 'Add to cart' button"

    return {
        "name": "duplicate_add_to_cart",
        "task": "Add the cheapest mouse to the cart.",
        "script": script,
        "expect": expect,
    }


def scenario_ambiguous_ask():
    script = {
        "get_page_info": {"url": "https://shop.example/s", "title": "Shop", "tabs": ""},
        "get_elements": '[1] link "Shop"\n[10] link "Wireless Mouse"\n[11] link "Keyboard"\n[12] link "Webcam"',
        "click": "Clicked.",
    }

    def expect(terminal, calls):
        kind, text = terminal
        if kind == "ask":
            return True, "asked instead of guessing"
        return False, f"expected ask_user, got {kind!r} ({(text or '')[:80]!r})"

    return {
        "name": "ambiguous_ask",
        "task": "Add the best one to the cart.",
        "script": script,
        "expect": expect,
    }


def scenario_multi_page_memory():
    page1 = '[1] link "Shop"\n[20] link "Alpha Mouse - ₹799"\n[30] link "Next page"'
    page2 = '[1] link "Shop"\n[20] link "Beta Mouse - ₹549"'

    def get_elements(args, state):
        return page2 if state.get("page") == 2 else page1

    def get_page_info(args, state):
        page = "p2" if state.get("page") == 2 else "p1"
        return {"url": f"https://shop.example/{page}", "title": "Shop", "tabs": ""}

    def read_text(args, state):
        return "Beta Mouse ₹549." if state.get("page") == 2 else "Alpha Mouse ₹799."

    def click(args, state):
        if args.get("element") == 30:
            state["page"] = 2
        return "Clicked."

    script = {
        "get_elements": get_elements,
        "get_page_info": get_page_info,
        "read_text": read_text,
        "click": click,
    }

    def expect(terminal, calls):
        kind, text = terminal
        if kind != "done":
            return False, f"expected done, got {kind!r}"
        missing = [n for n in ("549", "799") if n not in (text or "")]
        if missing:
            return False, f"final answer lost {missing} from an earlier page"
        return True, "remembered both pages' prices"

    return {
        "name": "multi_page_memory",
        "task": "Compare the mouse on this page with the mouse on the next page - which is cheaper, and by how much?",
        "script": script,
        "expect": expect,
    }


def scenario_fabrication_trap():
    script, _ = shop_script(MOUSE_PRODUCTS, with_buttons=False)

    def expect(terminal, calls):
        kind, text = terminal
        nums = _has_number(text)
        if not nums:
            return True, "no fabricated number"
        if "couldn't confirm" in (text or "").lower():
            return True, "flagged by the fact-check caveat"
        return False, f"invented number(s) with no caveat: {nums}"

    return {
        "name": "fabrication_trap",
        "task": "What's the price of the keyboard?",
        "script": script,
        "expect": expect,
    }


SCENARIOS = [
    scenario_cheapest_report,
    scenario_duplicate_add_to_cart,
    scenario_ambiguous_ask,
    scenario_multi_page_memory,
    scenario_fabrication_trap,
]


async def run_scenario(build, model):
    scenario = build()
    browser, calls = make_browser(scenario["script"])
    transcript = []

    async def ui(kind, text):
        transcript.append((kind, text))

    async def confirm(text):
        return True

    async def plan_review(steps):
        return "approve", steps

    agent = Agent(browser, ui, confirm, plan_review)
    try:
        await agent.run(scenario["task"], model)
    except Exception as err:
        return scenario["name"], False, f"agent raised {err!r}"
    terminal = next(((k, t) for k, t in reversed(transcript) if k in ("done", "ask", "stopped", "error")),
                     ("", ""))
    passed, reason = scenario["expect"](terminal, calls)
    return scenario["name"], passed, reason


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", help="one model id from app.config.MODELS")
    parser.add_argument("--all-models", action="store_true", help="run against every configured strong model")
    args = parser.parse_args()

    if not MODELS:
        print("No models configured (check backend/.env) - nothing to run.")
        return 1

    if args.all_models:
        models = [m for m, info in MODELS.items() if info["tier"] == "strong"]
    elif args.model:
        if args.model not in MODELS:
            print(f"Unknown model {args.model!r}. Configured: {', '.join(MODELS)}")
            return 1
        models = [args.model]
    else:
        models = [next(iter(MODELS))]

    total = 0
    failed = 0
    for model in models:
        print(f"\n== {model} ==")
        for build in SCENARIOS:
            name, passed, reason = await run_scenario(build, model)
            total += 1
            failed += not passed
            print(f"  [{'PASS' if passed else 'FAIL'}] {name}: {reason}")

    print(f"\n{total - failed}/{total} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
