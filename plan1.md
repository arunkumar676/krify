The plan
Phase 0: Measure first (no code)
Pick 5 benchmark tasks: a look-up, a search, a form fill, a multi-page task and a Sheets task.
Run them and read the 📊 Report. For each call, record how many tokens went to system, tools, page view, [Page text], header and chat history.
Run uv run python checks/run.py --record on known-good code so there's a golden baseline.
Set a pass bar, for example "same task success, at least X% fewer input tokens".
Gate: if the page view plus [Page text] is under about 30% of input on real pages, do Phase 4 first.

Phase 1: Smarter "unchanged"
In add_with_page, compare with layout() (it already strips typed values) instead of raw equality.
If the layout is the same but field values changed, send one line like [Layout unchanged; fields now: …] instead of the full view.
Add a replay scenario in checks/replay.py.
This is the smallest and safest change.

Phase 2: Delta view for same-page changes
When the URL is unchanged and the change is small, send the change_lines diff plus only the changed elements, not the whole view.
Always send the full view when any of these is true:
the URL changed
a dialog opened or closed
the action failed
the delta is larger than N% of the view
the model calls get_elements (this tool already exists and acts as "refresh")
every N steps, as a safety refresh
Spike first: check that [n] numbers stay stable between views. CLAUDE.md says both "stable via data-krify-id" and "re-assigned every page view", so confirm which. If numbers shift, a delta can't be used safely.


Phase 3: Make big optional parts pull-on-demand
In Unrestricted mode, send [Page text] only on the first view of a URL, not on every same-page step.
Tune the tool descriptions and prompt so the model uses find_text, read_text and inspect for details instead of waiting for them to be pushed.
find_text currently also appends a page view (loop.py:440). Make that conditional on whether the page changed.


Phase 4: Cut the fixed overhead (in parallel)
Tighten the system prompt (~5.7k chars) and tool schemas (~6.2k chars in Basic).
Keep the static prefix stable so provider prompt caching helps. Changing tool_groups mid-task already invalidates it, so avoid reshuffling.
Keep checks/prompt_check.py green. It enforces that a rule never names a tool that isn't sent.

Phase 5: Fix the guardrail interactions
Fact-check leak. view.py:74 adds the full view to state.seen unconditionally. With deltas, the model could cite a number it never saw and still pass check_facts. Change it so seen records only what was sent.
Add replay scenarios for: a stale [n] after a delta, a number the model never saw, and a batch whose layout moved.

Phase 6: Roll out behind a flag
Add a VIEW_MODE=full|hybrid setting, default full, like STRUCTURED_VIEW.
Run checks/run.py. Only the token golden and the new scenarios should differ.
Rerun the 5 tasks and compare with Phase 0. Switch the default only if you meet the bar.
Advantages
Fewer tokens on long tasks. Most steps on the same page send a few lines, not a full view.
Few extra model calls. The model still gets what it needs automatically, unlike pure pull.
Server-side safety stays intact, because the checks read the full state.
It's incremental and reversible. Each phase ships separately behind a flag.
It reuses what exists: layout(), page_changes and get_elements.
Disadvantages and risks
Stale or shifted [n] numbers if numbering isn't stable. A wrong click costs a full round trip.
Weaker models may misread a delta. Free-tier models might treat "+ new, ~ changed" lines as the whole page and lose context.
The fact check weakens unless seen is fixed (Phase 5).
More code paths. Full, unchanged and delta views multiply what has to be tested, so the replay suite grows.
Prompt-cache churn if prefixes shift between steps, which can cancel part of the savings.
Savings are unmeasured. They depend on how much of real input is page view versus fixed overhead (Phase 0 decides).
Little help for one-shot tasks. A 2-step look-up gains almost nothing, because the fixed overhead dominates.
Not included
Pure pull-only (no automatic view), because more calls mean more fixed overhead and more rate-limit hits.
Replacing JSON tools with text commands. That needs its own reliability test on free-tier models.
Want me to start with Phase 0 (reading the report data and measuring real tasks)? That's read-only, and it tells us whether Phase 1–3 or Phase 4 deserves to go first.



