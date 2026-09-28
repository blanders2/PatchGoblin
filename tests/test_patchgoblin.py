import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from patchgoblin import create_app  # noqa: E402
from patchgoblin.hosts import HostError, LocalHost, SSHHost, terminal_command  # noqa: E402
from patchgoblin.providers import OpenAIAgent, ProjectFiles  # noqa: E402

FAKE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_agent.py")
H = {"X-PatchGoblin": "1"}


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
        for col in ("unplanned", "planned", "queue", "finished"):
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
        self.assertIn("working...", task["output"])

        # Uncommitted user work is checkpointed separately before the AI runs.
        with open(os.path.join(self.proj_dir, "notes.txt"), "w") as fh:
            fh.write("mine\n")
        self.assertEqual(self.action(pid, tid, "queue").status_code, 200)
        task = self.wait_for(pid, tid, {"done", "failed"})
        self.assertEqual(task["status"], "done", task["error"])
        self.assertTrue(os.path.exists(os.path.join(self.proj_dir, "agent_output.txt")))
        log = git_log(self.proj_dir)
        self.assertTrue(log[0].startswith("PatchGoblin: task #1 Create output file"))
        self.assertEqual(log[1], "PatchGoblin: checkpoint before task #1")
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.proj_dir,
                              capture_output=True, text=True).stdout.strip()
        self.assertEqual(task["commit"], head)

        with open(os.path.join(self.proj_dir, ".patchgoblin", "tasks.json"), encoding="utf-8") as fh:
            stored = json.load(fh)["tasks"][0]
        self.assertEqual(stored["status"], "done")

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

    def test_queue_and_dequeue(self):
        pid = self.add_project()["id"]
        engine = self.app.config["ENGINE"]
        tid = self.post_task(pid, "t")["id"]
        self.action(pid, tid, "mark_planned")
        with mock.patch.object(engine, "kick"):
            self.assertEqual(self.action(pid, tid, "queue").get_json()["status"], "queued")
        self.assertEqual(self.action(pid, tid, "dequeue").get_json()["status"], "planned")
        self.assertEqual(self.action(pid, tid, "bogus").status_code, 400)

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


if __name__ == "__main__":
    unittest.main()
