"""Isolated browser QA server. Fake AI replies; real storage, Git and HTTP."""
import tempfile
from pathlib import Path

from waitress import serve
from patchgoblin.app import create_app, validate_config
from patchgoblin.host import Project, dispatch


class FixtureTransport:
    def call(self, project, action, payload=None):
        return dispatch({"project": project, "action": action, "payload": payload or {}})


def fake_provider(self, prompt, planning):
    return "Review the reported issue.\n\nUpdated plan:\n1. Fix the greeting.\n2. Verify its content."


if __name__ == "__main__":
    Project.provider = fake_provider
    with tempfile.TemporaryDirectory(prefix="patchgoblin-browser-") as temp:
        app = create_app(Path(temp) / "registry", transport=FixtureTransport())
        config = validate_config({"name": "Browser test project", "path": str(Path(temp) / "project")})
        project = Project(config)
        project.init()
        task = project.mutate("create", {"title": "Review the generated greeting", "description": "Browser test fixture: verify the result, discuss a fix, and approve it."})
        project.checkpoint("Fixture baseline")
        project.mutate("mark_planned", {"task_id": task["id"]})
        project.mutate("queue", {"task_id": task["id"]})
        project.run(task["id"])
        app.extensions["registry"].change(lambda data: data["projects"].append(config))
        app.extensions["worker"].start()
        print("Browser QA fixture ready at http://127.0.0.1:5051", flush=True)
        serve(app, host="127.0.0.1", port=5051, threads=8)
