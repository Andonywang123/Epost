"""Isolated receiver tests; only temporary fixtures and fake platform callbacks."""
import json
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

import agent_receiver
import agent_task
from coordinator import CoordinatorError
from run_dashboard import LocalHost, RequestError
import test_trigger_server as fixtures


class FakeClock:
    def __init__(self, on_sleep=None):
        self.value = 0.0
        self.on_sleep = on_sleep
        self.sleeps = 0

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.sleeps += 1
        if self.on_sleep:
            self.on_sleep(self.sleeps)
        self.value += seconds


class ReceiverTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.TriggerServerTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def receive(self, app, sid, clock, executor=None, timeout=5, revision=None):
        revision = app.snapshot(sid)["assetRevision"] if revision is None else revision
        return agent_task.receive_task(app, sid, revision, timeout,
                                       executor or (lambda request: self.fail("Unexpected platform execution")),
                                       clock=clock.now, sleeper=clock.sleep)

    def test_no_confirmation_never_executes_and_timeout_deactivates(self):
        app, sid, review = self.fixture.prepare()
        original = self.fixture.state_path(app, sid).read_bytes()
        def waiting(_):
            self.assertEqual(app.snapshot(sid)["agentReceiver"]["status"], "ready")
            self.assertEqual(self.fixture.state_path(app, sid).read_bytes(), original)
        clock = FakeClock(waiting)
        with self.assertRaises(CoordinatorError) as error:
            self.receive(app, sid, clock, timeout=3)
        self.assertEqual(error.exception.code, "RECEIVER_TIMEOUT")
        self.assertEqual(clock.sleeps, 3)
        self.assertEqual(app.snapshot(sid)["agentReceiver"]["status"], "offline")
        self.assertEqual(self.fixture.state_path(app, sid).read_bytes(), original)

    def test_receiver_starts_after_chat_intake_before_dashboard_material_confirmation(self):
        app = LocalHost(self.fixture.root / "intake-only", self.fixture.config)
        snapshot = app.create_or_resume_session()
        sid = snapshot["sessionId"]
        app.coordinator.select_kind(sid, "video", snapshot["assetRevision"])
        app.coordinator.store_media(sid, "video", [self.fixture.video])
        snapshot = app.coordinator.store_metadata(sid, None, True, "原样标题", "原样正文")
        self.assertEqual(snapshot["stage"], "AWAIT_MATERIAL_CONFIRMATION")
        calls = []
        def interact(_):
            self.assertEqual(calls, [])
            self.assertEqual(app.snapshot(sid)["agentReceiver"]["status"], "ready")
            status, current = self.fixture.request(app, "POST", "/api/materials/confirm", {
                "sessionId": sid, "assetRevision": snapshot["assetRevision"], "confirmed": True,
            })
            self.assertEqual(status, 200)
            status, review = self.fixture.request(app, "POST", "/api/plan/prepare", {
                "sessionId": sid, "assetRevision": current["assetRevision"],
                "rows": [{"platform": "youtube", "selected": True, "action": "draft",
                          "timing": None, "accountProfile": "test", "postSettings": {}, "acceptLocalDraft": True}],
            })
            self.assertEqual(status, 200)
            self.assertEqual(self.fixture.confirm(app, sid, review)[0], 200)
        def execute(request):
            calls.append(request)
            return {"status": "ISOLATED_TEST_ONLY", "outcome": "success"}
        self.assertEqual(self.receive(app, sid, FakeClock(interact), execute)["stage"], "FINISHED")
        self.assertEqual(len(calls), 1)


    def test_six_platform_action_time_routes_preserve_choices_and_clear_non_schedule_handoff_time(self):
        config = json.loads(self.fixture.config.read_text())
        for entry in config["platforms"]:
            if entry["platform"] == "youtube":
                entry["accounts"][0]["settings"] = {"privacy": "public"}
        self.fixture.config.write_text(json.dumps(config))
        for platform in ("xiaohongshu", "youtube"):
            for decision in ("draft", "publish", "schedule"):
                with self.subTest(platform=platform, decision=decision):
                    app, sid, _ = self.fixture.prepare(platform, name=platform + decision)
                    timing = None if decision == "draft" else {
                        "kind": "now" if decision == "publish" else "scheduled",
                        "region": "china" if platform == "xiaohongshu" else "us",
                        "timezone": "Asia/Shanghai" if platform == "xiaohongshu" else "America/New_York",
                    }
                    if decision == "schedule":
                        timing.update(date="2099-06-01", minute=14 * 60 + 37)
                    status, review = self.fixture.request(app, "POST", "/api/plan/prepare", {
                        "sessionId": sid, "assetRevision": app.snapshot(sid)["assetRevision"],
                        "rows": [{"platform": platform, "selected": True,
                                  "action": "draft" if decision == "draft" else "publish",
                                  "timing": timing, "accountProfile": "test", "postSettings": {},
                                  "acceptLocalDraft": platform == "youtube"}],
                    })
                    self.assertEqual(status, 200)
                    calls = []
                    frozen = []
                    def approve(_):
                        self.assertEqual(calls, [])
                        self.assertEqual(app.snapshot(sid)["agentReceiver"]["status"], "ready")
                        self.assertEqual(self.fixture.confirm(app, sid, review)[0], 200)
                        frozen.append(app.coordinator.get_agent_task(sid))
                    def execute(request):
                        calls.append(request)
                        self.assertEqual(app.snapshot(sid)["agentReceiver"]["status"], "busy")
                        return {"status": "ISOLATED_TEST_ONLY", "outcome": "success"}
                    final = self.receive(app, sid, FakeClock(approve), execute)
                    self.assertEqual(final["stage"], "FINISHED")
                    self.assertEqual(final["agentReceiver"]["status"], "offline")
                    self.assertEqual(len(calls), 1)
                    request, payload = calls[0], frozen[0]["payload"]
                    target = payload["targets"][0]
                    self.assertEqual(request["platform"], platform)
                    self.assertEqual(request["decision"], decision)
                    self.assertEqual(request["source"], payload["source"])
                    self.assertEqual(request["planHash"], frozen[0]["planHash"])
                    for field in ("accountProfile", "accountSettings", "draftScope", "acceptLocalDraft", "policies"):
                        self.assertEqual(request[field], target[field])
                    schedule = target["schedule"]
                    if decision == "schedule":
                        self.assertEqual(request["scheduledAt"], schedule["scheduledAt"])
                        self.assertEqual(request["timezone"], schedule["timezone"])
                        self.assertEqual(request["scheduleUtc"], schedule["utc"])
                    else:
                        self.assertIsNone(request["scheduledAt"])
                        self.assertIsNone(request["timezone"])
                        self.assertIsNone(request["scheduleUtc"])
                        if decision == "publish":
                            self.assertEqual(schedule["kind"], "now")
                            self.assertIsNotNone(schedule["timezone"])
                    self.assertEqual(request["authorization"]["confirmationSource"], "dashboard")

    def test_no_returns_to_configuration_and_receiver_keeps_waiting(self):
        app, sid, review = self.fixture.prepare()
        calls = []
        def interact(count):
            self.assertEqual(calls, [])
            if count == 1:
                self.assertEqual(self.fixture.confirm(app, sid, review, False)[0], 200)
                self.assertEqual(app.snapshot(sid)["stage"], "CONFIGURING")
            else:
                snapshot = app.snapshot(sid)
                next_review = app.coordinator.prepare_plan(sid, snapshot["assetRevision"], snapshot["settings"])
                self.assertEqual(self.fixture.confirm(app, sid, next_review)[0], 200)
        def execute(request):
            calls.append(request)
            return {"status": "ISOLATED_TEST_ONLY", "outcome": "success"}
        final = self.receive(app, sid, FakeClock(interact), execute)
        self.assertEqual(final["stage"], "FINISHED")
        self.assertEqual(len(calls), 1)

    def test_changed_materials_stop_old_receiver(self):
        app, sid, review = self.fixture.prepare()
        def change(_):
            app.coordinator.store_metadata(sid, None, True, "changed title", "changed body")
            self.assertEqual(app.snapshot(sid)["agentReceiver"]["status"], "offline")
        with self.assertRaises(CoordinatorError) as error:
            self.receive(app, sid, FakeClock(change))
        self.assertEqual(error.exception.code, "RECEIVER_MATERIALS_CHANGED")
        self.assertEqual(app.snapshot(sid)["agentReceiver"]["status"], "offline")
        self.assertEqual(app.snapshot(sid)["metadata"]["title"], "changed title")

    def test_duplicate_receiver_and_refresh_restart_do_not_dispatch_twice(self):
        app, sid, review = self.fixture.prepare()
        calls = []
        def waiting(_):
            original = self.fixture.state_path(app, sid).read_bytes()
            with self.assertRaises(CoordinatorError) as error:
                self.receive(app, sid, FakeClock())
            self.assertEqual(error.exception.code, "RECEIVER_ALREADY_ACTIVE")
            for current in (app, LocalHost(app.workspace, self.fixture.config)):
                for snapshot in self.fixture.read_refreshes(current, sid):
                    self.assertEqual(snapshot["agentReceiver"]["status"], "ready")
                self.assertEqual(self.fixture.state_path(app, sid).read_bytes(), original)
            self.assertEqual(self.fixture.confirm(app, sid, review)[0], 200)
        def execute(request):
            calls.append(request)
            original = self.fixture.state_path(app, sid).read_bytes()
            with self.assertRaises(CoordinatorError) as error:
                self.receive(app, sid, FakeClock())
            self.assertEqual(error.exception.code, "RECEIVER_TASK_ALREADY_STARTED")
            for snapshot in self.fixture.read_refreshes(LocalHost(app.workspace, self.fixture.config), sid):
                self.assertEqual(snapshot["agentReceiver"]["status"], "busy")
            self.assertEqual(self.fixture.state_path(app, sid).read_bytes(), original)
            return {"status": "ISOLATED_TEST_ONLY", "outcome": "success"}
        self.assertEqual(self.receive(app, sid, FakeClock(waiting), execute)["stage"], "FINISHED")
        self.assertEqual(len(calls), 1)
        with self.assertRaises(CoordinatorError):
            self.receive(app, sid, FakeClock())
        self.assertEqual(len(calls), 1)

    def test_receiver_requires_session_version_recent_heartbeat_and_owned_lock(self):
        app, sid, _ = self.fixture.prepare()
        revision = app.snapshot(sid)["assetRevision"]
        with agent_receiver.ReceiverLease(app.coordinator, sid, revision):
            self.assertEqual(app.snapshot(sid)["agentReceiver"]["status"], "ready")
            with patch.object(agent_receiver, "process_alive", return_value=False):
                self.assertEqual(app.snapshot(sid)["agentReceiver"]["status"], "offline")
            future = time.time() + agent_receiver.HEARTBEAT_TTL + 2
            with patch.object(agent_receiver.time, "time", return_value=future):
                self.assertEqual(app.snapshot(sid)["agentReceiver"]["status"], "offline")
            path = app.workspace / "sessions" / sid / "receiver-state.json"
            still_active = path.read_text()
        # Simulate a leftover file written by a process that no longer owns its lock.
        path.write_text(still_active)
        self.assertEqual(app.snapshot(sid)["agentReceiver"]["status"], "offline")

    def test_process_crash_leaves_no_live_receiver_despite_active_record(self):
        app, sid, _ = self.fixture.prepare()
        revision = app.snapshot(sid)["assetRevision"]
        code = (
            "import os,sys\nfrom pathlib import Path\n"
            "from run_dashboard import LocalHost\nfrom agent_receiver import ReceiverLease\n"
            "app=LocalHost(Path(sys.argv[1]),Path(sys.argv[2]))\n"
            "with ReceiverLease(app.coordinator,sys.argv[3],int(sys.argv[4])):\n"
            " print('READY',flush=True)\n sys.stdin.readline()\n os._exit(0)\n"
        )
        process = subprocess.Popen([sys.executable, "-u", "-c", code,
                                    str(app.workspace), str(self.fixture.config), sid, str(revision)],
                                   cwd=str(Path(agent_task.__file__).resolve().parent),
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(), "READY")
            self.assertEqual(app.snapshot(sid)["agentReceiver"]["status"], "ready")
            process.communicate("\n", timeout=5)
            record = json.loads((app.workspace / "sessions" / sid / "receiver-state.json").read_text())
            self.assertTrue(record["active"])
            self.assertEqual(app.snapshot(sid)["agentReceiver"]["status"], "offline")
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)

    def test_permission_denied_pid_probe_is_unverifiable_not_stopped_or_ready(self):
        app, sid, _ = self.fixture.prepare()
        revision = app.snapshot(sid)["assetRevision"]
        with agent_receiver.ReceiverLease(app.coordinator, sid, revision):
            with patch.object(agent_receiver.os, "kill", side_effect=PermissionError("sandbox denied signal probe")):
                receiver = app.snapshot(sid)["agentReceiver"]
                self.assertEqual(receiver["status"], "offline")
                self.assertEqual(receiver["reason"], "process_unverifiable")
            path = app.workspace / "sessions" / sid / "receiver-state.json"
            stale_active = path.read_text()
        path.write_text(stale_active)
        with patch.object(agent_receiver.os, "kill", side_effect=PermissionError("sandbox denied signal probe")):
            receiver = app.snapshot(sid)["agentReceiver"]
            self.assertEqual(receiver["status"], "offline")
            self.assertEqual(receiver["reason"], "process_unverifiable")

    def test_wrong_session_version_and_timeout_parameters_never_execute(self):
        app, sid, _ = self.fixture.prepare()
        revision = app.snapshot(sid)["assetRevision"]
        for arguments in (("", revision, 5), (sid, 0, 5), (sid, revision, 0),
                          (sid, revision, 3601), (sid, revision + 1, 5)):
            with self.subTest(arguments=arguments), self.assertRaises(CoordinatorError):
                agent_task.receive_task(app, *arguments, executor=lambda request: self.fail("must not execute"))
        other = app.coordinator.create_session()["sessionId"]
        with self.assertRaises(RequestError):
            agent_task.receive_task(app, other, revision, 5, lambda request: self.fail("must not execute"))

    def test_active_session_change_stops_waiting_receiver(self):
        app, sid, _ = self.fixture.prepare()
        other = app.coordinator.create_session()["sessionId"]
        def change_active(_):
            app.host_state_path.write_text(json.dumps({"activeSessionId": other}))
        with self.assertRaises(RequestError) as error:
            self.receive(app, sid, FakeClock(change_active))
        self.assertEqual(error.exception.code, "STALE_SESSION")
        self.assertEqual(app.snapshot(sid)["agentReceiver"]["status"], "offline")

    def test_receive_cli_requires_explicit_session_and_wait_bound(self):
        app, sid, _ = self.fixture.prepare()
        for arguments in (("receive", "--asset-revision", "3", "--timeout", "1"),
                          ("--session", sid, "receive", "--asset-revision", "3")):
            completed = subprocess.run([sys.executable, str(Path(agent_task.__file__).resolve()),
                                        "--workspace", str(app.workspace), *arguments],
                                       capture_output=True, text=True, check=False)
            self.assertEqual(completed.returncode, 2)
        self.assertFalse((app.workspace / "sessions" / sid / "receiver-state.json").exists())

    def test_non_dashboard_approval_is_not_consumed(self):
        app, sid, review = self.fixture.prepare()
        app.coordinator.confirm_plan(sid, review["planId"], review["planHash"], True,
                                     trusted_user_event_id="not-a-dashboard-event")
        original = self.fixture.state_path(app, sid).read_bytes()
        with self.assertRaises(CoordinatorError) as error:
            self.receive(app, sid, FakeClock())
        self.assertEqual(error.exception.code, "DASHBOARD_CONFIRMATION_REQUIRED")
        self.assertEqual(self.fixture.state_path(app, sid).read_bytes(), original)
        self.assertEqual(app.snapshot(sid)["agentReceiver"]["status"], "offline")

    def test_legacy_finished_and_paused_are_read_only_not_replayed(self):
        for stage in ("PAUSED", "FINISHED"):
            app, sid, review = self.fixture.prepare(name=stage)
            app.coordinator.confirm_plan(sid, review["planId"], review["planHash"], True,
                                         trusted_user_event_id="legacy-event")
            with app.coordinator._locked(sid):
                state = app.coordinator._load(sid)
                state["stage"] = stage
                state["approval"].pop("confirmationSource")
                app.coordinator._save(sid, state)
            original = self.fixture.state_path(app, sid).read_bytes()
            result = agent_task.execute_task(app, sid, review["planHash"], lambda request: self.fail("must not replay"))
            self.assertEqual(result["stage"], stage)
            with self.assertRaises(CoordinatorError):
                self.receive(app, sid, FakeClock())
            self.assertEqual(self.fixture.state_path(app, sid).read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
