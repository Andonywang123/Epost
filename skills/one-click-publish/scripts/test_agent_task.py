"""Independent Agent CLI tests: fake local adapter, no network/platform calls."""
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import agent_task
from coordinator import CoordinatorError
import test_trigger_server as fixtures


class AgentTaskTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.TriggerServerTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def cli(self, app, *arguments):
        completed = subprocess.run([
            sys.executable, str(Path(agent_task.__file__).resolve()),
            "--workspace", str(app.workspace), "--config", str(self.fixture.config),
            *arguments,
        ], capture_output=True, text=True, check=False)
        self.assertTrue(completed.stdout, completed.stderr)
        return completed.returncode, json.loads(completed.stdout)

    def test_inspect_requires_real_saved_confirmation_and_is_read_only(self):
        app, sid, review = self.fixture.prepare()
        status, result = self.cli(app, "inspect")
        self.assertEqual(status, 1)
        self.assertEqual(result["error"]["code"], "NOT_AUTHORIZED")
        self.fixture.confirm(app, sid, review)
        original = self.fixture.state_path(app, sid).read_bytes()
        status, result = self.cli(app, "inspect")
        self.assertEqual(status, 0)
        self.assertEqual(result["result"]["stage"], "AWAIT_AGENT")
        self.assertEqual(result["result"]["planHash"], review["planHash"])
        media = result["result"]["payload"]["source"]["media"]["files"][0]
        self.assertTrue(Path(media["path"]).is_file())
        self.assertEqual(self.fixture.state_path(app, sid).read_bytes(), original)

    def test_separate_agent_process_executes_once_without_http_server(self):
        config = json.loads(self.fixture.config.read_text())
        marker = self.fixture.root / "fake-adapter-calls.txt"
        fake_adapter = (
            "import json,sys; from pathlib import Path; r=json.load(sys.stdin); "
            "p=Path(sys.argv[1]); "
            "p.write_text((p.read_text() if p.exists() else '')+r['decision']+'\\n'); "
            "print(json.dumps({'status':'TEST_ONLY','outcome':'success'}))"
        )
        for entry in config["platforms"]:
            entry["command"] = [str(Path(sys.executable).resolve()), "-c", fake_adapter, str(marker)]
        self.fixture.config.write_text(json.dumps(config))
        for platform in ("xiaohongshu", "youtube"):
            app, sid, review = self.fixture.prepare(platform, name=platform)
            self.fixture.confirm(app, sid, review)
            before = marker.read_text() if marker.exists() else ""
            for _ in range(2):
                status, result = self.cli(app, "execute", "--plan-hash", review["planHash"])
                self.assertEqual(status, 0)
                self.assertEqual(result["result"]["stage"], "FINISHED")
            self.assertEqual(marker.read_text(), before + "draft\n")

    def test_old_hash_or_paused_task_never_calls_executor(self):
        app, sid, review = self.fixture.prepare()
        self.fixture.confirm(app, sid, review)
        with self.assertRaises(CoordinatorError), patch("subprocess.run", side_effect=AssertionError("must not execute")):
            agent_task.execute_task(app, sid, "0" * 64)
        failed = agent_task.execute_task(app, sid, review["planHash"],
                                         lambda request: {"status": "TEST_NEEDS_USER", "outcome": "needs_user"})
        self.assertEqual(failed["stage"], "PAUSED")
        with patch("subprocess.run", side_effect=AssertionError("must not retry")):
            again = agent_task.execute_task(app, sid, review["planHash"])
        self.assertEqual(again["stage"], "PAUSED")

    def test_concurrent_agent_request_does_not_interrupt_owned_worker(self):
        app, sid, review = self.fixture.prepare()
        self.fixture.confirm(app, sid, review)
        calls = []
        def fake_executor(request):
            calls.append(request["jobId"])
            original = self.fixture.state_path(app, sid).read_bytes()
            with patch("subprocess.run", side_effect=AssertionError("duplicate execution")):
                second = agent_task.execute_task(app, sid, review["planHash"])
            self.assertEqual(second["stage"], "EXECUTING")
            self.assertEqual(self.fixture.state_path(app, sid).read_bytes(), original)
            return {"status": "TEST_ONLY", "outcome": "success"}
        final = agent_task.execute_task(app, sid, review["planHash"], fake_executor)
        self.assertEqual(final["stage"], "FINISHED")
        self.assertEqual(len(calls), 1)

    def test_direct_agent_attachment_intake_only_stores(self):
        workspace = self.fixture.root / "direct-intake"
        workspace.mkdir()
        app = agent_task.context(workspace, self.fixture.config)
        snap = app.create_or_resume_session()
        title, body = self.fixture.root / "title.txt", self.fixture.root / "body.txt"
        title.write_text("原标题", encoding="utf-8")
        body.write_text("原正文", encoding="utf-8")
        status, result = self.cli(app, "import-media", "--kind", "video", "--asset-revision", str(snap["assetRevision"]), "--files", str(self.fixture.video))
        self.assertEqual(status, 0)
        media = result["result"]["media"]
        status, result = self.cli(app, "import-metadata", "--asset-revision", str(result["result"]["assetRevision"]), "--title-file", str(title), "--body-file", str(body), "--no-cover")
        self.assertEqual(status, 0)
        snapshot = result["result"]
        self.assertEqual(snapshot["stage"], "AWAIT_MATERIAL_CONFIRMATION")
        self.assertEqual(snapshot["media"], media)
        self.assertEqual(snapshot["metadata"]["title"], "原标题")
        self.assertEqual(snapshot["results"], [])
        self.assertFalse(snapshot["handoffConfirmed"])

    def test_chat_configuration_and_confirmation_cli_paths_are_removed(self):
        app, sid, review = self.fixture.prepare()
        original = self.fixture.state_path(app, sid).read_bytes()
        for action in ("confirm-materials", "prepare-plan", "confirm-plan"):
            completed = subprocess.run([sys.executable, str(Path(agent_task.__file__).resolve()),
                                        "--workspace", str(app.workspace), action],
                                       capture_output=True, text=True, check=False)
            self.assertEqual(completed.returncode, 2)
            self.assertIn("invalid choice", completed.stderr)
            self.assertEqual(self.fixture.state_path(app, sid).read_bytes(), original)

    def test_non_dashboard_approval_cannot_be_executed(self):
        app, sid, review = self.fixture.prepare()
        app.coordinator.confirm_plan(sid, review["planId"], review["planHash"], True,
                                     trusted_user_event_id="NOT_A_DASHBOARD_EVENT")
        self.assertEqual(app.coordinator.get_agent_task(sid)["confirmationSource"], "unknown")
        with self.assertRaises(CoordinatorError) as error, patch("subprocess.run", side_effect=AssertionError("must not execute")):
            agent_task.execute_task(app, sid, review["planHash"])
        self.assertEqual(error.exception.code, "DASHBOARD_CONFIRMATION_REQUIRED")
        with self.assertRaises(CoordinatorError):
            app.coordinator.run_next(sid, lambda request: self.fail("must not execute"))


if __name__ == "__main__":
    unittest.main()
