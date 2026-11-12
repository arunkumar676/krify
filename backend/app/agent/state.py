"""Per-task state: what the agent knows about the task in progress and the page it's on.

One TaskState per Agent (side-panel connection). start() resets it IN PLACE when the user sends a
message – it's never replaced, because a message that answers Krify's question (ask_user) carries
on with the same task: its goal, plan, notes and what was read and done so far are kept. The
connection's own things (the chat, the callbacks, the session switches) live on Agent.

The guardrails/verification/progress functions take a TaskState as their first argument.
"""

from dataclasses import dataclass, field


@dataclass(slots=True)  # slots: a mistyped field name raises instead of quietly adding a new one
class TaskState:
    # ---- The task: shown to the AI at every step so it keeps the goal in view. Kept while the user
    # answers a question (start(continuing=True)).
    task_text: str = ""
    product_text: str = ""  # the user's own words, for the wrong-product checks
    goal: str = ""
    plan: list = field(default_factory=list)
    done_when: str = ""
    plan_approved: bool = False  # has the user signed off on the current plan?
    notes: list = field(default_factory=list)
    sites: set = field(default_factory=set)  # sites seen this task – a new one triggers a plan check
    seen: list = field(default_factory=list)  # everything read from pages this task – used by the fact check
    actions: list = field(default_factory=list)  # (op, args, result) that really ran – checked against "I have …" claims
    form_edit: dict | None = None  # {url, name}: a form Krify started filling and hasn't submitted yet
    leave_warned: set = field(default_factory=set)  # steps that would leave that form, refused once already

    # ---- This run: reset every time the user sends a message.
    turn_start: int = 0  # len(actions) when the user's latest message came in
    idle_steps: int = 0  # steps in a row that made no progress (stuck detector)
    last_read: str = ""  # newest read_text result, to catch reading the same text again
    note_warnings: set = field(default_factory=set)  # wrong-product warnings already given (the 2nd try is saved)
    product_check_failed: bool = False  # final answer sent back once for not naming the product
    fact_check_failed: bool = False
    filter_check_failed: bool = False
    claim_check_failed: bool = False
    handoff_check_failed: bool = False
    ui_check_failed: bool = False
    page_status: str = ""  # the page's own [Status] line as of the last page view
    form: dict | None = None  # the form being filled in the latest view (pageview.View.form)
    submit_guarded: set = field(default_factory=set)  # (form name, empty fields) a Save click was already refused for
    current_url: str = ""
    scrolls: dict = field(default_factory=dict)  # url (+ " box") -> {"total", "idle" (scrolls that showed nothing new), "views" seen}
    edit_allowed: bool = False  # the user allowed editing a Google Sheet/Doc in this task
    recent_steps: list = field(default_factory=list)  # for the repeat detector
    shot_size: tuple | None = None  # (width, height) of the last screenshot
    shot_page: str | None = None  # the page when the last screenshot was taken
    last_cell: str = ""
    boost_next: bool = True  # the first reply (interpreting the raw task) gets more thought
    trail: list = field(default_factory=list)  # (page key, title) after each action that changed the page – loop detection
    loop_warnings: int = 0
    loop_question: str = ""  # set when it's still looping after a warning: stop and ask the user

    # ---- The latest page view: overwritten by every page view, never reset between tasks.
    elements: str = ""  # latest numbered element list, used by the safety check
    view_text: str = ""  # the latest page view as the AI reads it (structured lines with field hints)
    open_list: tuple | None = None  # the open custom dropdown list in the latest view (pageview.View.open_list)
    unrestricted: bool = False  # the side panel's Unrestricted switch, as of the latest page view
    current_title: str = ""
    last_typed_line: str = ""

    def start(self, task, continuing):
        """A new message from the user. continuing: it answers Krify's question (ask_user), so carry on
        with the same task; otherwise it's a new task. Agent.run() resets its own (connection) parts."""
        if continuing:  # same task: keep the goal, plan, notes and what was read so far
            self.product_text = f"{self.product_text} {task}"
            self.task_text = f"{self.task_text}\n(You asked the user; they answered: {task})"
            self.seen.append(task)
        else:
            self.product_text = task
            self.task_text, self.notes, self.sites = task, [], set()
            self.goal, self.plan, self.done_when = "", [], ""
            self.plan_approved = False
            self.actions = []
            self.seen = [task]  # numbers the user gave count as known
            self.form_edit, self.leave_warned = None, set()
        self.idle_steps = 0
        self.last_read = ""
        self.note_warnings = set()
        self.product_check_failed = False
        self.fact_check_failed = False
        self.filter_check_failed = False
        self.claim_check_failed = False
        self.handoff_check_failed = False
        self.turn_start = len(self.actions)
        self.page_status = ""
        self.ui_check_failed = False
        self.current_url = ""
        self.scrolls = {}
        self.edit_allowed = False
        self.form, self.submit_guarded = None, set()
        self.recent_steps = []
        self.shot_size = None
        self.shot_page = None
        self.last_cell = ""
        self.boost_next = True
