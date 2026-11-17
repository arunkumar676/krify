"""Token cost table: what every replay scenario sends to the agent model, in characters, split into the
system prompt, the tool list, the older chat messages and the newest message – plus an estimate in tokens.

No LLM, browser or network: it runs checks/replay.py's scripted scenarios and only counts what each
agent request contained. Run via checks/run.py (a golden file, so any change to the prompt, the tools or
the chat trimming shows its token effect in the diff), or alone:  python -m checks.tokens [scenario …]

The fixtures are small pages, so the numbers are for comparing before/after, not for predicting a bill.
"""

import asyncio
import json
import sys

from checks import replay  # patches the model client before the agent is imported

from app.tools.schemas import TOOLS

CHARS_PER_TOKEN = 4.2  # measured on real requests (3.8–4.8 depending on the model)
TOOL_CHARS = {t["function"]["name"]: len(json.dumps(t, ensure_ascii=False)) for t in TOOLS}
COLUMNS = ("calls", "system", "tools", "older", "newest", "total")

rows = {}  # scenario name -> the sums of COLUMNS


def _chars(value):
    return len(json.dumps(value, ensure_ascii=False, default=str))


def collect(kind, data=None):
    """Stands in for replay.emit: nothing is printed, agent requests are counted."""
    if kind != "ask_ai":
        return
    messages = data["messages"]
    sizes = {
        "calls": 1,
        "system": _chars(messages[0]),
        "tools": sum(TOOL_CHARS.get(name, 0) for name in data["tools"]),
        "older": sum(_chars(m) for m in messages[1:-1]),
        "newest": _chars(messages[-1]) if len(messages) > 1 else 0,
    }
    sizes["total"] = sum(sizes[c] for c in COLUMNS[1:-1])
    row = rows.setdefault(replay.SCENARIO.name, dict.fromkeys(COLUMNS, 0))
    for column in COLUMNS:
        row[column] += sizes[column]


def table():
    width = max(len(name) for name in rows) if rows else 8
    lines = [f"{'scenario':<{width}}  " + "  ".join(f"{c:>8}" for c in COLUMNS) + "  ~tokens"]
    total = dict.fromkeys(COLUMNS, 0)
    for name, row in rows.items():
        lines.append(f"{name:<{width}}  " + "  ".join(f"{row[c]:>8}" for c in COLUMNS)
                     + f"  {round(row['total'] / CHARS_PER_TOKEN):>7}")
        for column in COLUMNS:
            total[column] += row[column]
    lines.append(f"{'ALL':<{width}}  " + "  ".join(f"{total[c]:>8}" for c in COLUMNS)
                 + f"  {round(total['total'] / CHARS_PER_TOKEN):>7}")
    return "\n".join(lines)


async def main():
    replay.VERBOSE = True  # each agent request then carries its whole chat
    replay.emit = collect
    only = set(sys.argv[1:])
    for make in replay.SCENARIOS:
        if not only or make.__name__ in only:
            await replay.run(make)
    print("Characters sent to the agent model per scenario (older = chat before the newest message):")
    print(table())


if __name__ == "__main__":
    asyncio.run(main())
