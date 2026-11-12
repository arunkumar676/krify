"""The final answer: what the user sees when a task ends."""

import logging
import re

from app import history
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

    async def finish(self, kind, text):
        """Send the final message, with how many tokens the task used, and save the result."""
        log.info("Task used %d tokens", self.tokens)
        if kind == "done":
            if missing := unexplained_numbers(text, self.state.seen):
                text = f"{text}\n⚠️ Krify couldn't confirm these numbers on the pages it read: {', '.join(missing)}"
            if missing := unverified_phrases(text, self.state.seen):
                text = f"{text}\n⚠️ Krify couldn't confirm these names on the pages it read: {', '.join(missing)}"
        self.end_task(kind, text)
        if self.dev_mode and self.trace:
            steps = "\n".join(f"{i}. {t}" for i, t in enumerate(self.trace, 1))
            text = f"{text}\n\n🔍 Debug trace:\n{steps}"
        if kind == "done":
            text = f"{text}\n· used {self.tokens / 1000:.1f}k tokens"
        await self.ui(kind, text)

    def end_task(self, status, summary):
        """Save how the task ended (done / ask / stopped / error) in the history."""
        self.last_status = status
        if self.task_id:
            history.finish_task(self.task_id, status, summary, self.tokens, self.model)
