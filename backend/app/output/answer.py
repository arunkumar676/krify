"""The final answer: what the user sees when a task ends."""

import logging
import re

from app import history
from app.context import router
from app.llm import report
from app.verification.facts import unexplained_numbers, unverified_phrases

log = logging.getLogger("krify")


def preview(text, limit=160):
    """Short one-line preview of a read result, for the dev-mode debug trace."""
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"


class OutputMixin:
    """How a task ends: the final message in the side panel and the history record.

    Mixed into Agent (agent/loop.py): uses its connection (browser, ui, confirm, messages, …), its
    TaskState (self.state) and the other layers' methods through self."""

    async def try_gate(self, task):
        """The cheap-model gate (context/router.py): when the message needs no browser (chat, general
        knowledge) the cheap model's reply is the answer and the agent never starts. Returns True when it
        answered, False to carry on with the agent (also whenever the gate is off or has any problem)."""
        self.router_groups = None  # unknown until the gate answers: then all tools are sent
        cheap = router.cheap_model()
        if not cheap:
            return False
        report.set_purpose("router")
        try:
            kind, reply, tokens, self.router_groups = await router.gate(task, self.last_task, self.gate_tail, cheap)
        finally:
            report.set_purpose(None)
        self.tokens += tokens
        if kind == "task":
            self.gate_tail = ""  # a browser task follows: the chit-chat context no longer applies
            return False
        self.gate_tail = router.tail_of(task, reply)
        self.model = cheap
        await self.finish("done", reply, verify=False)  # its own words, not read from a page: nothing to fact-check
        return True

    async def finish(self, kind, text, verify=True):
        """Send the final message, with how many tokens the task used, and save the result."""
        log.info("Task used %d tokens", self.tokens)
        if kind == "done" and verify:
            if missing := unexplained_numbers(text, self.state.seen):
                text = f"{text}\n⚠️ Krify couldn't confirm these numbers on the pages it read: {', '.join(missing)}"
            if missing := unverified_phrases(text, self.state.seen):
                text = f"{text}\n⚠️ Krify couldn't confirm these names on the pages it read: {', '.join(missing)}"
        self.end_task(kind, text)
        if self.dev_mode and self.trace:
            steps = "\n".join(f"{i}. {t}" for i, t in enumerate(self.trace, 1))
            text = f"{text}\n\n🔍 Debug trace:\n{steps}"
        if kind == "done":
            detail = ""
            if self.task_id and (t := report.totals(self.task_id))[0]:
                detail = f" ({t[1] / 1000:.1f}k sent / {t[2] / 1000:.1f}k received, {t[0]} LLM calls)"
            text = f"{text}\n· used {self.tokens / 1000:.1f}k tokens{detail}"
        await self.ui(kind, text)

    def end_task(self, status, summary):
        """Save how the task ended (done / ask / stopped / error) in the history."""
        self.last_status = status
        if self.task_id:
            calls, sent, received = report.totals(self.task_id)
            history.finish_task(self.task_id, status, summary, self.tokens, self.model, calls, sent, received)
            report.prune()
