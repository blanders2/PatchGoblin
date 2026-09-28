import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from patchgoblin import create_app  # noqa: E402
from patchgoblin.hosts import HostError, LocalHost, SSHHost, terminal_command  # noqa: E402
from patchgoblin.providers import OpenAIAgent, Outcome, ProjectFiles  # noqa: E402

FAKE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_agent.py")
H = {"X-PatchGoblin": "1"}
ASKING_PLAN = "## Steps\n1. do it\n\n## Questions for you\n1. Which colour?\n"


def git_log(path):
    return subprocess.run(["git", "log", "--pretty=%s"], cwd=path, capture_output=True,
                          text=True).stdout.splitlines()


class AppTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = create_app(os.path.join(self.tmp.name, "data"), start_engine=False)
        self.app.config["SETTINGS"].update({"commands": {"claude": {
            "plan": [sys.executable, FAKE, "plan"], "run": [sys.executable, FAKE, "run"]}}})
        self.client = self.app.test_client()
        self.proj_dir = os.path.join(self.tmp.name, "proj")

    def add_project(self, **extra):
        res = self.client.post("/api/projects", headers=H,
                               json={"path": self.proj_dir, "provider": "claude", **extra})
        self.assertEqual(res.status_code, 201, res.get_json())
        return res.get_json()

    def post_task(self, pid, title, description=""):
        res = self.client.post(f"/api/projects/{pid}/tasks", headers=H,
                               json={"title": title, "description": description})
        self.assertEqual(res.status_code, 201)
        return res.get_json()

    def action(self, pid, tid, action, **extra):
        return self.client.post(f"/api/projects/{pid}/tasks/{tid}/action", headers=H,
                                json={"action": action, **extra})

    def wait_for(self, pid, tid, statuses, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            tasks = self.client.get(f"/api/projects/{pid}/tasks").get_json()["tasks"]
            task = next(t for t in tasks if t["id"] == tid)
            if task["status"] in statuses and not task["active"]:
                return task
            time.sleep(0.2)
        self.fail(f"task {tid} stuck in {task['status']}")


class ProjectTests(AppTestCase):
    def test_add_project_creates_dir_repo_and_tasks_file(self):
        project = self.add_project()
        self.assertTrue(os.path.isdir(os.path.join(self.proj_dir, ".git")))
        with open(os.path.join(self.proj_dir, ".patchgoblin", "tasks.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["tasks"], [])
        self.assertEqual(git_log(self.proj_dir), ["PatchGoblin: initial commit"])
        self.assertEqual(project["name"], "proj")
        dup = self.client.post("/api/projects", headers=H, json={"path": self.proj_dir})
        self.assertEqual(dup.status_code, 400)

    def test_existing_repo_is_left_alone(self):
        os.makedirs(self.proj_dir)
        subprocess.run(["git", "init", "-q"], cwd=self.proj_dir, check=True)
        self.add_project()
        self.assertEqual(git_log(self.proj_dir), [])
        self.assertFalse(os.path.exists(os.path.join(self.proj_dir, ".gitignore")))

    def test_missing_dir_without_create_is_rejected(self):
        res = self.client.post("/api/projects", headers=H, json={"path": self.proj_dir, "create": False})
        self.assertEqual(res.status_code, 400)

    def test_browse_lists_folders(self):
        for name in ("beta", "Alpha", ".hidden"):
            os.makedirs(os.path.join(self.tmp.name, "tree", name))
        open(os.path.join(self.tmp.name, "tree", "file.txt"), "w").close()
        tree = os.path.join(self.tmp.name, "tree")
        res = self.client.post("/api/browse", headers=H, json={"path": tree})
        data = res.get_json()
        self.assertEqual(res.status_code, 200, data)
        self.assertEqual([d["name"] for d in data["dirs"]], [".hidden", "Alpha", "beta"])
        self.assertEqual(data["dirs"][1]["path"], os.path.join(tree, "Alpha"))
        self.assertEqual(data["parent"], self.tmp.name)
        home = self.client.post("/api/browse", headers=H, json={"home": True}).get_json()
        self.assertEqual(home["path"], os.path.expanduser("~"))
        missing = self.client.post("/api/browse", headers=H, json={"path": os.path.join(tree, "nope")})
        self.assertEqual(missing.status_code, 502)
        self.assertEqual(self.client.post("/api/browse", json={"path": tree}).status_code, 403)

    @unittest.skipUnless(os.name == "nt", "drive list is Windows-only")
    def test_browse_windows_drives(self):
        data = self.client.post("/api/browse", headers=H, json={"path": ""}).get_json()
        self.assertEqual(data["path"], "")
        self.assertTrue(any(d["path"].upper().startswith("C:") for d in data["dirs"]))

    def test_writes_require_header(self):
        res = self.client.post("/api/projects", json={"path": self.proj_dir})
        self.assertEqual(res.status_code, 403)
        res = self.client.post("/api/projects", headers={**H, "Origin": "http://evil.example"},
                               json={"path": self.proj_dir})
        self.assertEqual(res.status_code, 403)

    def test_open_terminal(self):
        project = self.add_project()
        url = f"/api/projects/{project['id']}/terminal"
        with mock.patch("patchgoblin.hosts.subprocess.Popen") as popen:
            res = self.client.post(url, headers=H)
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(popen.call_count, 1)
        argv, kwargs = popen.call_args[0][0], popen.call_args[1]
        self.assertTrue(self.proj_dir in argv or kwargs["cwd"] == self.proj_dir, (argv, kwargs))
        self.assertEqual(self.client.post(url).status_code, 403)
        self.assertEqual(self.client.post("/api/projects/nope/terminal", headers=H).status_code, 404)

    def test_index_has_queue_tabs(self):
        html = self.client.get("/").get_data(as_text=True)
        for col in ("unplanned", "drafted", "planned", "queue", "finished"):
            self.assertIn(f'role="tab" id="tab-{col}" data-col="{col}"', html)
            self.assertIn(f'id="col-{col}" data-col="{col}" role="tabpanel" aria-labelledby="tab-{col}"', html)


class WorkflowTests(AppTestCase):
    def test_plan_queue_run_commit(self):
        pid = self.add_project()["id"]
        tid = self.post_task(pid, "Create output file", "Make agent_output.txt")["id"]

        self.assertEqual(self.action(pid, tid, "queue").status_code, 400)  # must be planned first
        self.assertEqual(self.action(pid, tid, "plan").status_code, 200)
        task = self.wait_for(pid, tid, {"planned"})
        self.assertIn("Step one", task["plan"])
        self.assertFalse(task["plan"].startswith("Title:"))
        self.assertEqual(task["title"], "Create agent output file")
        self.assertTrue(any(h["event"] == "Title rewritten by AI (was: Create output file)"
                            for h in task["history"]))
        self.assertIn("working...", task["output"])

        # Uncommitted user work is checkpointed separately before the AI runs.
        with open(os.path.join(self.proj_dir, "notes.txt"), "w") as fh:
            fh.write("mine\n")
        self.assertEqual(self.action(pid, tid, "queue").status_code, 200)
        task = self.wait_for(pid, tid, {"done", "failed"})
        self.assertEqual(task["status"], "done", task["error"])
        self.assertTrue(os.path.exists(os.path.join(self.proj_dir, "agent_output.txt")))
        log = git_log(self.proj_dir)
        self.assertTrue(log[0].startswith("PatchGoblin: task #1 Create agent output file"))
        self.assertEqual(log[1], "PatchGoblin: checkpoint before task #1")
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.proj_dir,
                              capture_output=True, text=True).stdout.strip()
        self.assertEqual(task["commit"], head)

        with open(os.path.join(self.proj_dir, ".patchgoblin", "tasks.json"), encoding="utf-8") as fh:
            stored = json.load(fh)["tasks"][0]
        self.assertEqual(stored["status"], "done")

    def test_plan_keeps_title_when_disabled(self):
        pid = self.add_project()["id"]
        res = self.client.patch(f"/api/projects/{pid}", headers=H, json={"rewrite_titles": False})
        self.assertIs(res.get_json()["rewrite_titles"], False)
        tid = self.post_task(pid, "Create output file")["id"]
        self.assertEqual(self.action(pid, tid, "plan").status_code, 200)
        task = self.wait_for(pid, tid, {"planned"})
        self.assertEqual(task["title"], "Create output file")
        self.assertNotIn("Title:", task["plan"])
        self.assertIn("Step one", task["plan"])

    def test_manual_plan_and_failed_run(self):
        pid = self.add_project()["id"]
        tid = self.post_task(pid, "This will FAIL")["id"]
        res = self.client.patch(f"/api/projects/{pid}/tasks/{tid}", headers=H, json={"plan": "do it"})
        self.assertEqual(res.get_json()["plan"], "do it")
        self.assertEqual(self.action(pid, tid, "mark_planned").get_json()["status"], "planned")
        self.action(pid, tid, "queue")
        task = self.wait_for(pid, tid, {"done", "failed"})
        self.assertEqual(task["status"], "failed")
        self.assertIn("exited with code 3", task["error"])
        # A failed task can be re-queued or sent back to planned.
        self.assertEqual(self.action(pid, tid, "mark_planned").get_json()["status"], "planned")

    def test_plan_questions_and_answers(self):
        pid = self.add_project()["id"]
        tid = self.post_task(pid, "ASK me things")["id"]
        self.assertEqual(self.action(pid, tid, "plan").status_code, 200)
        task = self.wait_for(pid, tid, {"drafted"})
        self.assertEqual(task["questions"], ["Which colour should the output be?", "Should it log?"])
        self.assertEqual(task["history"][-1]["event"], "AI plan drafted (2 open questions)")

        bad = self.action(pid, tid, "plan", answers="blue")
        self.assertEqual(bad.status_code, 400)
        bad = self.action(pid, tid, "plan", answers=[{"question": "q", "answer": 3}])
        self.assertEqual(bad.status_code, 400)

        answers = [{"question": task["questions"][0], "answer": "blue"},
                   {"question": task["questions"][1], "answer": "  "}]
        res = self.action(pid, tid, "plan", answers=answers)
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertIn("with answers", res.get_json()["history"][-1]["event"])
        task = self.wait_for(pid, tid, {"planned"})
        self.assertIn("A: blue", task["plan"])
        self.assertNotIn("Should it log", task["plan"])
        self.assertEqual(task["questions"], [])

    def patch_plan(self, pid, tid, plan):
        res = self.client.patch(f"/api/projects/{pid}/tasks/{tid}", headers=H, json={"plan": plan})
        self.assertEqual(res.status_code, 200, res.get_json())
        return res.get_json()

    def test_transitions_from_drafted(self):
        pid = self.add_project()["id"]
        engine = self.app.config["ENGINE"]
        tid = self.post_task(pid, "t")["id"]
        self.patch_plan(pid, tid, ASKING_PLAN)
        self.assertEqual(self.action(pid, tid, "mark_planned").get_json()["status"], "drafted")

        res = self.action(pid, tid, "mark_planned")
        self.assertEqual(res.status_code, 400)
        self.assertIn("open questions", res.get_json()["error"])
        self.assertEqual(self.action(pid, tid, "unplan").get_json()["status"], "unplanned")
        self.assertEqual(self.action(pid, tid, "mark_planned").get_json()["status"], "drafted")
        with mock.patch.object(engine, "kick"):
            self.assertEqual(self.action(pid, tid, "queue").get_json()["status"], "queued")
        # Removing it from the queue lands back in Drafted while the questions remain.
        self.assertEqual(self.action(pid, tid, "dequeue").get_json()["status"], "drafted")

        self.slow_ai(0.1)
        self.assertEqual(self.action(pid, tid, "plan").status_code, 200)
        self.assertEqual(self.wait_for(pid, tid, {"planned"})["plan"], "plan text")

    def test_edit_then_mark(self):
        pid = self.add_project()["id"]
        tid = self.post_task(pid, "t")["id"]
        self.patch_plan(pid, tid, ASKING_PLAN)
        self.action(pid, tid, "mark_planned")
        # Saving never changes the status; the buttons do.
        self.assertEqual(self.patch_plan(pid, tid, "## Steps\n1. do it\n\n## Questions for you\nNone.")["status"],
                         "drafted")
        self.assertEqual(self.action(pid, tid, "mark_planned").get_json()["status"], "planned")
        res = self.action(pid, tid, "mark_drafted")
        self.assertEqual(res.status_code, 400)
        self.assertIn("no open questions", res.get_json()["error"])
        self.assertEqual(self.patch_plan(pid, tid, ASKING_PLAN)["status"], "planned")
        tasks = self.client.get(f"/api/projects/{pid}/tasks").get_json()["tasks"]
        self.assertEqual(tasks[0]["status"], "planned")  # reconcile leaves hand-added questions alone
        task = self.action(pid, tid, "mark_drafted").get_json()
        self.assertEqual(task["status"], "drafted")
        self.assertEqual(task["history"][-1]["event"], "Moved to drafted")

    def test_batch_mark_planned_and_drafted(self):
        pid = self.add_project()["id"]
        a, b, c = (self.post_task(pid, t)["id"] for t in ("a", "b", "c"))
        self.patch_plan(pid, b, ASKING_PLAN)
        data = self.batch(pid, "mark_planned", [a, b, c]).get_json()
        self.assertTrue(all(r["ok"] for r in data["results"]))
        by_id = {t["id"]: t for t in data["tasks"]}
        self.assertEqual([by_id[i]["status"] for i in (a, b, c)], ["planned", "drafted", "planned"])
        self.assertEqual(by_id[b]["history"][-1]["event"], "Marked planned (plan has open questions)")

        self.patch_plan(pid, c, ASKING_PLAN)
        data = self.batch(pid, "mark_drafted", [a, b, c]).get_json()
        results = {r["id"]: r for r in data["results"]}
        self.assertIn("no open questions", results[a]["error"])
        self.assertIn("drafted", results[b]["error"])
        self.assertTrue(results[c]["ok"])
        data = self.batch(pid, "mark_planned", [b, c]).get_json()
        self.assertEqual([r["ok"] for r in data["results"]], [False, False])
        self.assertIn("open questions", data["results"][0]["error"])

    def test_batch_plan_drafted_or_planned(self):
        pid = self.add_project()["id"]
        engine = self.app.config["ENGINE"]
        ids = [self.post_task(pid, t)["id"] for t in ("ask 1", "plain", "ask 2")]

        def fake(project, task, mode, prompt, job):
            if task["title"].startswith("ask"):
                return Outcome(True, text=ASKING_PLAN)
            return Outcome(True, text="## Steps\n1. go\n\n## Questions for you\nNone.")
        with mock.patch.object(engine, "_ai", side_effect=fake):
            self.batch(pid, "plan", ids)
            statuses = [self.wait_for(pid, tid, {"drafted", "planned"})["status"] for tid in ids]
        self.assertEqual(statuses, ["drafted", "planned", "drafted"])

    def test_old_planned_tasks_with_questions_become_drafted(self):
        pid = self.add_project()["id"]
        a, b = (self.post_task(pid, t)["id"] for t in ("a", "b"))
        engine, store = self.app.config["ENGINE"], self.app.config["STORE"]
        path = os.path.join(self.proj_dir, ".patchgoblin", "tasks.json")

        def seed(version, plans):
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
            doc["version"] = version
            for task, plan in zip(doc["tasks"], plans):
                task["status"], task["plan"] = "planned", plan
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(doc, fh)
            store.forget(pid)
            return os.stat(path).st_mtime_ns

        project = self.app.config["REGISTRY"].get(pid)
        stamp = seed(1, ["plain plan", "plain plan"])
        engine.reconcile(project)
        self.assertEqual(os.stat(path).st_mtime_ns, stamp)  # nothing to migrate: not rewritten

        seed(1, [ASKING_PLAN, "plain plan"])
        engine.reconcile(project)
        doc = store.read(project, fresh=True)
        self.assertEqual([t["status"] for t in doc["tasks"]], ["drafted", "planned"])
        self.assertEqual(doc["tasks"][0]["history"][-1]["event"], "Plan has open questions")
        self.assertEqual(doc["version"], 2)

        stamp = seed(2, [ASKING_PLAN, ASKING_PLAN])
        engine.reconcile(project)  # already migrated: planned tasks keep their questions
        self.assertEqual(os.stat(path).st_mtime_ns, stamp)

    def test_queue_and_dequeue(self):
        pid = self.add_project()["id"]
        engine = self.app.config["ENGINE"]
        tid = self.post_task(pid, "t")["id"]
        self.action(pid, tid, "mark_planned")
        with mock.patch.object(engine, "kick"):
            self.assertEqual(self.action(pid, tid, "queue").get_json()["status"], "queued")
        self.assertEqual(self.action(pid, tid, "dequeue").get_json()["status"], "planned")
        self.assertEqual(self.action(pid, tid, "bogus").status_code, 400)

    def batch(self, pid, action, ids, **extra):
        return self.client.post(f"/api/projects/{pid}/tasks/batch", headers=H,
                                json={"action": action, "ids": ids, **extra})

    def slow_ai(self, delay=0.4):
        """Replace the engine's AI call with a slow fake that records peak planning concurrency."""
        engine = self.app.config["ENGINE"]
        seen = {"now": 0, "peak": 0, "calls": []}
        lock = threading.Lock()

        def fake(project, task, mode, prompt, job):
            with lock:
                seen["now"] += 1
                seen["peak"] = max(seen["peak"], seen["now"])
                seen["calls"].append(task["id"])
            time.sleep(delay)
            with lock:
                seen["now"] -= 1
            return Outcome(True, text="plan text")
        patcher = mock.patch.object(engine, "_ai", side_effect=fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return seen

    def test_batch_mark_planned_and_queue(self):
        pid = self.add_project()["id"]
        engine = self.app.config["ENGINE"]
        ids = [self.post_task(pid, f"t{i}")["id"] for i in range(3)]
        res = self.batch(pid, "mark_planned", ids)
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertTrue(all(r["ok"] for r in res.get_json()["results"]))
        with mock.patch.object(engine, "kick") as kick:
            data = self.batch(pid, "queue", ids).get_json()
        self.assertEqual(kick.call_count, 1)
        tasks = [t for t in data["tasks"] if t["id"] in ids]
        self.assertEqual({t["status"] for t in tasks}, {"queued"})
        self.assertEqual(len({t["queued_at"] for t in tasks}), 1)
        data = self.batch(pid, "dequeue", ids).get_json()
        self.assertEqual({t["status"] for t in data["tasks"]}, {"planned"})

    def test_batch_partial_failure(self):
        pid = self.add_project()["id"]
        engine = self.app.config["ENGINE"]
        a, b = (self.post_task(pid, t)["id"] for t in ("a", "b"))
        self.action(pid, b, "mark_planned")
        with mock.patch.object(engine, "kick") as kick:
            res = self.batch(pid, "queue", [a, b, 99, b])
        self.assertEqual(res.status_code, 200)
        results = {r["id"]: r for r in res.get_json()["results"]}
        self.assertEqual(list(results), [a, b, 99])
        self.assertFalse(results[a]["ok"])
        self.assertIn("unplanned", results[a]["error"])
        self.assertTrue(results[b]["ok"])
        self.assertEqual(results[99]["error"], "not found")
        self.assertEqual(kick.call_count, 1)
        with mock.patch.object(engine, "kick") as kick:
            self.batch(pid, "queue", [a])
        kick.assert_not_called()

    def test_batch_plan_unlimited(self):
        pid = self.add_project()["id"]
        ids = [self.post_task(pid, f"t{i}")["id"] for i in range(3)]
        seen = self.slow_ai()
        res = self.batch(pid, "plan", ids, feedback="keep it short")
        self.assertTrue(all(r["ok"] for r in res.get_json()["results"]), res.get_json())
        for tid in ids:
            self.assertEqual(self.wait_for(pid, tid, {"planned"})["plan"], "plan text")
        self.assertEqual(seen["peak"], 3)

    def test_batch_plan_respects_limit(self):
        pid = self.add_project()["id"]
        res = self.client.patch(f"/api/projects/{pid}", headers=H, json={"plan_limit": 1})
        self.assertEqual(res.get_json()["plan_limit"], 1)
        ids = [self.post_task(pid, f"t{i}")["id"] for i in range(3)]
        seen = self.slow_ai(0.2)
        self.batch(pid, "plan", ids)
        for tid in ids:
            self.wait_for(pid, tid, {"planned"})
        self.assertEqual(seen["peak"], 1)
        self.assertEqual(sorted(seen["calls"]), ids)

    def test_plan_limit_validation(self):
        pid = self.add_project()["id"]
        url = f"/api/projects/{pid}"
        for bad in (-1, "abc", 1.5, True):
            self.assertEqual(self.client.patch(url, headers=H, json={"plan_limit": bad}).status_code, 400, bad)
        for value, stored in ((0, 0), ("", 0), (None, 0), ("3", 3), (2, 2)):
            self.assertEqual(self.client.patch(url, headers=H, json={"plan_limit": value})
                             .get_json()["plan_limit"], stored)

    def test_batch_cancel_waiting_planner(self):
        pid = self.add_project()["id"]
        self.client.patch(f"/api/projects/{pid}", headers=H, json={"plan_limit": 1})
        a, b = (self.post_task(pid, t)["id"] for t in ("a", "b"))
        self.action(pid, b, "mark_planned")
        seen = self.slow_ai(1.5)
        self.action(pid, a, "plan")
        deadline = time.time() + 5
        while not seen["calls"] and time.time() < deadline:
            time.sleep(0.05)
        self.action(pid, b, "plan")
        res = self.batch(pid, "cancel", [b])
        self.assertTrue(res.get_json()["results"][0]["ok"], res.get_json())
        task = self.wait_for(pid, b, {"planned"})
        self.assertIn("cancelled", task["error"])
        self.assertIn("Waiting for a planning slot", task["output"])
        self.wait_for(pid, a, {"planned"})
        self.assertEqual(seen["calls"], [a])
        res = self.batch(pid, "cancel", [a])
        self.assertIn("No AI job", res.get_json()["results"][0]["error"])

    def test_batch_delete_and_provider_skip_locked(self):
        pid = self.add_project()["id"]
        a, b, c = (self.post_task(pid, t)["id"] for t in ("a", "b", "c"))
        path = os.path.join(self.proj_dir, ".patchgoblin", "tasks.json")
        engine = self.app.config["ENGINE"]
        engine.jobs[(pid, c)] = mock.Mock()  # keep reconcile() from resetting the fake "running" task
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        doc["tasks"][2]["status"] = "running"
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        self.app.config["STORE"].forget(pid)

        data = self.batch(pid, "set_provider", [a, b, c], provider="codex").get_json()
        self.assertEqual([r["ok"] for r in data["results"]], [True, True, False])
        by_id = {t["id"]: t for t in data["tasks"]}
        self.assertEqual((by_id[a]["provider"], by_id[c]["provider"]), ("codex", ""))
        self.assertEqual(by_id[a]["history"][-1]["event"], "Edited provider")
        self.assertEqual(self.batch(pid, "set_provider", [a], provider="nope").status_code, 400)
        data = self.batch(pid, "set_provider", [a], provider="").get_json()
        self.assertEqual(data["tasks"][0]["provider"], "")

        data = self.batch(pid, "delete", [a, b, c]).get_json()
        self.assertEqual([r["ok"] for r in data["results"]], [True, True, False])
        self.assertEqual([t["id"] for t in data["tasks"]], [c])
        del engine.jobs[(pid, c)]

    def test_batch_validation(self):
        pid = self.add_project()["id"]
        tid = self.post_task(pid, "t")["id"]
        for action, ids in (("bogus", [tid]), ("queue", []), ("queue", ["1"]), ("queue", [True]),
                            ("queue", "1"), ("queue", list(range(201)))):
            self.assertEqual(self.batch(pid, action, ids).status_code, 400, (action, ids))
        res = self.client.post(f"/api/projects/{pid}/tasks/batch", json={"action": "queue", "ids": [tid]})
        self.assertEqual(res.status_code, 403)
        self.assertEqual(self.batch("nope", "queue", [tid]).status_code, 404)

    def test_interrupted_tasks_are_reconciled(self):
        pid = self.add_project()["id"]
        tid = self.post_task(pid, "t")["id"]
        path = os.path.join(self.proj_dir, ".patchgoblin", "tasks.json")
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        doc["tasks"][0]["status"] = "running"
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        self.app.config["STORE"].forget(pid)
        task = self.wait_for(pid, tid, {"failed"}, timeout=5)
        self.assertIn("interrupted", task["error"])

        # An interrupted re-plan of a drafted task goes back to Drafted.
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        doc["tasks"][0].update(status="planning", prev_status="drafted", plan=ASKING_PLAN)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        self.app.config["STORE"].forget(pid)
        task = self.wait_for(pid, tid, {"drafted"}, timeout=5)
        self.assertIn("Planning was interrupted", task["error"])


def git(path, *args, check=True):
    return subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@example.com", *args],
                          cwd=path, capture_output=True, text=True, check=check).stdout.strip()


class RemoteTests(AppTestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {"GIT_TERMINAL_PROMPT": "0"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.bare = os.path.join(self.tmp.name, "origin.git")
        subprocess.run(["git", "init", "-q", "--bare", self.bare], check=True)

    def setup_remote(self):
        pid = self.add_project()["id"]
        res = self.client.put(f"/api/projects/{pid}/remote", headers=H, json={"url": self.bare})
        self.assertEqual(res.status_code, 200, res.get_json())
        return pid, res.get_json()

    def sync(self, pid, **body):
        return self.client.post(f"/api/projects/{pid}/remote/sync", headers=H, json=body)

    def branch(self):
        return git(self.proj_dir, "symbolic-ref", "--short", "HEAD")

    def clone(self):
        other = os.path.join(self.tmp.name, "other")
        subprocess.run(["git", "clone", "-q", "-b", self.branch(), self.bare, other], check=True)
        return other

    def commit_file(self, repo, name, push=False):
        with open(os.path.join(repo, name), "w") as fh:
            fh.write(name + "\n")
        git(repo, "add", name)
        git(repo, "commit", "-q", "-m", f"add {name}")
        if push:
            git(repo, "push", "-q", "origin", "HEAD")

    def run_task(self, pid):
        tid = self.post_task(pid, "Create output file")["id"]
        self.client.patch(f"/api/projects/{pid}/tasks/{tid}", headers=H, json={"plan": "do it"})
        self.action(pid, tid, "mark_planned")
        self.assertEqual(self.action(pid, tid, "queue").status_code, 200)
        return self.wait_for(pid, tid, {"done", "failed"})

    def test_remote_set_and_status(self):
        pid, status = self.setup_remote()
        self.assertEqual(status["url"], self.bare)
        self.assertEqual(status["branch"], self.branch())
        self.assertGreaterEqual(status["ahead"], 1)
        self.assertEqual(status["upstream"], "")
        got = self.client.get(f"/api/projects/{pid}/remote").get_json()
        self.assertEqual(got["url"], self.bare)
        self.assertIs(got["auto_sync"], False)
        self.assertEqual(got["sync_mode"], "ff-only")
        # An empty URL removes the remote.
        res = self.client.put(f"/api/projects/{pid}/remote", headers=H, json={"url": ""})
        self.assertEqual(res.get_json()["url"], "")
        self.assertEqual(self.sync(pid).status_code, 502)

    def test_set_remote_rejects_option_like_url(self):
        pid = self.add_project()["id"]
        for bad in ("--upload-pack=touch /tmp/x", "a b", "x\ny"):
            res = self.client.put(f"/api/projects/{pid}/remote", headers=H, json={"url": bad})
            self.assertEqual(res.status_code, 400, bad)
        self.assertEqual(git(self.proj_dir, "remote"), "")

    def test_sync_pushes_to_empty_remote(self):
        pid, _ = self.setup_remote()
        res = self.sync(pid)
        data = res.get_json()
        self.assertEqual(res.status_code, 200, data)
        head = git(self.proj_dir, "rev-parse", "HEAD")
        self.assertEqual(git(self.bare, "rev-parse", self.branch()), head)
        self.assertEqual((data["ahead"], data["behind"]), (0, 0))
        self.assertEqual(data["upstream"], f"origin/{self.branch()}")
        self.assertTrue(any("Pushed" in line for line in data["log"]))

    def test_sync_commits_dirty_tree_first(self):
        pid, _ = self.setup_remote()
        with open(os.path.join(self.proj_dir, "notes.txt"), "w") as fh:
            fh.write("mine\n")
        self.assertEqual(self.sync(pid).status_code, 200)
        self.assertEqual(git_log(self.proj_dir)[0], "PatchGoblin: checkpoint before sync")
        self.assertEqual(git(self.bare, "log", "-1", "--pretty=%s", self.branch()),
                         "PatchGoblin: checkpoint before sync")

    def test_sync_pulls_fast_forward(self):
        pid, _ = self.setup_remote()
        self.assertEqual(self.sync(pid).status_code, 200)
        other = self.clone()
        self.commit_file(other, "remote.txt", push=True)
        res = self.sync(pid, push=False)
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertTrue(os.path.exists(os.path.join(self.proj_dir, "remote.txt")))
        self.assertEqual(git_log(self.proj_dir)[0], "add remote.txt")

    def diverge(self, pid):
        self.assertEqual(self.sync(pid).status_code, 200)
        other = self.clone()
        self.commit_file(other, "remote.txt", push=True)
        self.commit_file(self.proj_dir, "local.txt")

    def test_sync_diverged_ff_only_fails_cleanly(self):
        pid, _ = self.setup_remote()
        self.diverge(pid)
        head = git(self.proj_dir, "rev-parse", "HEAD")
        res = self.sync(pid, mode="ff-only")
        self.assertEqual(res.status_code, 502)
        self.assertIn("fast-forward", res.get_json()["error"])
        self.assertEqual(git(self.proj_dir, "rev-parse", "HEAD"), head)
        self.assertEqual(git(self.proj_dir, "status", "--porcelain"), "")
        gitdir = os.path.join(self.proj_dir, ".git")
        for marker in ("MERGE_HEAD", "rebase-merge", "rebase-apply"):
            self.assertFalse(os.path.exists(os.path.join(gitdir, marker)))

    def test_sync_rebase_mode(self):
        pid, _ = self.setup_remote()
        self.diverge(pid)
        res = self.sync(pid, mode="rebase")
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(git_log(self.proj_dir)[:2], ["add local.txt", "add remote.txt"])
        self.assertEqual(git(self.proj_dir, "rev-list", "--merges", "--count", "HEAD"), "0")
        self.assertEqual(git(self.bare, "rev-parse", self.branch()), git(self.proj_dir, "rev-parse", "HEAD"))

    def test_sync_rebase_conflict_is_aborted(self):
        pid, _ = self.setup_remote()
        self.assertEqual(self.sync(pid).status_code, 200)
        other = self.clone()
        self.commit_file(other, "same.txt", push=True)
        with open(os.path.join(self.proj_dir, "same.txt"), "w") as fh:
            fh.write("different\n")
        git(self.proj_dir, "add", "same.txt")
        git(self.proj_dir, "commit", "-q", "-m", "local same")
        res = self.sync(pid, mode="rebase")
        self.assertEqual(res.status_code, 502)
        self.assertIn("aborted", res.get_json()["error"])
        gitdir = os.path.join(self.proj_dir, ".git")
        self.assertFalse(os.path.exists(os.path.join(gitdir, "rebase-merge")))
        self.assertEqual(git_log(self.proj_dir)[0], "local same")

    def test_sync_rejected_while_busy(self):
        pid, _ = self.setup_remote()
        self.app.config["ENGINE"].jobs[(pid, 99)] = object()
        try:
            self.assertEqual(self.sync(pid).status_code, 400)
        finally:
            del self.app.config["ENGINE"].jobs[(pid, 99)]
        self.assertEqual(self.sync(pid, mode="bogus").status_code, 400)
        self.assertEqual(self.sync(pid).status_code, 200)

    def test_auto_sync_off_by_default(self):
        pid, _ = self.setup_remote()
        task = self.run_task(pid)
        self.assertEqual(task["status"], "done", task["error"])
        self.assertEqual(git(self.bare, "rev-list", "--all"), "")

    def test_auto_sync_pushes_after_task(self):
        pid, _ = self.setup_remote()
        self.client.patch(f"/api/projects/{pid}", headers=H, json={"auto_sync": True})
        task = self.run_task(pid)
        self.assertEqual(task["status"], "done", task["error"])
        self.assertTrue(any(h["event"] == "Synced with origin" for h in task["history"]), task["history"])
        pushed = git(self.bare, "log", "--pretty=%s", self.branch()).splitlines()
        self.assertTrue(any(s.startswith("PatchGoblin: task #1") for s in pushed), pushed)
        self.assertIn(task["commit"], git(self.bare, "rev-list", self.branch()))

    def test_auto_sync_failure_does_not_fail_task(self):
        pid = self.add_project()["id"]
        self.client.put(f"/api/projects/{pid}/remote", headers=H,
                        json={"url": os.path.join(self.tmp.name, "missing.git")})
        self.client.patch(f"/api/projects/{pid}", headers=H, json={"auto_sync": True})
        task = self.run_task(pid)
        self.assertEqual(task["status"], "done", task["error"])
        self.assertTrue(any(h["event"].startswith("Auto-sync failed") for h in task["history"]),
                        task["history"])

    def test_update_project_auto_sync_field(self):
        pid = self.add_project()["id"]
        res = self.client.patch(f"/api/projects/{pid}", headers=H,
                                json={"auto_sync": True, "sync_mode": "rebase"})
        self.assertIs(res.get_json()["auto_sync"], True)
        self.assertEqual(res.get_json()["sync_mode"], "rebase")
        got = self.client.get(f"/api/projects/{pid}/remote").get_json()
        self.assertEqual((got["auto_sync"], got["sync_mode"]), (True, "rebase"))
        res = self.client.patch(f"/api/projects/{pid}", headers=H, json={"sync_mode": "merge"})
        self.assertEqual(res.status_code, 400)

    def test_ssh_run_env_is_quoted(self):
        host = SSHHost("me@box")
        with mock.patch("patchgoblin.hosts.communicate") as comm:
            comm.return_value = mock.Mock(ok=True)
            host.run(["git", "fetch"], cwd="/srv/p", env={"GIT_TERMINAL_PROMPT": "0"})
        self.assertEqual(comm.call_args.args[0][-1], "cd /srv/p && env GIT_TERMINAL_PROMPT=0 git fetch")


class ChatTests(AppTestCase):
    def wait_chat(self, pid, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            chat = self.client.get(f"/api/projects/{pid}/chat").get_json()
            if not chat["active"]:
                return chat
            time.sleep(0.2)
        self.fail("chat reply never finished")

    def test_chat_runs_read_only_in_project(self):
        pid = self.add_project()["id"]
        url = f"/api/projects/{pid}/chat"
        self.assertEqual(self.client.post(url, headers=H, json={"message": " "}).status_code, 400)
        self.assertEqual(self.client.post(url, json={"message": "hi"}).status_code, 403)
        res = self.client.post(url, headers=H, json={"message": "What does this project do?"})
        self.assertEqual(res.status_code, 200, res.get_json())
        chat = self.wait_chat(pid)
        self.assertEqual([m["role"] for m in chat["messages"]], ["user", "assistant"])
        self.assertIn("Step one", chat["messages"][1]["text"])  # the fake agent's read-only reply
        self.assertFalse(os.path.exists(os.path.join(self.proj_dir, "agent_output.txt")))

        self.client.post(url, headers=H, json={"message": "Now FAIL please"})
        chat = self.wait_chat(pid)
        self.assertTrue(chat["messages"][-1]["error"])
        self.assertIn("exited with code 3", chat["messages"][-1]["text"])

        self.assertEqual(self.client.post(url + "/cancel", headers=H).status_code, 400)
        self.assertEqual(self.client.delete(url, headers=H).get_json()["messages"], [])
        self.assertEqual(self.client.get("/api/projects/nope/chat").status_code, 404)

    def test_chat_prompt_keeps_recent_history(self):
        from patchgoblin.providers import MAX_CHAT_CONTEXT, chat_prompt
        messages = [{"role": "user", "text": "old " * MAX_CHAT_CONTEXT},
                    {"role": "assistant", "text": "an answer"}, {"role": "user", "text": "latest question"}]
        prompt = chat_prompt(messages)
        self.assertNotIn("old old", prompt)
        self.assertLess(prompt.index("an answer"), prompt.index("latest question"))
        self.assertIn("DO NOT create, modify or delete", prompt)


class PlanPromptTests(unittest.TestCase):
    def test_plan_questions_parsing(self):
        from patchgoblin.providers import plan_questions
        plan = ("Summary.\n\n## Steps\n1. Do a thing\n\n## Questions for you\n"
                "1. Should we keep the old API\n   for existing callers?\n2) Which DB?\n- Bullet one\n\n"
                "Trailing prose.\n## Other\n1. not a question")
        self.assertEqual(plan_questions(plan), [
            "Should we keep the old API for existing callers?", "Which DB?", "Bullet one"])
        self.assertEqual(plan_questions("### Open questions or risks\n- Is X ok?"), ["Is X ok?"])
        self.assertEqual(plan_questions("## Questions for you\nNone."), [])
        self.assertEqual(plan_questions("## Questions for you\n1. None"), [])
        self.assertEqual(plan_questions("1. Step\n2. Step"), [])
        self.assertEqual(plan_questions(""), [])

    def test_plan_prompt_with_answers(self):
        from patchgoblin.providers import plan_prompt
        task = {"id": 1, "title": "T", "description": "", "plan": "old plan"}
        prompt = plan_prompt(task, "use tabs", [{"question": "Colour?", "answer": "blue"},
                                                {"question": "Log?", "answer": " "}])
        self.assertIn("## Answers to your questions\nQ: Colour?\nA: blue", prompt)
        self.assertNotIn("Log?", prompt)
        self.assertIn("use tabs", prompt)
        self.assertIn("## Questions for you", prompt)
        self.assertNotIn("## Answers", plan_prompt(task))

    def test_plan_prompt_title_instruction(self):
        from patchgoblin.providers import plan_prompt
        task = {"id": 1, "title": "T", "description": "", "plan": ""}
        self.assertNotIn("Title: <", plan_prompt(task))
        self.assertIn("Title: <", plan_prompt(task, rewrite_title=True))

    def test_split_title(self):
        from patchgoblin.providers import MAX_TITLE, split_title
        self.assertEqual(split_title("Title: Add X\n\n1. Step"), ("Add X", "1. Step"))
        self.assertEqual(split_title("**Title:** Add X\n\nPlan"), ("Add X", "Plan"))
        self.assertEqual(split_title("# Title: `Add X`\nPlan"), ("Add X", "Plan"))
        self.assertEqual(split_title("\n\n  title - \"Add   X\"\n\nPlan"), ("Add X", "Plan"))
        self.assertEqual(split_title("1. Step\nTitle: late"), ("", "1. Step\nTitle: late"))
        self.assertEqual(split_title("# Title of the plan\nPlan"), ("", "# Title of the plan\nPlan"))
        self.assertEqual(split_title("Title: \"\"\nPlan"), ("", "Title: \"\"\nPlan"))
        self.assertEqual(split_title(""), ("", ""))
        title, plan = split_title("Title: " + "x" * 300 + "\nPlan")
        self.assertEqual(len(title), MAX_TITLE)
        self.assertEqual(plan, "Plan")


class HostTests(unittest.TestCase):
    def test_ssh_target_validation(self):
        for bad in ("", "-oProxyCommand=calc", "user@host; rm -rf /", "a b"):
            with self.assertRaises(HostError):
                SSHHost(bad)
        self.assertEqual(SSHHost("me@box", 2222).label, "me@box:2222")

    def test_ssh_commands_are_quoted(self):
        host = SSHHost("me@box")
        with mock.patch("patchgoblin.hosts.communicate") as comm:
            comm.return_value = mock.Mock(ok=True)
            host.run(["claude", "-p", "it's"], cwd="/srv/my app", input="prompt")
        argv = comm.call_args.args[0]
        self.assertEqual(argv[-2], "me@box")
        self.assertEqual(argv[-1], "cd '/srv/my app' && claude -p 'it'\"'\"'s'")
        self.assertIn("BatchMode=yes", argv)

    def test_terminal_commands(self):
        local, ssh = LocalHost(), SSHHost("me@box", 2222)
        have = lambda names: (lambda n: n if n in names else None)  # noqa: E731
        self.assertEqual(terminal_command(local, "C:\\a b", "win32", have({"wt"})),
                         (["wt", "-d", "C:\\a b"], None))
        self.assertEqual(terminal_command(local, "C:\\a", "win32", have(set())),
                         (["powershell.exe", "-NoExit"], "C:\\a"))
        argv, cwd = terminal_command(ssh, "/srv/my app", "win32", have({"wt"}))
        self.assertEqual(argv[:6], ["wt", "ssh", "-t", "-p", "2222", "me@box"])
        self.assertIn("cd '/srv/my app'", argv[6])
        self.assertIsNone(cwd)
        self.assertEqual(terminal_command(local, "/a", "darwin", have(set()))[0],
                         ["open", "-a", "Terminal", "/a"])
        self.assertIn("ssh -t -p 2222 me@box", terminal_command(ssh, "/a", "darwin", have(set()))[0][2])
        self.assertEqual(terminal_command(local, "/a", "linux", have({"xterm"})), (["xterm"], "/a"))
        argv, cwd = terminal_command(ssh, "/a", "linux", have({"gnome-terminal", "xterm"}))
        self.assertEqual(argv[:3], ["gnome-terminal", "--", "ssh"])
        with self.assertRaises(HostError):
            terminal_command(local, "/a", "linux", have(set()))

    def test_ssh_list_dirs_parses_find_output(self):
        host = SSHHost("me@box")
        out = "/home/me/code\n./zeta\n./api server\n./.config\n"
        with mock.patch("patchgoblin.hosts.communicate") as comm:
            comm.return_value = mock.Mock(ok=True, returncode=0, stdout=out, stderr="")
            data = host.list_dirs("/home/me/code/")
        self.assertIn("cd /home/me/code ", comm.call_args.args[0][-1])
        self.assertEqual([d["name"] for d in data["dirs"]], [".config", "api server", "zeta"])
        self.assertEqual(data["dirs"][1]["path"], "/home/me/code/api server")
        self.assertEqual((data["path"], data["parent"], data["sep"]), ("/home/me/code", "/home/me", "/"))
        with mock.patch("patchgoblin.hosts.communicate") as comm:
            comm.return_value = mock.Mock(ok=False, returncode=45, stdout="", stderr="")
            with self.assertRaises(HostError):
                host.list_dirs("/nope")

    def test_project_files_are_confined(self):
        with tempfile.TemporaryDirectory() as root:
            files = ProjectFiles(LocalHost(), root)
            self.assertEqual(files.resolve("src/a.py"), os.path.join(root, "src", "a.py"))
            for bad in ("../x", "/etc/passwd", "a/../../x", "C:/x"):
                with self.assertRaises(ValueError):
                    files.resolve(bad)
            with self.assertRaises(ValueError):
                files.resolve(".git/config", for_write=True)


class OpenAIAgentTests(unittest.TestCase):
    def test_tool_loop_writes_files_in_run_mode(self):
        with tempfile.TemporaryDirectory() as root:
            job = mock.Mock(cancelled=False)
            job.elapsed.return_value = 0
            agent = OpenAIAgent(LocalHost(), root, {"base_url": "http://x", "model": "m", "max_steps": 5},
                                "", "run", job)
            replies = [
                {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [{
                    "id": "c1", "type": "function",
                    "function": {"name": "write_file",
                                 "arguments": json.dumps({"path": "hello.txt", "content": "hi"})}}]}}]},
                {"choices": [{"message": {"role": "assistant", "content": "Wrote hello.txt"}}]},
            ]
            with mock.patch.object(agent, "_request", side_effect=replies):
                outcome = agent.run("do it", timeout=60)
            self.assertTrue(outcome.ok)
            self.assertEqual(outcome.text, "Wrote hello.txt")
            with open(os.path.join(root, "hello.txt")) as fh:
                self.assertEqual(fh.read(), "hi")

    def test_plan_mode_has_no_write_tool(self):
        agent = OpenAIAgent(LocalHost(), ".", {"model": "m", "allow_commands": True}, "", "plan", mock.Mock())
        names = {t["function"]["name"] for t in agent.tools()}
        self.assertEqual(names, {"list_files", "read_file", "search"})

    # ---- HTTP-level behaviour (urlopen patched) ---------------------------------
    def job(self):
        job = mock.Mock(cancelled=False)
        job.elapsed.return_value = 0
        return job

    def ep(self, **extra):
        return {"id": "or", "name": "OpenRouter", "base_url": "https://or.example/api/v1", "model": "m",
                "max_steps": 5, **extra}

    def run_agent(self, ep, replies, root=".", mode="plan"):
        """Run the agent against canned JSON replies; returns (outcome, requests sent)."""
        sent = []

        def fake_urlopen(req, timeout=None):
            sent.append(req)
            resp = mock.MagicMock()
            resp.__enter__.return_value.read.return_value = json.dumps(replies[len(sent) - 1]).encode()
            return resp

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            outcome = OpenAIAgent(LocalHost(), root, ep, "", mode, self.job()).run("go", timeout=60)
        return outcome, sent

    DONE = {"choices": [{"message": {"role": "assistant", "content": "done"}}]}

    def test_saved_key_wins_over_env_and_env_is_fallback(self):
        with mock.patch.dict(os.environ, {"PG_TEST_KEY": "envkey"}):
            _, sent = self.run_agent(self.ep(api_key="saved", api_key_env="PG_TEST_KEY"), [self.DONE])
            self.assertEqual(sent[0].get_header("Authorization"), "Bearer saved")
            _, sent = self.run_agent(self.ep(api_key_env="PG_TEST_KEY"), [self.DONE])
            self.assertEqual(sent[0].get_header("Authorization"), "Bearer envkey")

    def test_keyless_sends_no_authorization(self):
        outcome, sent = self.run_agent(self.ep(), [self.DONE])
        self.assertTrue(outcome.ok)
        self.assertIsNone(sent[0].get_header("Authorization"))

    def test_extra_headers(self):
        headers = {"HTTP-Referer": "https://x", "X-Bad": "a\r\nInjected: 1", "Authorization": "Bearer user"}
        _, sent = self.run_agent(self.ep(api_key="k", headers=headers), [self.DONE])
        got = {k.lower(): v for k, v in sent[0].header_items()}
        self.assertEqual(got["http-referer"], "https://x")
        self.assertNotIn("x-bad", got)
        self.assertEqual(got["authorization"], "Bearer k")
        _, sent = self.run_agent(self.ep(headers={"Authorization": "Bearer user"}), [self.DONE])
        self.assertEqual(sent[0].get_header("Authorization"), "Bearer user")

    def test_base_url_variants(self):
        for base in ("https://h/v1/", "https://h/v1/chat/completions", " https://h/v1 "):
            _, sent = self.run_agent(self.ep(base_url=base), [self.DONE])
            self.assertEqual(sent[0].full_url, "https://h/v1/chat/completions")
        outcome, sent = self.run_agent(self.ep(base_url="ftp://h"), [self.DONE])
        self.assertFalse(outcome.ok)
        self.assertIn("http", outcome.error)

    def test_error_reply_and_missing_choices_fail(self):
        outcome, _ = self.run_agent(self.ep(), [{"error": {"message": "No endpoints found that support tool use"}}])
        self.assertFalse(outcome.ok)
        self.assertIn("No endpoints found that support tool use", outcome.error)
        self.assertIn("OpenRouter", outcome.error)
        outcome, _ = self.run_agent(self.ep(), [{"choices": []}])
        self.assertFalse(outcome.ok)
        self.assertIn("no choices", outcome.error)

    def test_http_error_uses_endpoint_name_and_message(self):
        import io
        import urllib.error
        err = urllib.error.HTTPError("u", 401, "no", {}, io.BytesIO(b'{"error": {"message": "bad key"}}'))
        self.addCleanup(err.close)
        with mock.patch("urllib.request.urlopen", side_effect=err):
            outcome = OpenAIAgent(LocalHost(), ".", self.ep(api_key="k"), "", "plan", self.job()).run("go", 60)
        self.assertEqual(outcome.error, "OpenRouter API error 401: bad key")

    def test_tool_call_without_id_and_dict_arguments(self):
        with tempfile.TemporaryDirectory() as root:
            replies = [{"choices": [{"message": {
                "role": "assistant", "content": "", "reasoning": "secret thoughts",
                "tool_calls": [{"type": "function", "function": {
                    "name": "write_file", "arguments": {"path": "a.txt", "content": "hi"}}}]}}]},
                self.DONE]
            outcome, sent = self.run_agent(self.ep(), replies, root=root, mode="run")
            self.assertTrue(outcome.ok, outcome.error)
            with open(os.path.join(root, "a.txt")) as fh:
                self.assertEqual(fh.read(), "hi")
            messages = json.loads(sent[1].data)["messages"]
            echo, tool = messages[2], messages[3]
            self.assertNotIn("reasoning", echo)
            self.assertEqual(echo["tool_calls"][0]["id"], "call_0_0")
            self.assertEqual(json.loads(echo["tool_calls"][0]["function"]["arguments"]), {"path": "a.txt", "content": "hi"})
            self.assertEqual(tool["tool_call_id"], "call_0_0")

    def test_missing_env_key_fails_early_for_remote_hosts_only(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PG_MISSING_KEY", None)
            outcome, sent = self.run_agent(self.ep(api_key_env="PG_MISSING_KEY"), [self.DONE])
            self.assertFalse(outcome.ok)
            self.assertIn("PG_MISSING_KEY", outcome.error)
            self.assertEqual(sent, [])
            outcome, sent = self.run_agent(self.ep(api_key_env="PG_MISSING_KEY",
                                                   base_url="http://localhost:11434/v1"), [self.DONE])
            self.assertTrue(outcome.ok)

    def test_list_models(self):
        from patchgoblin.providers import list_models
        data = {"data": [{"id": "b"}, {"id": "a"},
                         {"id": "no-tools", "supported_parameters": ["temperature"]},
                         {"id": "tools", "supported_parameters": ["tools", "temperature"]}]}
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = json.dumps(data).encode()
        with mock.patch("urllib.request.urlopen", return_value=resp) as urlopen:
            self.assertEqual(list_models(self.ep()), ["a", "b", "tools"])
        self.assertEqual(urlopen.call_args.args[0].full_url, "https://or.example/api/v1/models")
        import io
        import urllib.error
        err = urllib.error.HTTPError("u", 404, "no", {}, io.BytesIO(b"not here"))
        self.addCleanup(err.close)
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaisesRegex(RuntimeError, "404"):
                list_models(self.ep())


class EndpointSettingsTests(AppTestCase):
    def put(self, **data):
        return self.client.put("/api/settings", headers=H, json=data)

    def endpoint(self, **extra):
        return {"name": "OpenRouter", "base_url": "https://openrouter.ai/api/v1", "model": "x/y", **extra}

    def test_legacy_openai_block_migrates(self):
        from patchgoblin.providers import run_ai
        from patchgoblin.store import Settings
        data_dir = os.path.join(self.tmp.name, "legacy")
        os.makedirs(data_dir)
        with open(os.path.join(data_dir, "settings.json"), "w") as fh:
            json.dump({"openai": {"base_url": "http://old/v1", "model": "old-model"}}, fh)
        s = Settings(data_dir).get()
        self.assertEqual(len(s["endpoints"]), 1)
        ep = s["endpoints"][0]
        self.assertEqual((ep["id"], ep["base_url"], ep["model"], ep["api_key_env"]),
                         ("openai", "http://old/v1", "old-model", "OPENAI_API_KEY"))
        self.assertNotIn("openai", s)
        job = mock.Mock(cancelled=False)
        with mock.patch("patchgoblin.providers.OpenAIAgent") as agent:
            agent.return_value.run.return_value = Outcome(True, "ok")
            out = run_ai("openai", "plan", "p", host=None, project={"path": "."}, settings=s, model="", job=job)
        self.assertTrue(out.ok)
        self.assertEqual(agent.call_args.args[2]["id"], "openai")
        out = run_ai("deleted-id", "plan", "p", host=None, project={"path": "."}, settings=s, model="", job=job)
        self.assertFalse(out.ok)
        self.assertIn("not configured", out.error)

    def test_save_endpoints_and_validation(self):
        res = self.put(endpoints=[self.endpoint(api_key="sk-secret"),
                                  self.endpoint(name="Local", base_url="http://localhost:11434/v1")])
        self.assertEqual(res.status_code, 200, res.get_json())
        data = res.get_json()
        self.assertEqual([e["id"] for e in data["endpoints"]], ["openrouter", "local"])
        self.assertEqual([p["id"] for p in data["providers"]], ["claude", "codex", "openrouter", "local"])
        self.assertIn("x/y", data["models"]["openrouter"])
        self.assertNotIn("sk-secret", json.dumps(data))
        got = self.client.get("/api/settings").get_json()
        self.assertNotIn("sk-secret", json.dumps(got))
        self.assertTrue(got["endpoints"][0]["api_key_saved"])
        self.assertFalse(got["endpoints"][1]["api_key_saved"])
        self.assertEqual(self.app.config["SETTINGS"].get()["endpoints"][0]["api_key"], "sk-secret")

        # A blank key keeps the saved one; api_key_clear removes it.
        self.put(endpoints=[self.endpoint(id="openrouter"), self.endpoint(id="local", name="Local")])
        self.assertEqual(self.app.config["SETTINGS"].get()["endpoints"][0]["api_key"], "sk-secret")
        self.put(endpoints=[self.endpoint(id="openrouter", api_key_clear=True)])
        s = self.app.config["SETTINGS"].get()
        self.assertEqual(s["endpoints"][0]["api_key"], "")
        self.assertEqual(len(s["endpoints"]), 1)

        for bad in ([self.endpoint(id="a"), self.endpoint(id="a")], [self.endpoint(id="claude")],
                    [self.endpoint(id="Bad Id")], [self.endpoint(name="")], [self.endpoint(base_url="ftp://x")],
                    [self.endpoint(max_steps=0)], [self.endpoint(headers={"X": "a\nb"})]):
            self.assertEqual(self.put(endpoints=bad).status_code, 400, bad)

    def test_projects_and_tasks_accept_endpoint_ids(self):
        self.put(endpoints=[self.endpoint(id="or")])
        project = self.add_project()
        pid = project["id"]
        res = self.client.patch(f"/api/projects/{pid}", headers=H, json={"provider": "or"})
        self.assertEqual(res.get_json()["provider"], "or")
        self.assertEqual(self.client.patch(f"/api/projects/{pid}", headers=H,
                                           json={"provider": "nope"}).status_code, 400)
        res = self.client.post(f"/api/projects/{pid}/tasks", headers=H, json={"title": "t", "provider": "or"})
        self.assertEqual(res.status_code, 201)
        tid = res.get_json()["id"]
        self.assertEqual(self.client.post(f"/api/projects/{pid}/tasks", headers=H,
                                          json={"title": "t", "provider": "nope"}).status_code, 400)
        self.assertEqual(self.client.patch(f"/api/projects/{pid}/tasks/{tid}", headers=H,
                                           json={"provider": "nope"}).status_code, 400)
        res = self.client.post(f"/api/projects/{pid}/tasks/batch", headers=H,
                               json={"action": "set_provider", "ids": [tid], "provider": "or"})
        self.assertEqual(res.status_code, 200)
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn('value="or">OpenRouter<', page)

    def test_models_route_caches_and_refreshes(self):
        self.put(endpoints=[self.endpoint(id="or", models=["pinned"])])
        with mock.patch("patchgoblin.app.list_models", return_value=["live-a", "pinned"]) as lm:
            data = self.client.get("/api/endpoints/or/models").get_json()
            self.assertEqual(data, {"models": ["pinned", "x/y", "live-a"], "error": ""})
            self.client.get("/api/endpoints/or/models")
            self.assertEqual(lm.call_count, 1)
            self.client.get("/api/endpoints/or/models?refresh=1")
            self.assertEqual(lm.call_count, 2)
        with mock.patch("patchgoblin.app.list_models", side_effect=RuntimeError("down")):
            data = self.client.get("/api/endpoints/or/models?refresh=1").get_json()
            self.assertEqual(data, {"models": ["pinned", "x/y"], "error": "down"})
        self.assertEqual(self.client.get("/api/endpoints/nope/models").status_code, 404)

    def test_endpoint_test_uses_saved_key(self):
        self.put(endpoints=[self.endpoint(id="or", api_key="sk-saved")])
        with mock.patch("patchgoblin.app.list_models", return_value=["a", "b"]) as lm:
            res = self.client.post("/api/endpoints/test", headers=H, json=self.endpoint(id="or"))
        self.assertEqual(res.get_json(), {"ok": True, "error": "", "count": 2})
        self.assertEqual(lm.call_args.args[0]["api_key"], "sk-saved")


class ModelTests(AppTestCase):
    def resolve(self, project, task=None, role="plan"):
        return self.app.config["ENGINE"].provider_for(project, task or {}, role)

    def set_settings(self, **values):
        self.app.config["SETTINGS"].update(values)

    def test_provider_for_roles(self):
        p = {"provider": "claude", "plan_model": "opus", "code_model": "sonnet", "chat_model": ""}
        self.assertEqual(self.resolve(p, role="plan"), ("claude", "opus"))
        self.assertEqual(self.resolve(p, role="run"), ("claude", "sonnet"))
        self.assertEqual(self.resolve(p, role="chat"), ("claude", "opus"))
        self.assertEqual(self.resolve({**p, "chat_model": "haiku"}, role="chat"), ("claude", "haiku"))
        task = {"code_model": "x"}
        self.assertEqual(self.resolve(p, task, "run"), ("claude", "x"))
        self.assertEqual(self.resolve(p, task, "plan"), ("claude", "opus"))
        # Legacy (unmigrated) project dicts still resolve.
        self.assertEqual(self.resolve({"provider": "claude", "model": "old"}, role="run"), ("claude", "old"))

    def test_provider_for_other_provider_and_globals(self):
        p = {"provider": "claude", "plan_model": "opus", "code_model": "sonnet"}
        codex_task = {"provider": "codex"}
        self.assertEqual(self.resolve(p, codex_task, "run"), ("codex", ""))
        self.assertEqual(self.resolve(p, {**codex_task, "plan_model": "gpt-5"}), ("codex", "gpt-5"))
        self.set_settings(commands={"codex": {"code_model": "gpt-5-codex"},
                                    "claude": {"code_model": "global-code"}})
        self.assertEqual(self.resolve(p, codex_task, "run"), ("codex", "gpt-5-codex"))
        blank = {"provider": "claude", "plan_model": "", "code_model": ""}
        self.assertEqual(self.resolve(blank, role="run"), ("claude", "global-code"))
        self.assertEqual(self.resolve(blank, role="plan"), ("claude", ""))

    def test_provider_for_endpoint_code_model_falls_back(self):
        self.set_settings(endpoints=[{"id": "or", "name": "OR", "base_url": "http://x/v1", "model": "m-plan"}])
        p = {"provider": "or", "plan_model": "", "code_model": ""}
        self.assertEqual(self.resolve(p, role="run"), ("or", "m-plan"))
        self.set_settings(endpoints=[{"id": "or", "name": "OR", "base_url": "http://x/v1",
                                      "model": "m-plan", "code_model": "m-code"}])
        self.assertEqual(self.resolve(p, role="run"), ("or", "m-code"))
        self.assertEqual(self.resolve(p, role="chat"), ("or", "m-plan"))

    def test_registry_migration(self):
        from patchgoblin.store import Registry
        data_dir = os.path.join(self.tmp.name, "legacy")
        os.makedirs(data_dir)
        path = os.path.join(data_dir, "projects.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"projects": [{"id": "a", "provider": "claude", "model": "opus", "extra": 1},
                                    {"id": "b", "provider": "codex", "plan_model": "p", "code_model": "c"}]}, fh)
        Registry(data_dir)
        a, b = Registry(data_dir).list()
        self.assertEqual({k: a.get(k) for k in ("plan_model", "code_model", "chat_model", "extra")},
                         {"plan_model": "opus", "code_model": "opus", "chat_model": "", "extra": 1})
        self.assertNotIn("model", a)
        self.assertEqual(b, {"id": "b", "provider": "codex", "plan_model": "p", "code_model": "c"})
        with open(path + ".bak", encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["projects"][0]["model"], "opus")
        before = os.path.getmtime(path)
        time.sleep(0.05)
        Registry(data_dir)
        self.assertEqual(os.path.getmtime(path), before)

    def test_project_routes(self):
        p = self.add_project(plan_model=" opus ", code_model="sonnet ", chat_model="")
        self.assertEqual((p["plan_model"], p["code_model"], p["chat_model"]), ("opus", "sonnet", ""))
        self.assertNotIn("model", p)
        url = f"/api/projects/{p['id']}"
        p = self.client.patch(url, headers=H, json={"chat_model": " haiku", "code_model": ""}).get_json()
        self.assertEqual((p["plan_model"], p["code_model"], p["chat_model"]), ("opus", "", "haiku"))
        p = self.client.patch(url, headers=H, json={"model": "legacy"}).get_json()
        self.assertEqual(p["plan_model"], "legacy")
        self.assertEqual(self.client.patch(url, headers=H, json={"plan_model": 3}).status_code, 400)

    def test_task_models_edit_and_batch(self):
        pid = self.add_project()["id"]
        a, b, c = (self.post_task(pid, t)["id"] for t in ("a", "b", "c"))
        # An old task without the model keys can still be edited.
        path = os.path.join(self.proj_dir, ".patchgoblin", "tasks.json")
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        for t in doc["tasks"]:
            t.pop("plan_model"), t.pop("code_model")
        doc["tasks"][2]["status"] = "running"
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        store = self.app.config["STORE"]
        store.forget(pid)
        self.app.config["ENGINE"].jobs[(pid, c)] = mock.Mock()
        self.addCleanup(self.app.config["ENGINE"].jobs.pop, (pid, c))
        res = self.client.patch(f"/api/projects/{pid}/tasks/{a}", headers=H, json={"code_model": " x "})
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual((res.get_json()["code_model"], res.get_json().get("plan_model", "")), ("x", ""))
        self.assertEqual(res.get_json()["history"][-1]["event"], "Edited code_model")

        batch = f"/api/projects/{pid}/tasks/batch"
        data = self.client.post(batch, headers=H, json={"action": "set_models", "ids": [a, b, c],
                                                        "plan_model": "opus"}).get_json()
        self.assertEqual([r["ok"] for r in data["results"]], [True, True, False])
        by_id = {t["id"]: t for t in data["tasks"]}
        self.assertEqual((by_id[a]["plan_model"], by_id[a]["code_model"]), ("opus", "x"))
        self.assertEqual((by_id[b]["plan_model"], by_id[b].get("code_model", "")), ("opus", ""))
        self.assertNotIn("plan_model", by_id[c])
        self.assertEqual(by_id[b]["history"][-1]["event"], "Edited models")
        res = self.client.post(batch, headers=H, json={"action": "set_models", "ids": [a]})
        self.assertEqual(res.status_code, 400)
        # Changing the provider clears model overrides chosen for the old one.
        data = self.client.post(batch, headers=H, json={"action": "set_provider", "ids": [a],
                                                        "provider": "codex"}).get_json()
        task = next(t for t in data["tasks"] if t["id"] == a)
        self.assertEqual((task["provider"], task["plan_model"], task["code_model"]), ("codex", "", ""))

    def test_settings_models_round_trip(self):
        res = self.client.put("/api/settings", headers=H, json={
            "commands": {"claude": {"plan_model": " opus ", "code_model": "sonnet"}},
            "endpoints": [{"name": "OR", "base_url": "http://x/v1", "model": "a", "code_model": " b "}]})
        data = res.get_json()
        self.assertEqual(res.status_code, 200, data)
        self.assertEqual((data["commands"]["claude"]["plan_model"], data["commands"]["claude"]["code_model"]),
                         ("opus", "sonnet"))
        self.assertEqual(data["commands"]["codex"]["plan_model"], "")
        self.assertEqual(data["endpoints"][0]["code_model"], "b")
        self.assertIn("b", data["models"]["or"])
        bad = self.client.put("/api/settings", headers=H, json={"commands": {"claude": {"plan_model": 5}}})
        self.assertEqual(bad.status_code, 400)

    def test_job_log_names_model(self):
        p = self.add_project(plan_model="opus", code_model="sonnet")
        job = mock.Mock()
        with mock.patch("patchgoblin.engine.run_ai", return_value=Outcome(True, "ok")) as run_ai:
            self.app.config["ENGINE"]._ai(p, {}, "run", "prompt", job)
            self.app.config["ENGINE"]._ai(p, {}, "plan", "prompt", job, role="chat")
        self.assertEqual([c.kwargs["model"] for c in run_ai.call_args_list], ["sonnet", "opus"])
        self.assertIn("run with claude / sonnet in", job.write.call_args_list[0].args[0])


if __name__ == "__main__":
    unittest.main()
