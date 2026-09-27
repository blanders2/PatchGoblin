"""Standard-library-only project agent, also sent over SSH without installation."""
import contextlib
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
import urllib.request
import uuid


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


@contextlib.contextmanager
def lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as stream:
        if os.fstat(stream.fileno()).st_size == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        deadline = time.monotonic() + (10 if path.name == "store.lock" else 0)
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (OSError, BlockingIOError):
                if time.monotonic() >= deadline:
                    raise ValueError("Project is busy. Try again shortly.") from None
                time.sleep(0.05)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def atomic_json(path, data):
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".tasks-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def command(args, cwd, input_text=None, timeout=120):
    # Resolve .cmd wrappers on Windows; input prompts are always stdin, never shell text.
    args = list(args)
    args[0] = shutil.which(args[0]) or args[0]
    proc = subprocess.Popen(args, cwd=cwd, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace",
                            start_new_session=os.name != "nt")
    try:
        out, err = proc.communicate(input_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, timeout=15)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
        proc.kill()
        proc.communicate()
        raise ValueError("Command timed out; inspect the project before retrying.") from None
    if proc.returncode:
        raise ValueError((err or out or f"Command exited {proc.returncode}")[-6000:])
    return out


class Project:
    def __init__(self, config):
        self.config = config
        path = Path(config["path"]).expanduser()
        if not path.is_absolute():
            raise ValueError("Project directory must be an absolute path.")
        self.root = path.resolve()
        self.meta = self.root / ".patchgoblin"
        self.file = self.meta / "tasks.json"

    def git(self, *args):
        return command(["git", *args], self.root)

    def load(self):
        data = json.loads(self.file.read_text(encoding="utf-8"))
        if data.get("version") != 1 or not isinstance(data.get("tasks"), list):
            raise ValueError("Unsupported tasks.json format; original file was preserved.")
        return data

    def save(self, data):
        data["updated_at"] = now()
        atomic_json(self.file, data)

    def init(self):
        self.root.mkdir(parents=True, exist_ok=True)
        self.meta.mkdir(exist_ok=True)
        new_repo = not (self.root / ".git").exists()
        if new_repo:
            self.git("init")
            ignore = self.root / ".gitignore"
            with ignore.open("a", encoding="utf-8") as stream:
                stream.write("\n# PatchGoblin defaults\n.env\n.env.*\n!.env.example\n*.pem\n*.key\n.venv/\nnode_modules/\n__pycache__/\n.patchgoblin-app/\n")
        # Keep operational locks and outputs out of commits, including existing repos.
        exclude = Path(self.git("rev-parse", "--git-path", "info/exclude").strip())
        if not exclude.is_absolute():
            exclude = self.root / exclude
        exclude.parent.mkdir(parents=True, exist_ok=True)
        rules = "\n.patchgoblin/*.lock\n.patchgoblin/*.log\n.patchgoblin/.tasks-*.tmp\n"
        if not exclude.exists() or ".patchgoblin/*.lock" not in exclude.read_text(encoding="utf-8"):
            with exclude.open("a", encoding="utf-8") as stream:
                stream.write(rules)
        with lock(self.meta / "store.lock"):
            if not self.file.exists():
                self.save({"version": 1, "tasks": []})
            if new_repo:
                # Bootstrap only the files we own. Existing source needs the user's checkpoint.
                self.git("add", "--", ".gitignore", ".patchgoblin/tasks.json")
                self.git("-c", "user.name=PatchGoblin", "-c", "user.email=patchgoblin@localhost",
                         "commit", "-m", "Initialize PatchGoblin task tracking")
            return {"path": str(self.root), "tasks": self.load()["tasks"]}

    def task(self, data, task_id):
        for task in data["tasks"]:
            if task["id"] == task_id:
                return task
        raise ValueError("Task not found.")

    def transition(self, task, status):
        task["status"] = status
        task["updated_at"] = now()
        task.setdefault("history", []).append({"status": status, "at": now()})

    def mutate(self, action, payload):
        with lock(self.meta / "store.lock"):
            data = self.load()
            if action == "create":
                title = str(payload.get("title", "")).strip()
                if not title or len(title) > 200:
                    raise ValueError("A title of 1–200 characters is required.")
                task = {"id": uuid.uuid4().hex, "title": title,
                        "description": str(payload.get("description", ""))[:30000],
                        "plan": "", "created_at": now(), "result": "", "error": "",
                        "provider": self.config["provider"], "commit": "", "messages": []}
                self.transition(task, "unplanned")
                data["tasks"].append(task)
            else:
                task = self.task(data, payload["task_id"])
                state = task["status"]
                if action == "edit":
                    if state not in {"unplanned", "planned", "failed", "validation"}:
                        raise ValueError("Only idle tasks can be edited.")
                    for key in ("title", "description", "plan"):
                        if key in payload:
                            value = str(payload[key]).strip()
                            if key == "title" and (not value or len(value) > 200):
                                raise ValueError("A title of 1–200 characters is required.")
                            task[key] = value[:50000]
                    task["updated_at"] = now()
                elif action in {"plan", "discuss"}:
                    if state not in {"unplanned", "planned", "failed", "validation"}:
                        raise ValueError("Task is already queued, active, or completed.")
                    if action == "discuss":
                        message = str(payload.get("message", "")).strip()
                        if not message or len(message) > 20000:
                            raise ValueError("Enter a message of 1–20,000 characters.")
                        task.setdefault("messages", []).append({"role": "user", "content": message, "at": now()})
                    task["error"] = ""
                    task["queued_at"] = now()
                    self.transition(task, "revising_queued" if state == "validation" else "planning_queued")
                elif action == "mark_planned":
                    if state not in {"unplanned", "failed"}:
                        raise ValueError("Task cannot be marked planned from its current state.")
                    task["error"] = ""
                    self.transition(task, "planned")
                elif action == "queue":
                    if state not in {"planned", "validation"}:
                        raise ValueError("Plan the task or mark it planned before queuing.")
                    task["queue_origin"] = state
                    task["queued_at"] = now()
                    task["error"] = ""
                    self.transition(task, "queued")
                elif action == "unqueue":
                    if state not in {"queued", "planning_queued", "revising_queued"}:
                        raise ValueError("Only waiting tasks can be removed from the queue.")
                    destination = task.get("queue_origin", "planned") if state == "queued" else "validation" if state == "revising_queued" else "unplanned"
                    self.transition(task, destination)
                elif action == "complete":
                    if state != "validation":
                        raise ValueError("Only tasks in validation can be marked complete.")
                    task["validated_at"] = now()
                    self.transition(task, "completed")
                elif action == "reopen":
                    if state not in {"planned", "completed", "failed"}:
                        raise ValueError("Active tasks cannot be reopened.")
                    task["error"] = ""
                    self.transition(task, "unplanned")
                else:
                    raise ValueError("Unknown task action.")
            self.save(data)
            return task

    def checkpoint(self, message):
        self.git("add", "--all")
        if self.git("diff", "--cached", "--name-only").strip():
            self.git("-c", "user.name=PatchGoblin", "-c", "user.email=patchgoblin@localhost",
                     "commit", "-m", message)
        return self.git("rev-parse", "HEAD").strip()

    def inspect(self):
        data = self.load()
        return {**data, "branch": self.git("branch", "--show-current").strip(),
                "git_status": self.git("status", "--short"),
                "providers": {"codex": bool(shutil.which("codex")),
                              "claude": bool(shutil.which("claude")),
                              "openai": bool(os.environ.get("OPENAI_API_KEY"))}}

    def run(self, task_id):
        with lock(self.meta / "run.lock"):
            with lock(self.meta / "store.lock"):
                data = self.load()
                task = self.task(data, task_id)
                if task["status"] not in {"queued", "planning_queued", "revising_queued"}:
                    raise ValueError("Task is no longer queued.")
                revising = task["status"] == "revising_queued"
                planning = task["status"] in {"planning_queued", "revising_queued"}
                self.transition(task, "revising" if revising else "planning" if planning else "running")
                task["provider"] = self.config["provider"]
                task["started_at"] = now()
                task["error"] = ""
                self.save(data)
            try:
                # Refuse to fold unrelated edits into an AI result commit.
                dirty = self.git("status", "--porcelain", "--", ".", ":(exclude).patchgoblin")
                if not planning and dirty.strip():
                    raise ValueError("Commit existing project changes first (use Git checkpoint), then mark this task planned and queue it again.")
                prompt = self.prompt(task, planning)
                output = self.provider(prompt, planning)
                if not output.strip():
                    raise ValueError("The AI returned no result.")
                with lock(self.meta / "store.lock"):
                    data = self.load()
                    task = self.task(data, task_id)
                    task["plan" if planning else "result"] = output[-100000:]
                    if planning:
                        task.setdefault("messages", []).append({"role": "assistant", "content": output[-100000:], "at": now()})
                    self.transition(task, "validation" if revising or not planning else "planned")
                    task["finished_at"] = now()
                    self.save(data)
                    if not planning:
                        task["commit"] = self.checkpoint(f"PatchGoblin: {task['title']}")
                        self.save(data)
                return task
            except Exception as exc:
                with lock(self.meta / "store.lock"):
                    data = self.load()
                    task = self.task(data, task_id)
                    task["error"] = str(exc)[-8000:]
                    self.transition(task, "validation" if revising else "failed")
                    task["finished_at"] = now()
                    self.save(data)
                return task

    def prompt(self, task, planning):
        return (
            f"You are working in project directory {self.root}. Follow AGENTS.md and project instructions.\n"
            "Do not modify .patchgoblin or .git, commit, push, or publish. PatchGoblin handles Git.\n"
            + ("Read the relevant project files. Discuss the user's latest feedback and collaboratively refine the implementation or fix plan. Answer questions directly, explain your diagnosis, and include an updated actionable plan with acceptance criteria and verification steps. Do not change files. The user decides when to queue implementation. Return plain Markdown.\n" if planning else
               "Implement the task and its plan in this directory. Verify what you can and report changes, checks, and remaining limitations accurately.\n")
            + f"\nTASK: {task['title']}\n{task['description']}\n\nEXISTING PLAN:\n{task['plan']}"
            + f"\n\nLAST IMPLEMENTATION RESULT:\n{task.get('result', '')}\nLAST ERROR:\n{task.get('error', '')}"
            + "\n\nPLANNING CONVERSATION (most recent messages):\n"
            + "\n\n".join(f"{m['role'].upper()}: {m['content']}" for m in task.get("messages", [])[-12:])
        )

    def provider(self, prompt, planning):
        provider = self.config["provider"]
        model = self.config.get("model", "")
        if provider == "openai":
            return self.api_agent(prompt, planning)
        if provider == "codex":
            args = ["codex", "exec", "--sandbox", "read-only" if planning else "workspace-write"]
            if model:
                args += ["--model", model]
            args += ["-"]
            return command(args, self.root, prompt, timeout=1800)
        if provider == "claude":
            allowed = "Read,Glob,Grep" if planning else "Read,Glob,Grep,Edit,Write"
            if not planning and self.config.get("allow_commands"):
                allowed += ",Bash"
            args = ["claude", "-p", "--output-format", "json", "--tools", allowed,
                    "--allowedTools", allowed, "--permission-mode", "plan" if planning else "acceptEdits"]
            if model:
                args += ["--model", model]
            result = json.loads(command(args, self.root, prompt, timeout=1800))
            if result.get("is_error"):
                raise ValueError(result.get("result", "Claude reported an error."))
            return result.get("result", "")
        raise ValueError("Unknown AI provider.")

    def safe_path(self, relative):
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("Path must stay inside the project.")
        parts = path.relative_to(self.root).parts
        if any(p in {".git", ".patchgoblin", ".patchgoblin-app"} or p.startswith(".env")
               or p.endswith((".pem", ".key")) for p in parts):
            raise ValueError("Path is reserved or contains credentials.")
        return path

    def file_tool(self, name, args, planning):
        if name == "list_files":
            files = self.git("ls-files", "--cached", "--others", "--exclude-standard", "-z").split("\0")
            visible = []
            for filename in files:
                if not filename:
                    continue
                try:
                    self.safe_path(filename)
                    visible.append(filename)
                except ValueError:
                    pass
            return "\n".join(visible)[:50000]
        if name == "read_file":
            path = self.safe_path(args["path"])
            if path.stat().st_size > 200000:
                raise ValueError("File exceeds 200 KB; read a smaller source file.")
            return path.read_text(encoding="utf-8")
        if name == "write_file" and not planning:
            path = self.safe_path(args["path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(args["content"], encoding="utf-8")
            return "Saved " + args["path"]
        if name == "run_command" and not planning and self.config.get("allow_commands"):
            argv = args["argv"]
            if not isinstance(argv, list) or not argv or not all(isinstance(v, str) for v in argv):
                raise ValueError("argv must be a nonempty list of strings.")
            return command(argv, self.root, timeout=120)[-20000:]
        raise ValueError("Tool is not available in this mode.")

    def api_agent(self, prompt, planning):
        key = os.environ.get("OPENAI_API_KEY")
        model = self.config.get("model")
        if not key or not model:
            raise ValueError("OpenAI requires OPENAI_API_KEY on the project host and a model in project settings.")
        definitions = [
            ("list_files", "List project files, respecting Git ignore rules.", {}),
            ("read_file", "Read a UTF-8 file relative to the project.", {"path": {"type": "string"}}),
        ]
        if not planning:
            definitions.append(("write_file", "Create or replace a UTF-8 project file.", {
                "path": {"type": "string"}, "content": {"type": "string"}}))
            if self.config.get("allow_commands"):
                definitions.append(("run_command", "Run a program with the project as its working directory. No shell expansion.", {
                    "argv": {"type": "array", "items": {"type": "string"}}}))
        tools = [{"type": "function", "name": name, "description": desc,
                  "parameters": {"type": "object", "properties": props,
                                 "required": list(props), "additionalProperties": False}, "strict": True}
                 for name, desc, props in definitions]
        messages = [{"role": "user", "content": prompt}]
        deadline = time.monotonic() + 1800
        for _ in range(40):
            if time.monotonic() >= deadline:
                raise ValueError("AI run exceeded the 30-minute limit. Inspect changes before retrying.")
            request = urllib.request.Request("https://api.openai.com/v1/responses",
                data=json.dumps({"model": model, "input": messages, "tools": tools, "store": False}).encode(),
                headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=180) as response:
                data = json.load(response)
            if data.get("status") != "completed":
                raise ValueError("OpenAI response did not complete: " + str(data.get("status")))
            messages.extend(data["output"])
            calls = [item for item in data["output"] if item["type"] == "function_call"]
            if not calls:
                return "\n".join(part["text"] for item in data["output"] if item["type"] == "message"
                                 for part in item.get("content", []) if part["type"] == "output_text")
            for call in calls:
                try:
                    result = self.file_tool(call["name"], json.loads(call["arguments"]), planning)
                except Exception as exc:
                    result = "Tool error: " + str(exc)
                messages.append({"type": "function_call_output", "call_id": call["call_id"], "output": result})
        raise ValueError("AI reached the 40-step limit. Inspect changes before retrying.")


def dispatch(request):
    project = Project(request["project"])
    action, payload = request["action"], request.get("payload", {})
    if action == "init":
        return project.init()
    if action == "inspect":
        return project.inspect()
    if action == "run":
        return project.run(payload["task_id"])
    if action == "checkpoint":
        with lock(project.meta / "run.lock"), lock(project.meta / "store.lock"):
            return {"commit": project.checkpoint("PatchGoblin: user checkpoint")}
    if action == "recover":
        with lock(project.meta / "run.lock"), lock(project.meta / "store.lock"):
            data = project.load()
            task = project.task(data, payload["task_id"])
            if task["status"] not in {"running", "planning", "revising"}:
                raise ValueError("Only interrupted active tasks can be recovered.")
            task["error"] = "Run interrupted. Inspect project changes before retrying."
            project.transition(task, "validation" if task["status"] == "revising" else "failed")
            project.save(data)
            return task
    return project.mutate(action, payload)


def main(request):
    try:
        print(json.dumps({"ok": True, "data": dispatch(request)}, ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))


if __name__ == "__main__":
    import sys
    main(json.load(sys.stdin))
