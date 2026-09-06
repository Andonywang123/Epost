"""Trigger-only HTTP regressions. No sockets, credentials, or real dispatch."""

from email.message import Message
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from coordinator import Coordinator
from run_dashboard import Handler, LocalHost, load_configuration


class TriggerServerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.image = self.root / "image.jpg"
        self.video = self.root / "video.mp4"
        self.image.write_bytes(b"isolated-image-fixture")
        self.video.write_bytes(b"isolated-video-fixture")
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({
            "localTimezone": "Asia/Shanghai",
            "platforms": [{
                "platform": platform, "available": True,
                "accounts": [{"id": "test", "label": "ISOLATED TEST ONLY", "settings": {}}],
                "requiredPostSettings": [], "requireAccountConfirmation": False,
                "command": [str(Path(sys.executable).resolve()), "-c", "raise SystemExit('NEVER RUN')"],
            } for platform in ("xiaohongshu", "youtube")],
        }), encoding="utf-8")

    def request(self, app, method, path, data=None, *, csrf=True):
        """Exercise actual route/origin/body logic without opening a listener."""
        handler = object.__new__(Handler)
        handler.server = SimpleNamespace(app=app, server_port=18766)
        handler.path = path
        handler.headers = Message()
        handler.headers["Host"] = "127.0.0.1:18766"
        handler.headers["Origin"] = "http://127.0.0.1:18766"
        handler.headers["Sec-Fetch-Site"] = "same-origin"
        if csrf:
            handler.headers["X-OCP-CSRF"] = app.csrf
        payload = json.dumps(data or {}).encode("utf-8")
        handler.headers["Content-Type"] = "application/json"
        handler.headers["Content-Length"] = str(len(payload))
        handler.rfile = io.BytesIO(payload)
        responses = []
        handler._json = lambda value, status=200: responses.append((status, value))
        getattr(handler, "do_" + method)()
        self.assertEqual(len(responses), 1)
        return responses[0]

    def prepare(self, platform="youtube", name="workspace"):
        # The web host must not create a background executor even at startup.
        with patch("threading.Thread", side_effect=AssertionError("HTTP spawned a worker")):
            app = LocalHost(self.root / name, self.config)
        snapshot = app.create_or_resume_session()
        sid = snapshot["sessionId"]
        kind = "image_post" if platform == "xiaohongshu" else "video"
        app.coordinator.select_kind(sid, kind, snapshot["assetRevision"])
        app.coordinator.store_media(sid, kind, [self.image if kind == "image_post" else self.video])
        snapshot = app.coordinator.store_metadata(sid, None, True, "测试标题", "测试正文")
        snapshot = app.coordinator.confirm_materials(sid, snapshot["assetRevision"])
        review = app.coordinator.prepare_plan(sid, snapshot["assetRevision"], [{
            "platform": platform, "selected": True, "action": "draft", "timing": None,
            "accountProfile": "test", "postSettings": {},
            "acceptLocalDraft": platform == "youtube",
        }])
        return app, sid, review

    def confirm(self, app, sid, review, confirmed=True, csrf=True):
        return self.request(app, "POST", "/api/plan/confirm", {
            "sessionId": sid, "planId": review["planId"], "planHash": review["planHash"],
            "confirmed": confirmed,
        }, csrf=csrf)

    def state_path(self, app, sid):
        return app.workspace / "sessions" / sid / "state.json"

    def read_refreshes(self, app, sid):
        responses = [app.snapshot(sid), app.create_or_resume_session()]
        for path in ("/api/bootstrap", "/api/session?id=" + sid):
            status, result = self.request(app, "GET", path)
            self.assertEqual(status, 200)
            self.assertEqual(result["executionMode"], "agent")
            if "stage" in result:
                responses.append(result)
        status, resumed = self.request(app, "POST", "/api/session/create", {})
        self.assertEqual(status, 200)
        responses.append(resumed)
        return responses

    def test_http_confirmation_only_persists_handoff_for_both_routes(self):
        with patch("subprocess.run", side_effect=AssertionError("HTTP executed a process")), \
                patch.object(Coordinator, "run_next", side_effect=AssertionError("HTTP dispatched")):
            adapters, commands, *_ = load_configuration(self.config)
            self.assertEqual(set(commands), {"xiaohongshu", "youtube"})
            self.assertTrue(adapters["xiaohongshu"]["available"])
            self.assertTrue(adapters["youtube"]["available"])
            for platform in ("xiaohongshu", "youtube"):
                with self.subTest(platform=platform):
                    app, sid, review = self.prepare(platform, name=platform)
                    self.assertNotIn("agentHandoff", app.snapshot(sid))
                    status, result = self.confirm(app, sid, review)
                    self.assertEqual(status, 200)
                    self.assertEqual(result["stage"], "AWAIT_AGENT")
                    self.assertEqual(result["executionMode"], "agent")
                    self.assertEqual(result["agentReceiver"]["status"], "offline")
                    self.assertEqual(result["agentHandoff"], {
                        "status": "waiting", "workspace": str(app.workspace),
                        "sessionId": sid, "planHash": review["planHash"],
                        "message": "本次许可已保存，接收器未就绪；请回到 Agent 检查并启动本批接收器。",
                    })
                    state = json.loads(self.state_path(app, sid).read_text())
                    self.assertEqual(state["approval"]["confirmationSource"], "dashboard")
                    self.assertEqual(len(state["jobs"]), 1)
                    self.assertEqual(state["jobs"][0]["platform"], platform)
                    self.assertEqual(state["jobs"][0]["state"], "QUEUED")
                    self.assertEqual(result["results"], [])

    def test_refresh_restart_and_duplicate_confirmation_do_not_change_waiting_task(self):
        app, sid, review = self.prepare()
        self.assertEqual(self.confirm(app, sid, review)[0], 200)
        original = self.state_path(app, sid).read_bytes()
        with patch("subprocess.run", side_effect=AssertionError("HTTP executed a process")), \
                patch.object(Coordinator, "run_next", side_effect=AssertionError("HTTP dispatched")), \
                patch("threading.Thread", side_effect=AssertionError("HTTP spawned a worker")):
            for current in (app, LocalHost(app.workspace, self.config)):
                for result in self.read_refreshes(current, sid):
                    self.assertEqual(result["stage"], "AWAIT_AGENT")
                    self.assertEqual(result["agentHandoff"]["status"], "waiting")
                self.assertEqual(self.confirm(current, sid, review)[0], 200)
                self.assertEqual(self.state_path(app, sid).read_bytes(), original)

    def test_refresh_restart_during_agent_execution_never_pauses_or_replays(self):
        app, sid, review = self.prepare()
        self.assertEqual(self.confirm(app, sid, review)[0], 200)
        calls = []

        def independent_agent_stub(request):
            calls.append(request)
            original = self.state_path(app, sid).read_bytes()
            with patch.object(Coordinator, "run_next", side_effect=AssertionError("HTTP dispatched")), \
                    patch("threading.Thread", side_effect=AssertionError("HTTP spawned a worker")):
                for current in (app, LocalHost(app.workspace, self.config)):
                    for result in self.read_refreshes(current, sid):
                        self.assertEqual(result["stage"], "EXECUTING")
                        self.assertEqual(result["agentHandoff"]["status"], "running")
                    self.assertEqual(self.state_path(app, sid).read_bytes(), original)
            return {"status": "TEST_ONLY", "outcome": "success"}

        with patch("subprocess.run", side_effect=AssertionError("No real platforms in this test")):
            final = app.coordinator.run_next(sid, independent_agent_stub, expected_plan_hash=review["planHash"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(final["stage"], "FINISHED")
        self.assertEqual(app.snapshot(sid)["agentHandoff"]["status"], "complete")

    def test_unconfirmed_and_cancelled_plans_never_expose_handoff(self):
        app, sid, review = self.prepare()
        self.assertNotIn("agentHandoff", app.snapshot(sid))
        status, result = self.confirm(app, sid, review, confirmed=False)
        self.assertEqual(status, 200)
        self.assertEqual(result["stage"], "CONFIGURING")
        self.assertNotIn("agentHandoff", result)
        state = json.loads(self.state_path(app, sid).read_text())
        self.assertIsNone(state["approval"])
        self.assertEqual(state["jobs"], [])

    def test_missing_csrf_cannot_authorize_handoff(self):
        app, sid, review = self.prepare()
        original = self.state_path(app, sid).read_bytes()
        status, result = self.confirm(app, sid, review, csrf=False)
        self.assertEqual(status, 403)
        self.assertEqual(result["error"]["code"], "CSRF_REJECTED")
        self.assertEqual(self.state_path(app, sid).read_bytes(), original)

    def test_handoff_message_matches_receiver_and_durable_task_status(self):
        app, sid, review = self.prepare()
        self.assertEqual(self.confirm(app, sid, review)[0], 200)
        original = self.state_path(app, sid).read_bytes()
        snapshot = app.coordinator.get_snapshot(sid)
        cases = [
            ("AWAIT_AGENT", "ready", "waiting", "本次许可已保存，等待 Agent 接收器接手。"),
            ("AWAIT_AGENT", "offline", "waiting", "本次许可已保存，接收器未就绪；请回到 Agent 检查并启动本批接收器。"),
            ("EXECUTING", "busy", "running", "Agent 正在执行已确认任务；刷新看板不会中断。"),
            ("PAUSED", "offline", "needs_user", "任务已暂停或需要人工核实。请回到 Agent 查看原因，勿重复发送。"),
            ("FINISHED", "offline", "complete", "本次已确认任务已完成；无需再次启动。"),
        ]
        for stage, receiver, status, message in cases:
            with self.subTest(stage=stage, receiver=receiver), patch("run_dashboard.receiver_snapshot", return_value={"status": receiver}):
                result = app.decorate_snapshot({**snapshot, "stage": stage})
                self.assertEqual(result["agentHandoff"]["status"], status)
                self.assertEqual(result["agentHandoff"]["message"], message)
                if stage in {"PAUSED", "FINISHED"}:
                    self.assertNotIn("启动本批接收器", result["agentHandoff"]["message"])
        self.assertEqual(self.state_path(app, sid).read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
