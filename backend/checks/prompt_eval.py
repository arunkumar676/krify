"""Prompt safety test: does the AI still pick the right first step after the rules/tools change?

Each case = a saved page (fixtures/) + a task + the acceptable first steps. Calls the REAL model
(uses API tokens: ~4-7k per case), so it is not part of checks/run.py.

  uv run python checks/prompt_eval.py --record      # today's rules: save checks/prompt_eval_baseline.json
  uv run python checks/prompt_eval.py               # after a change: score + compare with the baseline
  uv run python checks/prompt_eval.py --only 1,7    # just some cases
  uv run python checks/prompt_eval.py --runs 2      # each case twice (models are not fully repeatable)
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.context import pageview  # noqa: E402
from app.context.prompt import system_prompt  # noqa: E402
from app.llm.client import ask_ai  # noqa: E402
from app.tools.schemas import TOOLS  # noqa: E402

HERE = Path(__file__).resolve().parent
BASELINE = HERE / "prompt_eval_baseline.json"

# (fixture, task, acceptable tools, acceptable element numbers or None = any)
CASES = [
    ("add_activity_form", "pick Krishna as the contact", ["select_option"], [4]),
    ("add_activity_form", "set the channel to Email", ["select_option"], [5]),
    ("add_activity_form", "save this activity with contact Ravi and channel Phone", ["select_option", "batch", "plan"], None),
    ("add_activity_form", "set the activity type to Call", ["select_option", "ask_user"], [5, None]),
    ("add_activity_form", "write 'follow up next week' in the description", ["type"], [9]),
    ("amazon_search", "search for gaming keyboard", ["type"], [3]),
    ("amazon_search", "search for harry potter only in Books", ["select_option", "batch", "plan"], [2, None]),
    ("amazon_search", "what is the price of the first result", ["read_text", "find_text", "done"], None),
    ("amazon_search", "open my cart", ["click"], [6]),
    ("amazon_search", "how many results are there", ["done", "read_text"], None),
    ("browser_page", "what is krify's phone number", ["done", "find_text", "read_text"], None),
    ("browser_page", "show me the services menu", ["hover", "click"], [6]),
    ("browser_page", "open the careers page", ["find_elements", "find_text", "scroll", "hover", "click"], None),
    ("combined_text", "how many happy clients does krify have", ["done", "read_text", "find_text"], None),
    ("combined_text", "send krify an email", ["click"], [3]),
    ("combined_text", "summarise this page", ["read_text", "done"], None),
    ("clickable_rows", "open the Globex opportunity", ["click"], [7]),
    ("clickable_rows", "what's the amount for Initech", ["done", "read_text"], None),
    ("clickable_rows", "how many opportunities are there", ["done", "read_text"], None),
    ("clickable_rows", "delete the Acme opportunity", ["click"], [5]),
    ("login_dialog", "sign in with santhosh@gmail.com", ["type", "batch"], [6, None]),
    ("login_dialog", "log in", ["ask_user"], None),
    ("login_dialog", "close this popup", ["click"], [1]),
    ("login_dialog", "I forgot my password", ["click"], [2]),
    ("browser_sites", "donate to the cause", ["click", "scroll"], [48, None]),
    ("browser_sites", "search amazon for headphones in Fashion", ["select_option", "batch", "plan"], [3, None]),
    ("combined_text", "go to flipkart.com", ["open_url"], None),
    ("amazon_search", "compare the iPhone 16 price on amazon and flipkart", ["plan", "open_url", "new_tab", "type", "batch"], None),
]


def first_step(msg):
    """(tool name, args) of the model's first tool call; plain text counts as 'text'."""
    calls = getattr(msg, "tool_calls", None) or []
    if not calls:
        return "text", {"text": (getattr(msg, "content", "") or "")[:120]}
    f = calls[0].function
    try:
        args = json.loads(f.arguments or "{}")
    except ValueError:
        args = {"raw": f.arguments}
    if f.name == "batch" and args.get("steps"):
        args = {**args, "element": (args["steps"][0] or {}).get("element")}
    return f.name, args


def judge(case, name, args):
    """True = right first step. "text" = a readable plain-text answer where done was right: the app shows
    it too, but only after asking once more (one extra AI call), so it is counted apart."""
    _, _, tools, elements = case
    if name == "text":
        text = (args.get("text") or "").strip()
        readable = text and not text.startswith(("{", "[", "```"))
        return "text" if readable and "done" in tools else False
    if name not in tools:
        return False
    return elements is None or args.get("element") in elements


def label(ok):
    return "PASS (text, +1 ask)" if ok == "text" else ("PASS" if ok else "FAIL")


async def run_case(i, case, model):
    fixture, task = case[0], case[1]
    view = pageview.render(json.load(open(HERE.parent / "fixtures" / f"{fixture}.json", encoding="utf-8")))
    # Like the real loop: the planner has already written a plan and it is approved (or pre-approved).
    user = f"[Your task] {task}\n[Your plan] 1. {task}\n\n{view.text}\n{view.hint}".strip()
    messages = [{"role": "system", "content": system_prompt(None)}, {"role": "user", "content": user}]
    for wait in (60, 90, 120, 180, 0):
        try:
            msg, used, tokens = await ask_ai(messages, TOOLS, model)
            break
        except RuntimeError as e:  # every free model busy: wait and try again
            if not wait:
                raise
            print(f"   ({e} – waiting {wait}s)", flush=True)
            await asyncio.sleep(wait)
    name, args = first_step(msg)
    return {"case": i, "task": task, "tool": name, "args": args, "ok": judge(case, name, args), "model": used, "tokens": tokens}


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("--record", action="store_true")
    p.add_argument("--only", default="")
    p.add_argument("--runs", type=int, default=1)
    p.add_argument("--model", default="groq:openai/gpt-oss-120b")
    p.add_argument("--rescore", action="store_true", help="re-judge the saved baseline, no AI calls")
    a = p.parse_args()
    if a.rescore:
        scores = []
        for k, runs in json.loads(BASELINE.read_text(encoding="utf-8")).items():
            for r in runs:
                ok = judge(CASES[int(k) - 1], r["tool"], r["args"])
                scores.append(ok)
                print(f"{label(ok):19} {int(k):2} {r['task'][:45]:45} -> {r['tool']} {r['args'].get('element', '')}")
        print(f"\n{sum(s is True for s in scores)} pass + {scores.count('text')} plain-text answers (+1 ask each) "
              f"+ {scores.count(False)} fail, of {len(scores)}")
        return
    pick = {int(x) for x in a.only.split(",") if x} or set(range(1, len(CASES) + 1))
    base = json.loads(BASELINE.read_text(encoding="utf-8")) if BASELINE.exists() and not a.record else {}
    results, passed, total, tokens = {}, 0, 0, 0
    for i, case in enumerate(CASES, 1):
        if i not in pick:
            continue
        for _ in range(a.runs):
            r = await run_case(i, case, a.model)
            results.setdefault(str(i), []).append(r)
            total += 1
            passed += bool(r["ok"])
            tokens += r["tokens"] if isinstance(r["tokens"], int) else 0
            was = base.get(str(i), [{}])[0]
            diff = "" if not was else ("" if (was.get("tool"), was.get("args", {}).get("element")) == (r["tool"], r["args"].get("element")) else f"  (baseline: {was.get('tool')} {was.get('args', {}).get('element')})")
            print(f"{label(r['ok'])} {i:2} {case[1][:45]:45} -> {r['tool']} {r['args'].get('element', '')}{diff}", flush=True)
            if a.record:  # save as we go, so a crash keeps what was done
                BASELINE.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n{passed}/{total} passed · {tokens} tokens")
    if a.record:
        BASELINE.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"saved {BASELINE.name}")


if __name__ == "__main__":
    asyncio.run(main())
