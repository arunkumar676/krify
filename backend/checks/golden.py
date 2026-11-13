"""G2: text the model sees that must not change by a byte – the pageview CLI on every fixture,
the system prompt and the tool schemas. Run via checks/run.py (once per STRUCTURED_VIEW value)."""

import glob
import json
import subprocess
import sys

from checks._find import find, mod

pageview_module = mod("app.context.pageview", "app.agent.pageview").__name__
for fixture in sorted(glob.glob("fixtures/*.json")):
    for flags in ([], ["--full"], ["--flat"], ["--full", "--flat"]):
        out = subprocess.run([sys.executable, "-m", pageview_module, fixture, *flags],
                             capture_output=True, text=True, check=False)
        print(f"===== pageview {fixture} {' '.join(flags)} (exit {out.returncode})")
        print(out.stdout + out.stderr)

prompt = mod("app.context.prompt", "app.agent.prompt")
print("===== system_prompt()")
print(prompt.system_prompt())
for groups in ("basic", "forms", "tabs", "sheets", "mouse", "look", "lists"):  # what each tool group adds
    print(f"===== system_prompt({{'basic', {groups!r}}})" if groups != "basic" else "===== system_prompt({'basic'})")
    print(prompt.system_prompt({"basic", groups}))
print("===== system_prompt({'basic'}, topics=set())")  # no topic blocks (a task that isn't about shopping)
print(prompt.system_prompt({"basic"}, set()))
for location in (None, {}, {"country": "India"}, {"country": "India", "place": "Hyderabad, Telangana, India"}):
    print(f"===== where_line({location!r})")
    print(prompt.where_line(location))
print("===== TOOLS")
print(json.dumps(find("TOOLS"), ensure_ascii=False, indent=1))
print("===== PLANNER_PROMPT")
print(find("PLANNER_PROMPT"))
