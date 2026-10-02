"""Prompts sent to the AI back ends, and parsing of the plans that come back."""
from __future__ import annotations

import re

PLAN_INSTRUCTIONS = """\
You are planning a software task for the project in the current working directory.
Investigate the relevant code as needed, but DO NOT create, modify or delete any files.
Reply with ONLY the implementation plan, in Markdown:
- a one-paragraph summary of the approach
- numbered, concrete steps naming the files/functions to change
- how to verify the change (tests or checks to run)
- a "## Risks" section: things the implementer should watch for
- a "## Questions for you" section: a numbered list of decisions only the user can make,
  one line each. If there are none, write "None."
  If a question is yes/no, end it with `[Yes / No]`; if it has a few likely answers, end it
  with them in brackets separated by ` / `, e.g. `[Tabs / Spaces]` (2–5 short options).
  Open-ended questions have no brackets.
"""

PLAN_TRUST_LEVELS = ("low", "normal", "high")

TRUST_INSTRUCTIONS = {
    "normal": "",
    "low": """\
Planning trust is LOW: do not assume. Whenever requirements, scope, UX, naming or approach
are ambiguous, ask under "Questions for you" instead of choosing. Prefer asking over guessing.
List any unavoidable assumptions as one-line bullets in a "## Assumptions" section placed
before "## Risks" ("None." if there are none).
""",
    "high": """\
Planning trust is HIGH: act on your best judgement. Resolve ambiguity yourself using the
existing code, conventions and common practice. Record each such choice as a one-line bullet
in a "## Assumptions" section placed before "## Risks" ("None." if there are none).
Ask under "Questions for you" only about decisions that are costly or hard to undo if wrong
(e.g. data loss, public API or file-format changes, security), or that can't be inferred at
all. Never assume against an answer the user already gave. Most plans should have no questions.
""",
}


def resolve_trust(task: dict, project: dict) -> str:
    """The planning trust level for a task: its own override, else its project's, else 'normal'."""
    for level in ((task or {}).get("plan_trust"), (project or {}).get("plan_trust")):
        if level in PLAN_TRUST_LEVELS:
            return level
    return "normal"

TITLE_INSTRUCTIONS = """\
Start your reply with a single line `Title: <a concise, specific task title in the imperative,
under 80 characters>`, then a blank line, then the plan.
"""

MAX_TITLE = 120
_TITLE_LINE = re.compile(r"^\s*[#>*_\s]*title\s*[*_]*\s*[:\-]\s*[*_]*\s*(.+?)\s*[*_]*\s*$", re.IGNORECASE)


def split_title(text: str) -> tuple[str, str]:
    """Split a leading ``Title: …`` line off an AI plan: returns (title, rest), or ("", text)."""
    lines = (text or "").splitlines()
    first = next((i for i, line in enumerate(lines) if line.strip()), None)
    if first is None:
        return "", text
    match = _TITLE_LINE.match(lines[first])
    if not match:
        return "", text
    title = " ".join(match.group(1).strip().strip("\"'`").split())
    if len(title) > MAX_TITLE:
        title = title[:MAX_TITLE - 1].rstrip() + "…"
    if not title:
        return "", text
    return title, "\n".join(lines[first + 1:]).strip("\n")

_RUN_INTRO = """\
You are implementing a planned task in the project in the current working directory.
Follow the plan, adapting it if the code requires. Keep changes focused on this task.
"""
_RUN_OUTRO = """\
Do not edit anything under .patchgoblin/.
When you are done, reply with a short summary of what you changed and how you verified it.
"""

RUN_INSTRUCTIONS = (_RUN_INTRO
                    + "Do not run git commit or push; the changes are committed automatically when you finish.\n"
                    + _RUN_OUTRO)
RUN_INSTRUCTIONS_UNTRACKED = _RUN_INTRO + _RUN_OUTRO
RUN_INSTRUCTIONS_SVN = (_RUN_INTRO
                        + "Do not run svn commit, add or revert; the user checks changes in later.\n"
                        + _RUN_OUTRO)


CHAT_INSTRUCTIONS = """\
You are chatting with the user about the project in the current working directory.
Investigate the code as needed to answer, but DO NOT create, modify or delete any files.
Reply to the user's latest message in Markdown, concisely.
"""

MAX_CHAT_CONTEXT = 40000

IMAGE_REF = re.compile(r"!\[[^\]]*\]\((\.patchgoblin/attachments/[0-9a-f]{32}\.(?:png|jpg|gif|webp))\)")
MAX_IMAGES = 10
IMAGE_MIME = {"png": "image/png", "jpg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}


def image_refs(*texts: str) -> list[str]:
    """Unique attachment paths referenced by Markdown images in ``texts``, in order."""
    found: list[str] = []
    for text in texts:
        for path in IMAGE_REF.findall(text or ""):
            if path not in found:
                found.append(path)
    return found[:MAX_IMAGES]


def _images_section(paths: list[str]) -> str:
    return ("## Attached images\nThe text above references these image files (paths relative to the "
            "project root). Open each one with your file-reading tool to view it:\n"
            + "\n".join(f"- {p}" for p in paths))


def chat_prompt(messages: list[dict]) -> str:
    """The conversation so far (oldest turns dropped if long), ending with the user's message."""
    turns, size, user_texts = [], 0, []
    for msg in reversed(messages):
        turn = f"### {'User' if msg['role'] == 'user' else 'Assistant'}\n{msg['text'].strip()}"
        if turns and size + len(turn) > MAX_CHAT_CONTEXT:
            break
        turns.insert(0, turn)
        if msg["role"] == "user":
            user_texts.insert(0, msg["text"])
        size += len(turn)
    prompt = CHAT_INSTRUCTIONS + "\n# Conversation\n\n" + "\n\n".join(turns) + "\n"
    images = image_refs(*user_texts)
    return prompt + "\n" + _images_section(images) + "\n" if images else prompt


_QUESTIONS_HEADING = re.compile(r"^#{1,6}\s*(open\s+)?questions\b", re.IGNORECASE)
_HEADING = re.compile(r"^#{1,6}\s")
_ITEM = re.compile(r"^\s*(?:\d+[.)]|[-*+])\s+(.*)$")
_NONE = re.compile(r"^\W*(none|n/?a|no( open)? questions)\W*$", re.IGNORECASE)
_OPTIONS = re.compile(r"\s*\[([^\[\]]*/[^\[\]]*)\]\s*[.?]?\s*$")
MAX_OPTIONS = 6


def _parse_question(q: str) -> dict:
    """Split a trailing ``[A / B / C]`` off a question into its answer options."""
    match = _OPTIONS.search(q)
    if not match:
        return {"text": q, "options": []}
    options, seen = [], set()
    for opt in match.group(1).split("/"):
        opt = opt.strip()
        if opt and opt.lower() not in seen:
            seen.add(opt.lower())
            options.append(opt)
    options = options[:MAX_OPTIONS]
    if len(options) < 2:
        return {"text": q, "options": []}
    text = q[:match.start()].rstrip()
    if "?" in q[match.end(1):] and not text.endswith("?"):  # "Which [A / B]?" keeps its "?"
        text += "?"
    return {"text": text, "options": options}


def plan_questions(plan: str) -> list[dict]:
    """The items of the plan's "Questions for you" (or "Open questions") section, as
    ``{"text", "options"}``; options come only from a trailing ``[A / B]`` on the item."""
    items: list[str] = []
    inside = False
    for line in (plan or "").splitlines():
        stripped = line.strip()
        if _HEADING.match(stripped):
            if inside:
                break
            inside = bool(_QUESTIONS_HEADING.match(stripped))
            continue
        if not inside or not stripped:
            continue
        item = _ITEM.match(line)
        if item:
            items.append(item.group(1).strip())
        elif items and line[:1].isspace():
            items[-1] += " " + stripped  # wrapped continuation of the previous item
    return [_parse_question(q) for q in items if q and not _NONE.match(q)]


def ready_status(plan: str) -> str:
    """Where a task with this plan belongs once it is ready: 'drafted' while it has open questions."""
    return "drafted" if plan_questions(plan or "") else "planned"


def plan_prompt(task: dict, feedback: str = "", answers: list[dict] | None = None,
                rewrite_title: bool = False, trust: str = "normal", review_feedback: str = "",
                commit: str = "") -> str:
    # The title rule stays last so "Start your reply with…" wins.
    instructions = (PLAN_INSTRUCTIONS + TRUST_INSTRUCTIONS.get(trust, "")
                    + (TITLE_INSTRUCTIONS if rewrite_title else ""))
    parts = [instructions, f"# Task #{task['id']}: {task['title']}"]
    if task.get("description", "").strip():
        parts.append(f"## Description\n{task['description'].strip()}")
    if task.get("plan", "").strip():
        parts.append(f"## Current draft plan\n{task['plan'].strip()}")
    answered = [a for a in answers or [] if a.get("answer", "").strip()]
    if answered:
        qa = "\n\n".join(f"Q: {a.get('question', '').strip()}\nA: {a['answer'].strip()}" for a in answered)
        parts.append(f"## Answers to your questions\n{qa}\n\n"
                     "Fold these answers into a complete revised plan. Under \"Questions for you\", "
                     "repeat only questions that are still open.")
    if feedback.strip():
        parts.append(f"## Feedback on the plan from the user\n{feedback.strip()}\n\n"
                     "Produce a revised, complete plan that addresses this feedback.")
    if review_feedback.strip():
        parts.append(_review_section(review_feedback, commit))
    images = image_refs(task.get("description", ""), feedback, review_feedback)
    if images:
        parts.append(_images_section(images))
    return "\n\n".join(parts) + "\n"


def _review_section(feedback: str, commit: str = "") -> str:
    """The engineer's feedback on a finished AI run, framed as follow-up work."""
    where = (f"The previous attempt was already committed (`{commit[:10]}`)" if commit
             else "The previous attempt's changes are already in the working tree")
    return (f"## Feedback from reviewing the last AI run\n{feedback.strip()}\n\n"
            f"{where}, so the current code already includes it. Plan only the follow-up changes "
            "needed on top of the current code; do not redo work that is already correct.")


def run_prompt(task: dict, vcs: str = "git") -> str:
    """The run prompt; ``vcs`` is "git", "svn" or "" (no version control)."""
    instructions = {"git": RUN_INSTRUCTIONS, "svn": RUN_INSTRUCTIONS_SVN}.get(vcs, RUN_INSTRUCTIONS_UNTRACKED)
    parts = [instructions, f"# Task #{task['id']}: {task['title']}"]
    if task.get("description", "").strip():
        parts.append(f"## Description\n{task['description'].strip()}")
    plan = task.get("plan", "").strip() or "(No written plan: use the description.)"
    parts.append(f"## Plan\n{plan}")
    if (task.get("review_feedback") or "").strip():
        parts.append(_review_section(task["review_feedback"], task.get("commit", "")))
    images = image_refs(task.get("description", ""), task.get("review_feedback") or "")
    if images:
        parts.append(_images_section(images))
    return "\n\n".join(parts) + "\n"
