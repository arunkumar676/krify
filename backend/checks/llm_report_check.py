"""LLM report check: record fake requests (one ok, one failed, one with a screenshot) and assert the
report's numbers, the screenshot redaction, who may read it, pruning and deletion. Prints "ok" lines
(the run.py job fails on any assertion). Uses the in-memory database run.py sets up."""

import json

from openai.types.chat import ChatCompletion

from app import history
from app.llm import report

IMG = "data:image/png;base64," + "A" * 4096
TOOLS = [{"type": "function", "function": {"name": "click", "parameters": {}}}]


def completion(prompt, out, content="hi", tool=None):
    message = {"role": "assistant", "content": content}
    if tool:
        message["tool_calls"] = [{"id": "c1", "type": "function", "function": {"name": tool, "arguments": "{}"}}]
    return ChatCompletion.model_validate({
        "id": "x", "object": "chat.completion", "created": 0, "model": "m",
        "choices": [{"index": 0, "finish_reason": "stop", "message": message}],
        "usage": {"prompt_tokens": prompt, "completion_tokens": out, "total_tokens": prompt + out},
    })


def call(response=None, status="ok", tools=TOOLS, model="groq:a", messages=None, attempt=0):
    messages = messages or [{"role": "system", "content": "sys"}, {"role": "user", "content": "task"}]
    report.record_call(model, "groq", attempt, messages, tools, {"reasoning_effort": "low"}, False, False, 120,
                       response, status, "boom" if status != "ok" else "")


task = history.start_task("find a mouse", "groq:a", "client-1")
other = history.start_task("someone else's", "groq:a", "client-2")
report.set_task(task)
call(completion(10, 2, content="plan"), tools=None)  # planner
report.set_step(1)
call(status="rate_limit", model="groq:a")
call(completion(100, 20, tool="click"), model="gemini:b", attempt=0,
     messages=[{"role": "user", "content": [{"type": "text", "text": "look"}, {"type": "image_url", "image_url": {"url": IMG}}]}])

r = report.task_report(task, "client-1")
t = r["totals"]
assert (t["calls"], t["ok"], t["failed"]) == (3, 2, 1), t
assert (t["prompt_tokens"], t["completion_tokens"], t["total_tokens"]) == (110, 22, 132), t
assert [c["seq"] for c in r["calls"]] == [1, 2, 3]
assert [c["purpose"] for c in r["calls"]] == ["planner", "agent_step", "agent_step"]
assert r["calls"][0]["step"] is None and r["calls"][1]["step"] == 1
assert r["by_model"]["groq:a"]["failed"] == 1 and r["by_model"]["gemini:b"]["total_tokens"] == 120
assert r["calls"][2]["response"]["tool_calls"][0]["function"]["name"] == "click"
assert r["calls"][2]["request"]["tools"] == ["click"] and r["calls"][0]["request"]["tools"] == []
dump = json.dumps(r)
assert IMG not in dump and "[image ~4 KB]" in dump, "screenshot must be redacted"
md = report.task_report_markdown(r)
assert "LLM calls: 3 (2 ok, 1 failed)" in md and "[image ~4 KB]" in md and "boom" in md
print("ok: totals, order, redaction, markdown")

assert report.task_report(task, "client-2") is None, "another client must not read it"
assert report.latest_task_id("client-1") == task
print("ok: only the owner can read it")

assert report.totals(task) == (3, 110, 22)
history.finish_task(task, "done", "x", 132, "groq:a", *report.totals(task))
assert history.recent("client-1")[0]["calls"] == 3
report.prune(keep=1)  # keeps only the newest task's text
pruned = report.task_report(task, "client-1")["calls"][0]
assert pruned["request"] is None and pruned["prompt_tokens"] == 10, "pruning drops text, keeps numbers"
assert "no longer stored" in report.task_report_markdown(report.task_report(task, "client-1"))
print("ok: history totals, pruning")

history.clear("client-1")
assert report.totals(task) == (0, 0, 0), "deleting a task deletes its calls"
print("ok: delete")
