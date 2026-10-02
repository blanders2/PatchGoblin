"""Background work: AI planning jobs and the per-project run queue.

The queue itself is just the task states in each project's tasks.json: the
runner for a project repeatedly claims the oldest ``queued`` task. Runs in one
project are strictly sequential (they share a working tree and git history);
different projects run in parallel. Planning is read-only and runs immediately,
unless the project sets a ``plan_limit`` on simultaneous planning jobs.
"""
from __future__ import annotations

import logging
import threading
import time

from . import gitops, svnops
from .hosts import HostError, host_for, kill_tree
from .providers import (Cancelled, Outcome, chat_prompt, plan_prompt, plan_questions, ready_status, run_ai,
                        resolve_trust, run_prompt, split_title)
from .store import find_task, global_model, log_event, now, resolve_auto, set_status, vcs_for

log = logging.getLogger("patchgoblin")

MAX_OUTPUT = 60000
MAX_LIVE = 200000
MAX_EVENTS = 500
_EVENT_MARK = {"say": "", "tool": "→ ", "error": "! ", "note": "· "}


class Job:
    """A running AI job: its live output buffer and a handle for cancelling it."""

    def __init__(self, kind: str):
        self.kind = kind
        self.started = time.monotonic()
        self.cancelled = False
        self._chunks: list[str] = []
        self._size = 0
        self._proc = None
        self._lock = threading.Lock()
        self._events: list[dict] = []
        self.current = ""

    def event(self, kind: str, label: str, detail: str = "") -> None:
        """Record a step of the agent's work (kind: say, tool, error or note) for the live feed;
        it is also written to the raw log."""
        detail = detail if len(detail) <= 4000 else detail[:4000] + "…"
        with self._lock:
            self._events.append({"at": round(self.elapsed(), 1), "kind": kind, "label": label, "detail": detail})
            del self._events[:-MAX_EVENTS]
            if kind in ("tool", "say"):
                self.current = label.strip().splitlines()[0][:200] if label.strip() else self.current
        self.write(f"{_EVENT_MARK.get(kind, '')}{label}\n")

    def events(self) -> list[dict]:
        with self._lock:
            return list(self._events)

    def write(self, text: str) -> None:
        with self._lock:
            self._chunks.append(text)
            self._size += len(text)
            while self._size > MAX_LIVE and len(self._chunks) > 1:
                self._size -= len(self._chunks.pop(0))

    def text(self) -> str:
        with self._lock:
            return "".join(self._chunks)

    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def attach(self, proc) -> None:
        self._proc = proc
        if self.cancelled:
            kill_tree(proc)

    def cancel(self) -> None:
        self.cancelled = True
        if self._proc is not None:
            kill_tree(self._proc)


MAX_CHAT_MESSAGES = 200


class Chat:
    """A project's AI chat: the conversation (kept in memory only) and any reply in progress."""

    def __init__(self):
        self.messages: list[dict] = []
        self.job: Job | None = None

    def add(self, role: str, text: str, error: bool = False) -> None:
        self.messages.append({"role": role, "text": text, "at": now(), "error": error})
        del self.messages[:-MAX_CHAT_MESSAGES]


def clip_tail(text: str) -> str:
    return text if len(text) <= MAX_OUTPUT else "… [earlier output truncated]\n" + text[-MAX_OUTPUT:]


class Engine:
    def __init__(self, registry, store, settings):
        self.registry, self.store, self.settings = registry, store, settings
        self.jobs: dict[tuple[str, int], Job] = {}
        self.chats: dict[str, Chat] = {}
        self._lock = threading.Lock()
        self._runners: set[str] = set()
        self._pending: set[str] = set()
        # Planning concurrency per project. A Condition rather than a Semaphore because the
        # project's plan_limit can change at runtime and 0 means "no limit".
        self._plan_gate = threading.Condition()
        self._planners: dict[str, int] = {}
        # Held by a project's runner while it claims and runs a task, and by git sync, so a
        # manual sync never changes the working tree under a running task. Re-entrant so
        # auto-sync can run inside the task's own run.
        self._sync_locks: dict[str, threading.RLock] = {}
        self._runs_started: dict[str, int] = {}

    # ---- helpers -------------------------------------------------------
    def job(self, pid: str, tid: int) -> Job | None:
        return self.jobs.get((pid, tid))

    def busy(self, pid: str) -> bool:
        """True while any AI job (planning, run or chat) is active for the project."""
        return any(key[0] == pid for key in list(self.jobs)) or self.chat(pid).job is not None

    def _run_marker(self, pid: str) -> tuple[int, bool]:
        """(runs started so far, whether one is active) for the project: if this differs
        before and after a plan, or a run is active, working-tree changes may be the run's."""
        active = any(key[0] == pid and job.kind == "run" for key, job in list(self.jobs.items()))
        return self._runs_started.get(pid, 0), active

    def _sync_lock(self, pid: str) -> threading.RLock:
        with self._lock:
            return self._sync_locks.setdefault(pid, threading.RLock())

    def provider_for(self, project: dict, task: dict, role: str = "plan") -> tuple[str, str]:
        """The provider and model for a ``plan``, ``run`` or ``chat`` job.

        The model is the task's override, then the project's (only when the task uses the
        project's provider), then the Settings default; "" lets the CLI/endpoint choose.
        """
        provider = task.get("provider") or project.get("provider") or "claude"
        key = "code_model" if role == "run" else "plan_model"
        model = task.get(key) or ""
        if not model and provider == (project.get("provider") or "claude"):
            model = ((project.get("chat_model") if role == "chat" else "")
                     or project.get(key) or "")
        if not model:
            model = global_model(self.settings.get(), provider, key)
        return provider, model

    def _ai(self, project, task, mode, prompt, job, role=None) -> Outcome:
        provider, model = self.provider_for(project, task, role or mode)
        job.write(f"[{now()}] {mode} with {provider}{' / ' + model if model else ''} in {project['path']}\n")
        try:
            return run_ai(provider, mode, prompt, host=host_for(project), project=project,
                          settings=self.settings.get(), model=model, job=job,
                          run_marker=lambda: self._run_marker(project["id"]))
        except Cancelled:
            raise
        except Exception as exc:  # host/network failures become task errors
            log.exception("AI %s failed", mode)
            return Outcome(False, error=f"{type(exc).__name__}: {exc}")

    # ---- startup / consistency ------------------------------------------
    def reconcile(self, project: dict) -> None:
        """Tasks left 'planning'/'running' by a previous server process are interrupted."""
        pid = project["id"]
        doc = self.store.read(project)
        stale = [t["id"] for t in doc["tasks"]
                 if t["status"] in ("planning", "running") and (pid, t["id"]) not in self.jobs]
        if not stale:
            return
        with self.store.edit(project) as doc:
            for task in doc["tasks"]:
                if task["id"] in stale and (pid, task["id"]) not in self.jobs \
                        and task["status"] in ("planning", "running"):
                    if task["status"] == "planning":
                        task["error"] = "Planning was interrupted (PatchGoblin restarted)."
                        set_status(task, task.get("prev_status") or "unplanned", "Planning interrupted")
                    else:
                        task["error"] = ("Run was interrupted (PatchGoblin restarted). "
                                         "Review the working tree before re-queueing.")
                        set_status(task, "failed", "Run interrupted")

    def startup(self) -> None:
        def work():
            for project in self.registry.list():
                try:
                    self.reconcile(project)
                    self.kick(project["id"])
                except Exception as exc:
                    log.warning("Project %s unavailable at startup: %s", project.get("name"), exc)
        threading.Thread(target=work, name="pg-startup", daemon=True).start()

    # ---- planning --------------------------------------------------------
    def start_planning(self, project: dict, tid: int, feedback: str = "",
                       answers: list[dict] | None = None, auto: bool = False, review: bool = False) -> None:
        """Start AI planning. ``review`` sends back a review/done task with feedback: the
        committed work stays and the AI plans follow-up changes on top of it."""
        pid = project["id"]
        job = Job("plan")
        try:
            # The job is registered under the store lock so reconcile() never
            # mistakes this task for one orphaned by a previous process.
            with self.store.edit_task(project, tid) as task:
                if task is None:
                    raise KeyError(tid)
                if task.get("paused"):
                    raise ValueError("Task is paused; resume it first.")
                allowed = ("review", "done") if review else ("unplanned", "drafted", "planned", "failed")
                if task["status"] not in allowed:
                    raise ValueError(f"Cannot {'send back' if review else 'plan'} a task that is "
                                     f"{task['status']}.")
                if review and not feedback.strip():
                    raise ValueError("Send back needs feedback for the AI.")
                if (pid, tid) in self.jobs:
                    raise ValueError("This task already has a job running.")
                self.jobs[(pid, tid)] = job
                task["prev_status"] = task["status"]
                task["error"] = ""
                if review:
                    task["review_feedback"] = feedback.strip()
                    log_event(task, "Sent back from Finished with feedback" if task["status"] == "done"
                              else "Sent back after review with feedback")
                    set_status(task, "planning", "AI re-planning started with review feedback")
                else:
                    extras = [name for name, given in (("answers", answers), ("feedback", feedback.strip()))
                              if given]
                    set_status(task, "planning", "Auto-planned: AI planning started" if auto else
                               "AI planning started" + (" with " + "/".join(extras) if extras else ""))
                snapshot = dict(task)
        except BaseException:
            if self.jobs.get((pid, tid)) is job:
                del self.jobs[(pid, tid)]
            raise
        # Review feedback reaches the prompt through the task itself, not as plan feedback.
        threading.Thread(target=self._plan, args=(project, snapshot, "" if review else feedback, answers, job),
                         name=f"pg-plan-{pid}-{tid}", daemon=True).start()

    def _plan_limit(self, pid: str) -> int:
        try:
            return max(0, int((self.registry.get(pid) or {}).get("plan_limit") or 0))
        except (TypeError, ValueError):
            return 0

    # ---- automation --------------------------------------------------------
    def auto(self, pid: str, key: str) -> bool:
        """The project's effective automation mode, read fresh (never from a job's snapshot)."""
        return resolve_auto(self.registry.get(pid) or {}, self.settings.get(), key)

    def maybe_auto_queue(self, pid: str, task: dict) -> bool:
        """Queue a just-planned task if Auto-queue is on. Call inside ``store.edit``; the
        caller kicks the runner after leaving it."""
        # Paused tasks are skipped on purpose, even when "planned".
        if task["status"] != "planned" or task.get("paused") or not self.auto(pid, "auto_queue"):
            return False
        task["queued_at"] = now()
        set_status(task, "queued", "Auto-queued")
        return True

    def maybe_auto_promote(self, pid: str, task: dict) -> bool:
        """Move a drafted task with no open questions to Planned if Auto-promote is on.
        Call inside ``store.edit``."""
        if (task["status"] != "drafted" or task.get("paused") or plan_questions(task.get("plan") or "")
                or not self.auto(pid, "auto_promote")):
            return False
        set_status(task, "planned", "Auto-promoted: plan has no open questions")
        return True

    def apply_auto_now(self, project: dict, keys) -> None:
        """A mode was just turned on: apply it to the project's existing tasks, in the background."""
        keys = set(keys)
        if keys:
            threading.Thread(target=self._apply_auto, args=(project, keys),
                             name=f"pg-auto-{project['id']}", daemon=True).start()

    def _apply_auto(self, project: dict, keys: set) -> None:
        pid = project["id"]
        try:
            if "auto_queue" in keys:
                with self.store.edit(project) as doc:
                    queued = [t for t in doc["tasks"] if self.maybe_auto_queue(pid, t)]
                if queued:
                    self.kick(pid)
            if "auto_promote" in keys:
                with self.store.edit(project) as doc:
                    for t in doc["tasks"]:
                        self.maybe_auto_promote(pid, t)
            if "auto_plan" in keys:
                # Only unpaused "unplanned" tasks are picked; paused ones are left alone on purpose.
                ids = [t["id"] for t in self.store.read(project, fresh=True)["tasks"]
                       if t["status"] == "unplanned" and not t.get("paused")]
                for tid in ids:
                    try:  # planning threads wait for a slot, so plan_limit still applies
                        self.start_planning(project, tid, auto=True)
                    except (ValueError, KeyError) as exc:
                        log.warning("Auto-plan of %s#%s did not start: %s", pid, tid, exc)
        except Exception as exc:
            log.warning("Automation for project %s failed: %s", project.get("name"), exc)

    def _acquire_plan_slot(self, pid: str, job: Job) -> None:
        """Wait until the project has a free planning slot; raise Cancelled if cancelled first."""
        with self._plan_gate:
            waiting = False
            while True:
                if job.cancelled:
                    raise Cancelled()
                limit = self._plan_limit(pid)  # re-read so a changed limit applies at once
                if limit <= 0 or self._planners.get(pid, 0) < limit:
                    break
                if not waiting:
                    job.write(f"[{now()}] Waiting for a planning slot (limit {limit})\n")
                    waiting = True
                self._plan_gate.wait(timeout=1)
            self._planners[pid] = self._planners.get(pid, 0) + 1

    def _release_plan_slot(self, pid: str) -> None:
        with self._plan_gate:
            self._planners[pid] = max(0, self._planners.get(pid, 0) - 1)
            self._plan_gate.notify_all()

    def _plan(self, project, task, feedback, answers, job) -> None:
        pid, tid = project["id"], task["id"]
        rewrite = project.get("rewrite_titles", True) is not False
        trust = resolve_trust(task, project)
        try:
            try:
                self._acquire_plan_slot(pid, job)
                try:
                    prompt = plan_prompt(task, feedback, answers, rewrite_title=rewrite, trust=trust,
                                         review_feedback=task.get("review_feedback", ""),
                                         commit=task.get("commit", ""))
                    outcome = self._ai(project, task, "plan", prompt, job)
                finally:
                    self._release_plan_slot(pid)
            except Cancelled:
                outcome = Outcome(False, error="Planning cancelled.")
            # Only a task's first plan is auto-queued, not a re-plan with answers or feedback.
            first_plan = task.get("prev_status") == "unplanned" and not answers and not feedback.strip()
            queued = False
            with self.store.edit_task(project, tid) as current:
                if current is None:
                    return
                current["output"] = clip_tail(job.text())
                if outcome.ok:
                    title, plan = split_title(outcome.text) if rewrite else ("", outcome.text)
                    current["plan"] = plan.strip() or outcome.text
                    if title and title != current["title"]:
                        old, current["title"] = current["title"], title
                        log_event(current, f"Title rewritten by AI (was: {old})")
                    current["error"] = ""
                    n = len(plan_questions(current["plan"]))
                    if ready_status(current["plan"]) == "drafted":
                        set_status(current, "drafted",
                                   f"AI plan drafted ({n} open question{'s' if n != 1 else ''})")
                    elif current.get("prev_status") == "drafted" and not self.auto(pid, "auto_promote"):
                        set_status(current, "drafted",
                                   "AI plan ready (no open questions; move to Planned by hand)")
                    else:
                        set_status(current, "planned", "AI plan ready")
                        if first_plan:
                            queued = self.maybe_auto_queue(pid, current)
                else:
                    current["error"] = outcome.error
                    if current.get("prev_status") in ("review", "done"):
                        current["review_feedback"] = ""  # the send-back did not happen
                    set_status(current, current.get("prev_status") or "unplanned",
                               "AI planning failed" if not job.cancelled else "Planning cancelled")
            if queued:
                self.kick(pid)
        except Exception:
            log.exception("Could not record planning result for %s#%s", pid, tid)
        finally:
            self.jobs.pop((pid, tid), None)

    # ---- run queue -------------------------------------------------------
    def kick(self, pid: str) -> None:
        """Make sure the project's runner is working through its queue."""
        with self._lock:
            if pid in self._runners:
                self._pending.add(pid)
                return
            self._runners.add(pid)
        threading.Thread(target=self._runner, args=(pid,), name=f"pg-run-{pid}", daemon=True).start()

    def _runner(self, pid: str) -> None:
        while True:
            claimed = None
            with self._sync_lock(pid):  # waits for a manual sync to finish before claiming
                try:
                    project = self.registry.get(pid)
                    claimed = self._claim(project) if project else None
                except Exception as exc:
                    log.warning("Queue for project %s unavailable: %s", pid, exc)
                if claimed:
                    self._execute(project, *claimed)
            if claimed:
                continue
            with self._lock:
                if pid in self._pending:
                    self._pending.discard(pid)
                    continue
                self._runners.discard(pid)
                return

    def _claim(self, project: dict):
        pid, key, job = project["id"], None, Job("run")
        try:
            with self.store.edit(project) as doc:
                queued = sorted((t for t in doc["tasks"] if t["status"] == "queued" and not t.get("paused")),
                                key=lambda t: (t.get("queued_at") or "", t["id"]))
                if not queued:
                    return None
                task = queued[0]
                key = (pid, task["id"])
                self.jobs[key] = job
                self._runs_started[pid] = self._runs_started.get(pid, 0) + 1
                task["started_at"] = now()
                task["finished_at"] = None
                task["error"] = ""
                set_status(task, "running", "AI run started")
                claimed = dict(task)
        except BaseException:
            if key and self.jobs.get(key) is job:
                del self.jobs[key]
            raise
        return claimed, job

    def _execute(self, project: dict, task: dict, job: Job) -> None:
        pid, tid = project["id"], task["id"]
        host, path = host_for(project), project["path"]
        track = gitops.tracked(project)
        svn = svnops.tracked(project)
        ops = vcs_for(project)
        vcs = ops.NAME if ops else ""
        commit, outcome, svn_before, svn_error = "", None, None, ""
        try:
            try:
                if track and gitops.has_changes(host, path, ignore_metadata=True):
                    sha = gitops.commit_all(host, path, f"PatchGoblin: checkpoint before task #{tid}")
                    job.write(f"Committed pre-existing changes as checkpoint {sha[:10]}\n")
                if svn:
                    try:
                        svn_before = svnops.dirty_fingerprint(host, path)
                    except HostError as exc:
                        svn_error = f"Could not record file changes for check-in: {exc}"
                        job.write(svn_error + "\n")
                outcome = self._ai(project, task, "run", run_prompt(task, vcs), job)
            except Cancelled:
                outcome = Outcome(False, error="Cancelled by user. Review the working tree before re-queueing.")
            except HostError as exc:
                outcome = Outcome(False, error=str(exc))

            with self.store.edit_task(project, tid) as current:
                if current is None:
                    return
                current["output"] = clip_tail(outcome.text or job.text()) if outcome.ok else clip_tail(job.text())
                current["finished_at"] = now()
                if outcome.ok:
                    current["error"] = ""
                    current["review_feedback"] = ""
                    set_status(current, "review", "AI run finished; awaiting review")
                else:
                    current["error"] = outcome.error
                    set_status(current, "failed", "AI run failed")

            if outcome.ok:
                if track:
                    summary = outcome.text.strip()[:1500]
                    note = " (addressing review feedback)" if task.get("review_feedback") else ""
                    commit = gitops.commit_all(host, path,
                                               f"PatchGoblin: task #{tid} {task['title']}{note}\n\n{summary}\n")
                    with self.store.edit_task(project, tid) as current:
                        if current is not None:
                            current["commit"] = commit
                            log_event(current, f"Committed {commit[:10]}" if commit else "No file changes to commit")
                    if project.get("auto_sync") is True:
                        self._auto_sync(project, tid, job)
                elif svn:
                    self._record_svn_changes(project, task, outcome, svn_before, svn_error, job)
                else:
                    with self.store.edit_task(project, tid) as current:
                        if current is not None:
                            log_event(current, "Git tracking off; nothing committed")
        except Exception as exc:
            log.exception("Run of %s#%s failed", pid, tid)
            try:
                with self.store.edit_task(project, tid) as current:
                    if current is not None:
                        current["error"] = (current.get("error") or "") + f"\n{type(exc).__name__}: {exc}"
                        if current["status"] == "running":
                            set_status(current, "failed", "AI run failed")
            except Exception:
                log.exception("Could not record failure for %s#%s", pid, tid)
        finally:
            self.jobs.pop((pid, tid), None)

    def _record_svn_changes(self, project: dict, task: dict, outcome, before, error: str, job: Job) -> None:
        """Remember the files a finished run changed so the next check-in can list them. Nothing is
        committed; failures are logged on the task, never fail it."""
        pid, tid = project["id"], task["id"]
        host, path = host_for(project), project["path"]
        changes, event = [], error
        if before is not None:
            try:
                changes = svnops.changed_between(before, svnops.dirty_fingerprint(host, path))
            except HostError as exc:
                event = f"Could not record file changes for check-in: {exc}"
                job.write(event + "\n")
        with self.store.edit_task(project, tid) as current:
            if current is None:
                return
            current["summary"] = (outcome.text or "").strip()[:1500]
            current["changes"] = changes
            current["checkin_pending"] = bool(changes)
            current["checkin"] = ""
            log_event(current, event or (f"Recorded {len(changes)} changed file{'s' * (len(changes) != 1)} "
                                         "for check-in" if changes else "No file changes"))

    # ---- check-in (SVN) ---------------------------------------------------
    def pending_checkin(self, project: dict) -> list[dict]:
        doc = self.store.read(project, fresh=True)
        return sorted((t for t in doc["tasks"] if t.get("checkin_pending")), key=lambda t: t["id"])

    def checkin(self, project: dict, message: str) -> str:
        """Commit the whole working copy and stamp the revision on the pending tasks. The caller
        checks that no AI job is active. On failure the tasks stay pending."""
        pid = project["id"]
        lock = self._sync_lock(pid)
        if not lock.acquire(blocking=False):
            raise ValueError("A check-in or task run is already in progress for this project.")
        try:
            with self.store.lock(pid):
                ids = [t["id"] for t in self.pending_checkin(project)]
                self._stamp(project, ids, checkin="pending")  # so the commit has current tasks.json
                try:
                    rev = svnops.checkin(host_for(project), project["path"], message)
                except BaseException:
                    self._stamp(project, ids, checkin="")
                    raise
                self._stamp(project, ids, checkin=rev, pending=False,
                            event=f"Checked in as {rev}" if rev else "Nothing to check in")
                return rev
        finally:
            lock.release()

    def _stamp(self, project: dict, ids: list[int], checkin: str, pending: bool = True, event: str = "") -> None:
        with self.store.edit(project) as doc:
            for tid in ids:
                current = find_task(doc, tid)
                if current is None:
                    continue
                current["checkin"] = checkin
                current["checkin_pending"] = pending
                if event:
                    log_event(current, event)

    # ---- remote sync -----------------------------------------------------
    def sync(self, project: dict, mode: str, push: bool = True) -> dict:
        """Manual sync with origin. The caller checks that no AI job is active."""
        if not gitops.tracked(project):
            raise ValueError("Git tracking is off for this project.")
        pid = project["id"]
        lock = self._sync_lock(pid)
        if not lock.acquire(blocking=False):
            raise ValueError("A sync or task run is already in progress for this project.")
        try:
            return gitops.sync(host_for(project), project["path"], mode=mode, push=push,
                               guard=self.store.lock(pid))
        finally:
            self.store.forget(pid)  # a pull may have replaced tasks.json
            lock.release()

    def record_approval(self, project: dict, tid: int, title: str, note: str) -> None:
        """Commit an approval note as an empty commit, in the background so the request doesn't
        wait on git or a push. Failures are logged on the task, never undo the approval."""
        threading.Thread(target=self._record_approval, args=(project, tid, title, note),
                         name=f"pg-approve-{project['id']}-{tid}", daemon=True).start()

    def _record_approval(self, project: dict, tid: int, title: str, note: str) -> None:
        pid = project["id"]
        host, path = host_for(project), project["path"]
        try:
            with self._sync_lock(pid):
                sha = gitops.note_commit(host, path, f"PatchGoblin: approve task #{tid} {title}\n\n{note}\n")
            event = f"Approval note committed {sha[:10]}" if sha \
                else "No commits yet; approval note kept on the task only"
        except HostError as exc:
            sha, event = "", f"Approval note not committed: {exc}"
        with self.store.edit_task(project, tid) as current:
            if current is not None:
                if sha:
                    current["approval_commit"] = sha
                log_event(current, event)
        if sha and project.get("auto_sync") is True:
            self._auto_sync(project, tid, message=f"PatchGoblin: record task #{tid} approval")

    def _auto_sync(self, project: dict, tid: int, job: Job | None = None,
                   message: str | None = None) -> None:
        """Sync after a task's commit. Failures are logged on the task, never fail it."""
        pid = project["id"]
        host, path = host_for(project), project["path"]
        write = job.write if job is not None else (lambda _text: None)
        try:
            if not gitops.tracked(project) or not gitops.get_remote(host, path):
                return
            write(f"[{now()}] Auto-sync with origin\n")
            # Recording the commit hash left tasks.json changed; commit it before pulling.
            with self._sync_lock(pid):
                result = gitops.sync(host, path, mode=project.get("sync_mode") or "ff-only", push=True,
                                     message=message or f"PatchGoblin: record task #{tid} result",
                                     guard=self.store.lock(pid))
            write("".join(f"  {line}\n" for line in result["log"]))
            event = "Synced with origin"
        except (HostError, ValueError) as exc:
            write(f"Auto-sync failed: {exc}\n")
            event = f"Auto-sync failed: {exc}"
        finally:
            self.store.forget(pid)
        with self.store.edit_task(project, tid) as current:
            if current is not None:
                log_event(current, event)

    # ---- chat ------------------------------------------------------------
    def chat(self, pid: str) -> Chat:
        with self._lock:
            return self.chats.setdefault(pid, Chat())

    def send_chat(self, project: dict, text: str) -> Chat:
        """Add the user's message and start the AI reply. Chat is read-only, like planning."""
        chat = self.chat(project["id"])
        with self._lock:
            if chat.job is not None:
                raise ValueError("The AI is still replying; wait or cancel first.")
            chat.add("user", text)
            chat.job = job = Job("chat")
            prompt = chat_prompt(chat.messages)
        threading.Thread(target=self._chat, args=(project, chat, prompt, job),
                         name=f"pg-chat-{project['id']}", daemon=True).start()
        return chat

    def _chat(self, project, chat, prompt, job) -> None:
        try:
            try:
                outcome = self._ai(project, {}, "plan", prompt, job, role="chat")
            except Cancelled:
                outcome = Outcome(False, error="Reply cancelled.")
            if outcome.ok:
                chat.add("assistant", outcome.text)
            else:
                chat.add("assistant", outcome.error or "The AI did not reply.", error=True)
        finally:
            with self._lock:
                if chat.job is job:
                    chat.job = None

    def clear_chat(self, pid: str) -> None:
        chat = self.chat(pid)
        if chat.job is not None:
            raise ValueError("The AI is still replying; wait or cancel first.")
        chat.messages.clear()

    def cancel(self, pid: str, tid: int) -> bool:
        job = self.jobs.get((pid, tid))
        if job is None:
            return False
        job.cancel()
        return True
