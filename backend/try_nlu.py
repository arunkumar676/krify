"""Test natural language understanding on a fake page (no browser yet).

Chat mode:      uv run python try_nlu.py
One sentence:   uv run python try_nlu.py "search for wireless mouse"
"""

import json
import sys

from dotenv import load_dotenv
from groq import BadRequestError, Groq

from app.agent import pageview
from app.agent.prompt import system_prompt
from app.agent.tools import TOOLS
from app.config import STRUCTURED_VIEW

MODEL = "openai/gpt-oss-120b"
MAX_STEPS = 8  # per sentence

# A pretend shopping page, so we can test without a browser.
FAKE_PAGE = {
    "get_page_info": "URL: https://www.amazon.in/s?k=mouse | Title: Amazon.in: mouse",
    "get_elements": """[1] link "Amazon.in" (logo)
[2] link "Your Orders"
[3] link "Cart (0)"
[7] input "Search Amazon.in" (value: "mouse")
[8] button "Go"
[15] select "Sort by" = "Featured" (options: Featured, Price: Low to High, Price: High to Low, Avg. Customer Review, Newest Arrivals)
[20] link "Logitech M235 Wireless Mouse - ₹699"
[21] button "Add to cart" (for Logitech M235)
[22] link "HP X200 Wireless Mouse - ₹499"
[23] button "Add to cart" (for HP X200)
[24] link "Zebronics Zeb-Transformer Gaming Mouse - ₹349"
[25] button "Add to cart" (for Zebronics)
[30] link "Next page\"""",
    "read_text": "Results for 'mouse'. Logitech M235 Wireless Mouse ₹699, 4.3 stars. "
    "HP X200 Wireless Mouse ₹499, 4.1 stars. Zebronics Zeb-Transformer Gaming Mouse ₹349, 3.9 stars.",
}

# With the structured page view on, the fake page is a saved page model rendered the same way the
# agent renders a real one (see fixtures/ and app/agent/pageview.py).
if STRUCTURED_VIEW:
    with open("fixtures/amazon_search.json", encoding="utf-8") as _f:
        _view = pageview.render(json.load(_f))
    FAKE_PAGE["get_page_info"] = "URL: https://www.amazon.in/s?k=wireless+mouse | Title: Amazon.in: wireless mouse"
    FAKE_PAGE["get_elements"] = f"{_view.text}\n{_view.hint}".strip()
    FAKE_PAGE["read_text"] = ("Results for 'wireless mouse'. Logitech M185 Wireless Mouse ₹699 M.R.P ₹999, 4.3 stars. "
                              "Sponsored HP X200 Wireless Mouse ₹549, 4.1 stars. Portronics Toad 23 ₹399, 3.9 stars, "
                              "currently unavailable.")


def describe(name, args):
    """Turn a tool call into a short readable line, e.g. click(element=8)."""
    params = ", ".join(f"{k}={v!r}" for k, v in args.items())
    return f"{name}({params})"


def fake_run(name):
    """Pretend to run an operation. Page reads return fake data; actions just say OK."""
    return FAKE_PAGE.get(name, "OK. (Test mode: the page didn't really change. Assume the action worked.)")


def ask_ai(client, messages):
    """Call Groq. Retry when the model returns a broken tool call (happens now and then)."""
    for attempt in range(3):
        try:
            return client.chat.completions.create(
                model=MODEL, messages=messages, tools=TOOLS, tool_choice="auto"
            ).choices[0].message
        except BadRequestError as err:
            broken = "tool_use_failed" in str(err) or "output_parse_failed" in str(err)
            if not broken or attempt == 2:
                raise
            print("  (AI reply was broken, retrying...)")


def handle(client, messages, sentence):
    """Send one user sentence and print every operation the AI chooses."""
    messages.append({"role": "user", "content": f"{sentence}\n\n[Current page]\n{FAKE_PAGE['get_elements']}"})

    for _ in range(MAX_STEPS):
        reply = ask_ai(client, messages)

        if not reply.tool_calls:  # plain answer, no operation
            messages.append({"role": "assistant", "content": reply.content or ""})
            print(f"  💬 {reply.content}")
            return

        messages.append(
            {
                "role": "assistant",
                "content": reply.content or "",
                "tool_calls": [c.model_dump(exclude_none=True) for c in reply.tool_calls],
            }
        )
        for call in reply.tool_calls:
            name = call.function.name
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            print(f"  ➜ {describe(name, args)}")
            messages.append({"role": "tool", "tool_call_id": call.id, "content": fake_run(name)})
            if name in ("done", "ask_user"):
                return

    print("  (stopped: too many steps)")


def main():
    load_dotenv()
    client = Groq()
    messages = [{"role": "system", "content": system_prompt()}]

    if len(sys.argv) > 1:
        sentence = " ".join(sys.argv[1:])
        print(f"You: {sentence}")
        handle(client, messages, sentence)
        return

    print("Fake page: Amazon search results for 'mouse'. Type a request (or 'quit').")
    while (sentence := input("\nYou: ").strip()).lower() not in ("quit", "exit", "q"):
        if sentence:
            handle(client, messages, sentence)


if __name__ == "__main__":
    main()
