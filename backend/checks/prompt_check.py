"""The system prompt may only talk about tools that are sent with it.

For every combination of the optional tool groups: every tool name in the rules (the page-view legend is
about the view, not the tools, so it is left out) must be a tool sent for that combination. Also that
BY_GROUP uses real group names in TOOL_GROUPS order, and that "all groups" and "None" are the same text.
Exit 0 = all good (run by checks/run.py).
"""

import itertools
import re
import sys

from app.context import prompt
from app.tools.schemas import GROUP_ORDER, TOOL_GROUPS, tools_for

OPTIONAL = [g for g in GROUP_ORDER if g != "basic"]
EVERY_TOOL = {name for names in TOOL_GROUPS.values() for name in names}
problems = []

# BY_GROUP: real groups, same order as TOOL_GROUPS.
unknown = [g for g in prompt.BY_GROUP if g not in OPTIONAL]
if unknown:
    problems.append(f"BY_GROUP has groups that don't exist: {unknown}")
order = [g for g in OPTIONAL if g in prompt.BY_GROUP]
if list(prompt.BY_GROUP) != order:
    problems.append(f"BY_GROUP order {list(prompt.BY_GROUP)} differs from TOOL_GROUPS order {order}")

checked = 0
for size in range(len(OPTIONAL) + 1):
    for combo in itertools.combinations(OPTIONAL, size):
        groups = {"basic", *combo}
        sent = {t["function"]["name"] for t in tools_for(groups)}
        text = prompt.rules(groups)
        for name in sorted(EVERY_TOOL - sent):
            if re.search(rf"(?<![\w]){re.escape(name)}(?![\w])", text):
                problems.append(f"groups {sorted(groups)}: the rules name '{name}', which isn't sent")
        checked += 1

if prompt.system_prompt(None) != prompt.system_prompt({"basic", *OPTIONAL}):
    problems.append("system_prompt(None) differs from system_prompt(every group)")
plain, everything = len(prompt.system_prompt({"basic"})), len(prompt.system_prompt())
if plain >= everything:
    problems.append(f"the basic-only prompt ({plain}) is not shorter than the full one ({everything})")

for line in problems:
    print("✗", line)
print(f"{checked} group combinations checked; basic only {plain} chars, all groups {everything} chars")
sys.exit(1 if problems else 0)
