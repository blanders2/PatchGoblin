"""Request validation and the task state machine shared by the API routes."""
from __future__ import annotations

from . import gitops
from .prompts import PLAN_TRUST_LEVELS, plan_questions, ready_status
from .store import PAUSABLE, STATUSES, log_event, now, set_status

MAX_BATCH = 200


def plan_answers(value) -> list[dict] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not all(
            isinstance(a, dict) and isinstance(a.get("question", ""), str)
            and isinstance(a.get("answer", ""), str) for a in value):
        raise ValueError("answers must be a list of {question, answer} strings.")
    answers = [{"question": a.get("question", ""), "answer": a.get("answer", "")}
               for a in value if a.get("answer", "").strip()]
    return answers or None


def require_secrets_ack(data: dict) -> None:
    if data.get("secrets_ack") is not True:
        raise ValueError("Confirm that this directory contains no secrets (API keys, .env files, "
                         "credentials) before PatchGoblin creates a git repository and commits it.")


def plan_limit(value) -> int:
    """Max simultaneous planning jobs for a project; 0 means unlimited."""
    if value is None or value == "":
        return 0
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("Plan limit must be a whole number ≥ 0.")
    try:
        limit = int(value)
    except ValueError:
        raise ValueError("Plan limit must be a whole number ≥ 0.") from None
    if limit < 0:
        raise ValueError("Plan limit must be a whole number ≥ 0.")
    return limit

def sync_mode(value) -> str:
    if value not in gitops.SYNC_MODES:
        raise ValueError("Sync mode must be one of: " + ", ".join(gitops.SYNC_MODES) + ".")
    return value

def plan_trust(value, allow_blank: bool = False) -> str:
    """A planning trust level; "" (a task's "use the project's level") only if allowed."""
    if (value == "" and allow_blank) or value in PLAN_TRUST_LEVELS:
        return value
    raise ValueError("Plan trust must be one of: " + ", ".join(PLAN_TRUST_LEVELS) + ".")


def remote_view(project: dict, status: dict) -> dict:
    return {**status, "auto_sync": project.get("auto_sync") is True,
            "sync_mode": project.get("sync_mode") or "ff-only"}


# Simple state changes: action -> (allowed from, new status, history message).
# A "planned" target lands in "drafted" instead while the plan has open questions.
TRANSITIONS = {
    "mark_planned": (("unplanned", "failed", "drafted"), "planned", "Marked planned"),
    "mark_drafted": (("planned",), "drafted", "Moved to drafted"),
    "unplan": (("planned", "drafted"), "unplanned", "Moved back to unplanned"),
    "queue": (("planned", "drafted", "failed"), "queued", "Queued for AI"),
    "dequeue": (("queued",), "planned", "Removed from queue"),
    "approve": (("review",), "done", "Approved by engineer"),
    "reopen": (("done", "review"), "planned", "Reopened"),
    # Pause and resume only set or clear the task's "paused" flag (no status change); they
    # are handled in transition() and listed here for their allowed statuses.
    "pause": (PAUSABLE, None, "Paused"),
    "resume": (STATUSES, None, "Resumed"),
}

def transition(engine, pid: str, task: dict, action: str, queued_at: str | None = None) -> bool:
    """Apply a simple state change; True if the task was queued (by hand or Auto-queue)."""
    allowed, status, message = TRANSITIONS[action]
    source = task["status"]
    if action == "resume":
        if not task.get("paused"):
            raise ValueError(f"Cannot resume a task that is {source}.")
        task["paused"] = False
        log_event(task, message)
        return source == "queued"
    if task.get("paused"):
        raise ValueError("Task is paused; resume it first.")
    if source not in allowed:
        raise ValueError(f"Cannot {action.replace('_', ' ')} a task that is {task['status']}.")
    if action == "pause":
        task["paused"] = True
        log_event(task, message)
        return False
    if status == "planned":
        status = ready_status(task.get("plan", ""))
        if status == "drafted":
            if task["status"] == "drafted":
                raise ValueError("Plan still has open questions; "
                                 "answer them or remove them from the plan first.")
            message += " (plan has open questions)"
    elif action == "mark_drafted" and not plan_questions(task.get("plan", "")):
        raise ValueError("Plan has no open questions; nothing to draft.")
    if action == "queue":
        task["queued_at"] = queued_at or now()
    set_status(task, status, message)
    if action == "queue":
        return True
    # Like an AI plan, only a first plan is auto-queued (not one from drafted or failed).
    return action == "mark_planned" and source == "unplanned" and engine.maybe_auto_queue(pid, task)
