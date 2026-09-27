import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from patchgoblin.app import Registry, Transport, Worker, create_app, validate_config
from patchgoblin.host import Project, dispatch, lock


class ProjectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = validate_config({"name": "Test", "path": self.temp.name})
        self.project = Project(self.config)
        self.project.init()

    def create(self, title="Build a greeting"):
        return self.project.mutate("create", {"title": title, "description": "Write hello.txt"})

    def action(self, task, action, **kwargs):
        return self.project.mutate(action, {"task_id": task["id"], **kwargs})

    def test_full_lifecycle_with_real_git_and_reload(self):
        task = self.create()
        with self.assertRaisesRegex(ValueError, "Plan the task"):
            self.action(task, "queue")
        self.action(task, "plan")
        with patch.object(Project, "provider", return_value="1. Write hello.txt\n2. Check content"):
            planned = self.project.run(task["id"])
        self.assertEqual(planned["status"], "planned")
        self.assertIn("Write hello.txt", planned["plan"])
        self.project.checkpoint("Initial checkpoint")
        self.action(task, "queue")
        def implement(prompt, planning):
            self.assertFalse(planning)
            self.assertIn(str(self.project.root), prompt)
            (self.project.root / "hello.txt").write_text("hello\n", encoding="utf-8")
            return "Created hello.txt; checked content."
        with patch.object(Project, "provider", side_effect=implement):
            completed = self.project.run(task["id"])
        self.assertEqual(completed["status"], "validation")
        self.assertEqual(len(completed["commit"]), 40)
        self.assertEqual(self.project.git("show", "HEAD:hello.txt"), "hello\n")
        loaded = Project(self.config).load()["tasks"][0]
        self.assertEqual(loaded["commit"], completed["commit"])
        self.assertFalse(self.project.git("ls-files", ".patchgoblin/*.lock").strip())
        self.assertEqual(self.action(task, "complete")["status"], "completed")

    def test_validation_discussion_revision_requeue_and_explicit_completion(self):
        task = self.create()
        self.project.checkpoint("Initial")
        self.action(task, "mark_planned")
        with self.assertRaisesRegex(ValueError, "Only tasks in validation"):
            self.action(task, "complete")
        self.action(task, "queue")
        with patch.object(Project, "provider", return_value="Built the first version"):
            self.assertEqual(self.project.run(task["id"])["status"], "validation")
        feedback = self.action(task, "discuss", message="The greeting is wrong. Let's fix the plan.")
        self.assertEqual(feedback["status"], "revising_queued")
        self.assertIn("greeting is wrong", feedback["messages"][-1]["content"])
        def revise(prompt, planning):
            self.assertTrue(planning)
            self.assertIn("greeting is wrong", prompt)
            self.assertIn("Built the first version", prompt)
            return "Change greeting to hello; validate with a text assertion."
        with patch.object(Project, "provider", side_effect=revise):
            revised = self.project.run(task["id"])
        self.assertEqual(revised["status"], "validation")
        self.assertIn("text assertion", revised["plan"])
        self.assertEqual(revised["messages"][-1]["role"], "assistant")
        self.action(task, "queue")
        self.assertEqual(self.action(task, "unqueue")["status"], "validation")
        self.action(task, "queue")
        with patch.object(Project, "provider", return_value="Fixed and verified"):
            self.assertEqual(self.project.run(task["id"])["status"], "validation")
        self.assertEqual(self.action(task, "complete")["status"], "completed")

    def test_manual_planning_queue_removal_and_invalid_transition(self):
        task = self.create()
        self.action(task, "mark_planned")
        self.action(task, "queue")
        with self.assertRaisesRegex(ValueError, "Only idle"):
            self.action(task, "edit", title="Changed")
        removed = self.action(task, "unqueue")
        self.assertEqual(removed["status"], "planned")

    def test_new_repo_bootstrap_does_not_commit_existing_source(self):
        path = self.project.root / "existing-project"
        path.mkdir()
        (path / "source.txt").write_text("Existing user work", encoding="utf-8")
        project = Project({**self.config, "path": str(path)})
        project.init()
        tracked = project.git("ls-files").splitlines()
        self.assertIn(".patchgoblin/tasks.json", tracked)
        self.assertNotIn("source.txt", tracked)

    def test_failed_validation_discussion_preserves_review_state_and_feedback(self):
        task = self.create()
        self.action(task, "mark_planned")
        self.action(task, "queue")
        with patch.object(Project, "provider", return_value="Implementation result"):
            self.project.run(task["id"])
        self.action(task, "discuss", message="Please fix the edge case")
        with patch.object(Project, "provider", side_effect=ValueError("Provider offline")):
            result = self.project.run(task["id"])
        self.assertEqual(result["status"], "validation")
        self.assertEqual(result["messages"][-1]["content"], "Please fix the edge case")
        self.assertEqual(result["error"], "Provider offline")

    def test_unrelated_edits_block_execution_without_losing_changes(self):
        task = self.create()
        self.project.checkpoint("Initial")
        (self.project.root / "personal.txt").write_text("keep me", encoding="utf-8")
        self.action(task, "mark_planned")
        self.action(task, "queue")
        with patch.object(Project, "provider") as provider:
            failed = self.project.run(task["id"])
        self.assertEqual(failed["status"], "failed")
        self.assertIn("Commit existing", failed["error"])
        provider.assert_not_called()
        self.assertEqual((self.project.root / "personal.txt").read_text(), "keep me")

    def test_failed_provider_persists_and_can_be_replanned(self):
        task = self.create()
        self.action(task, "plan")
        with patch.object(Project, "provider", side_effect=ValueError("Login required")):
            failed = self.project.run(task["id"])
        self.assertEqual(failed["error"], "Login required")
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(self.action(task, "plan")["status"], "planning_queued")

    def test_no_duplicate_run_or_recovery_while_locked(self):
        task = self.create()
        self.action(task, "plan")
        with lock(self.project.meta / "run.lock"):
            with self.assertRaisesRegex(ValueError, "busy"):
                self.project.run(task["id"])
            with self.assertRaisesRegex(ValueError, "busy"):
                dispatch({"project": self.config, "action": "recover", "payload": {"task_id": task["id"]}})

    def test_corrupt_json_is_preserved(self):
        self.project.file.write_text("broken", encoding="utf-8")
        with self.assertRaises(json.JSONDecodeError):
            self.project.init()
        self.assertEqual(self.project.file.read_text(), "broken")

    def test_api_path_boundaries_and_planning_read_only(self):
        for filename in ("../outside", ".git/config", ".env", "config/token.key", ".patchgoblin/tasks.json"):
            with self.assertRaises(ValueError):
                self.project.safe_path(filename)
        with self.assertRaises(ValueError):
            self.project.file_tool("write_file", {"path": "oops", "content": "bad"}, True)
        with self.assertRaises(ValueError):
            self.project.file_tool("run_command", {"argv": ["echo", "no"]}, False)
        self.project.file_tool("write_file", {"path": "nested/file.txt", "content": "ok"}, False)
        self.assertEqual(self.project.file_tool("read_file", {"path": "nested/file.txt"}, True), "ok")

    def test_openai_tool_loop_writes_actual_project_file(self):
        self.project.config.update(provider="openai", model="test-model")
        responses = [
            {"status": "completed", "output": [{"type": "function_call", "name": "write_file", "call_id": "call_1", "arguments": json.dumps({"path": "api.txt", "content": "API edit"})}]},
            {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "Done"}]}]},
        ]
        seen = []
        def respond(request, timeout):
            seen.append(json.loads(request.data))
            return io.BytesIO(json.dumps(responses.pop(0)).encode())
        with patch.dict(os.environ, {"OPENAI_API_KEY": "fake-test-key"}), patch("urllib.request.urlopen", side_effect=respond):
            self.assertEqual(self.project.api_agent("Do work", False), "Done")
        self.assertEqual((self.project.root / "api.txt").read_text(), "API edit")
        self.assertEqual(seen[1]["input"][-1]["type"], "function_call_output")
        self.assertFalse(seen[0]["store"])

    def test_local_transport_runs_in_directory_with_spaces(self):
        path = self.project.root / "project with spaces"
        config = {**self.config, "path": str(path)}
        Transport().call(config, "init")
        task = Transport().call(config, "create", {"title": "Quotes ' $() ; stay data"})
        self.assertEqual(Transport().call(config, "inspect")["tasks"][0]["title"], task["title"])

    def test_ssh_payload_not_interpolated_and_bootstrap_executable(self):
        config = {**self.config, "kind": "ssh", "host": "me@example.com", "path": "/tmp/name with ' $()"}
        with patch("patchgoblin.app.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, '{"ok":true,"data":{}}', '')
            Transport().call(config, "create", {"title": "'; arbitrary text"})
            args = run.call_args.args[0]
            self.assertNotIn(config["path"], " ".join(args))
            self.assertIn("StrictHostKeyChecking=yes", args)
            source = run.call_args.kwargs["input"]
            compile(source, "remote-agent", "exec")
            self.assertNotIn(config["path"], source)


class AppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = create_app(Path(self.temp.name) / "state")
        self.client = self.app.test_client()
        self.headers = {"X-PatchGoblin-Token": self.app.extensions["csrf"]}

    def post(self, path, data):
        return self.client.post(path, json=data, headers=self.headers)

    def test_http_flow_persistence_and_csrf(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.post("/api/projects", json={}).status_code, 403)
        config = {"name": "Web project", "path": str(Path(self.temp.name) / "project")}
        result = self.post("/api/projects", config)
        self.assertEqual(result.status_code, 201, result.json)
        pid = result.json["id"]
        created = self.post(f"/api/projects/{pid}/tasks", {"title": "A task"})
        self.assertEqual(created.status_code, 201)
        tid = created.json["id"]
        self.assertEqual(self.post(f"/api/projects/{pid}/tasks/{tid}/queue", {}).status_code, 400)
        self.assertEqual(self.post(f"/api/projects/{pid}/tasks/{tid}/mark_planned", {}).json["status"], "planned")
        self.assertEqual(self.post(f"/api/projects/{pid}/tasks/{tid}/queue", {}).json["status"], "queued")
        restarted = create_app(Path(self.temp.name) / "state").test_client()
        self.assertEqual(restarted.get(f"/api/projects/{pid}").json["tasks"][0]["status"], "queued")
        self.assertEqual(self.post("/api/queue", {"paused": True}).status_code, 200)
        self.assertTrue(self.client.get("/api/projects").json["paused"])

    def test_reject_bad_host_origin_and_config(self):
        self.assertEqual(self.client.get("/", headers={"Host": "evil.example"}).status_code, 400)
        response = self.client.post("/api/queue", json={"paused": True}, headers={**self.headers, "Origin": "https://evil.example"})
        self.assertEqual(response.status_code, 403)
        for host in ("-oProxyCommand=oops", "host;whoami", "host with spaces"):
            with self.assertRaises(ValueError):
                validate_config({"name": "Bad", "path": "/tmp/x", "kind": "ssh", "host": host})


class WorkerTests(unittest.TestCase):
    def test_fifo_pause_and_interrupted_project(self):
        with tempfile.TemporaryDirectory() as temp:
            registry = Registry(temp)
            registry.change(lambda d: d.update(projects=[{"id": "one"}, {"id": "two"}, {"id": "interrupted"}]))
            calls = []
            class FakeTransport:
                def call(self, project, action, payload=None):
                    if action == "inspect":
                        if project["id"] == "interrupted":
                            return {"tasks": [{"status": "running"}, {"id": "blocked", "status": "queued", "queued_at": "0"}]}
                        return {"tasks": [{"id": project["id"], "title": "Work", "status": "queued", "queued_at": "2" if project["id"] == "one" else "1"}]}
                    calls.append(payload["task_id"])
            worker = Worker(registry, FakeTransport())
            worker.step()
            self.assertEqual(calls, ["two"])
            registry.change(lambda d: d.update(paused=True))
            worker.step()
            self.assertEqual(calls, ["two"])


if __name__ == "__main__":
    unittest.main()
