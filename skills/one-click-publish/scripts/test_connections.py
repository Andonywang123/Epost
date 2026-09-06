"""Isolated coordinator/adapter contract tests: never access real platforms."""
import argparse
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from coordinator import Coordinator, CoordinatorError
import adapter_runner
import run_dashboard


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.image = self.root / "image.jpg"
        self.image.write_bytes(b"test-only-image-fixture")
        self.video = self.root / "video.mp4"
        self.video.write_bytes(b"test-only-video-fixture")
        self.caps = {
            "xiaohongshu": dict(available=True, mediaKinds=["image_post"], modes=["draft", "publish", "schedule"], draftScope="platform",
                                accounts=[dict(id="test", label="TEST ONLY", settings={"cdp_url": "http://127.0.0.1:9222"})],
                                requireAccountConfirmation=True, requiredPostSettings=["visibility"]),
            "youtube": dict(available=True, mediaKinds=["video"], modes=["draft", "publish", "schedule"], draftScope="local",
                            accounts=[dict(id="test", label="TEST ONLY", settings={})], requireAccountConfirmation=True,
                            requiredPostSettings=["privacy", "made_for_kids", "contains_synthetic_media", "notify_subscribers"])
        }
        self.coordinator = Coordinator(self.root / "sessions", self.caps, local_timezone="Asia/Shanghai")

    def intake(self, platform):
        snapshot = self.coordinator.create_session()
        sid = snapshot["sessionId"]
        kind = "image_post" if platform == "xiaohongshu" else "video"
        self.coordinator.select_kind(sid, kind, snapshot["assetRevision"])
        self.coordinator.store_media(sid, kind, [self.image if kind == "image_post" else self.video])
        snapshot = self.coordinator.store_metadata(sid, None, True, "测试标题", "测试正文")
        self.assertEqual(snapshot["stage"], "AWAIT_MATERIAL_CONFIRMATION")
        self.assertEqual(snapshot["results"], [])
        return self.coordinator.confirm_materials(sid, snapshot["assetRevision"])

    def row(self, platform):
        return dict(platform=platform, selected=True, action="draft", timing=None, accountProfile="test", accountConfirmed=True,
                    acceptLocalDraft=platform == "youtube", postSettings={"visibility": "公开"} if platform == "xiaohongshu" else
                    dict(privacy="private", made_for_kids=False, contains_synthetic_media=False, notify_subscribers=False))

    def test_storage_and_cancel_never_dispatch(self):
        snapshot = self.intake("xiaohongshu")
        review = self.coordinator.prepare_plan(snapshot["sessionId"], snapshot["assetRevision"], [self.row("xiaohongshu")])
        result = self.coordinator.confirm_plan(snapshot["sessionId"], review["planId"], review["planHash"], False, trusted_user_event_id="isolated-test-cancel")
        self.assertEqual(result["stage"], "CONFIGURING")
        self.assertEqual(result["results"], [])

    def test_type_is_explicit_and_upload_cannot_silently_change_it(self):
        snapshot = self.coordinator.create_session()
        self.assertEqual(snapshot["stage"], "AWAIT_KIND")
        self.assertIsNone(snapshot["contentKind"])
        with self.assertRaises(CoordinatorError):
            self.coordinator.store_media(snapshot["sessionId"], "video", [self.video])
        self.coordinator.select_kind(snapshot["sessionId"], "video", 0)
        with self.assertRaises(CoordinatorError):
            self.coordinator.store_media(snapshot["sessionId"], "image_post", [self.image])

    def test_editing_media_preserves_cover_and_text_and_invalidates_consent(self):
        snapshot = self.intake("youtube")
        sid = snapshot["sessionId"]
        snapshot = self.coordinator.store_metadata(sid, self.image, False, "原标题", "原正文")
        self.coordinator.confirm_materials(sid, snapshot["assetRevision"])
        review = self.coordinator.prepare_plan(sid, snapshot["assetRevision"], [self.row("youtube")])
        changed = self.coordinator.store_media(sid, "video", [self.video])
        self.assertEqual(changed["metadata"], snapshot["metadata"])
        self.assertFalse(changed["sourceConfirmed"])
        self.assertEqual(changed["stage"], "AWAIT_MATERIAL_CONFIRMATION")
        self.assertIsNone(changed["pendingReview"])
        with self.assertRaises(CoordinatorError):
            self.coordinator.confirm_plan(sid, review["planId"], review["planHash"], True, trusted_user_event_id="test-stale")

    def test_editing_text_retains_main_files_and_saved_cover(self):
        snapshot = self.intake("youtube")
        sid = snapshot["sessionId"]
        snapshot = self.coordinator.store_metadata(sid, self.image, False, "标题", "正文")
        changed = self.coordinator.store_metadata(sid, None, False, "修改标题", "修改正文", keep_cover_revision=snapshot["assetRevision"])
        self.assertEqual(changed["media"], snapshot["media"])
        self.assertEqual(changed["metadata"]["cover"], snapshot["metadata"]["cover"])
        self.assertEqual(changed["metadata"]["title"], "修改标题")
        with self.assertRaises(CoordinatorError):
            self.coordinator.store_metadata(sid, None, False, "不应覆盖", "正文", keep_cover_revision=snapshot["assetRevision"])

    def test_switching_routes_keeps_both_media_packages_and_metadata(self):
        snapshot = self.intake("youtube")
        sid = snapshot["sessionId"]
        video_media = snapshot["media"]
        snapshot = self.coordinator.select_kind(sid, "image_post", snapshot["assetRevision"])
        self.assertIsNone(snapshot["media"])
        self.assertEqual(snapshot["metadata"]["title"], "测试标题")
        self.assertFalse(snapshot["sourceConfirmed"])
        snapshot = self.coordinator.store_media(sid, "image_post", [self.image])
        image_media = snapshot["media"]
        snapshot = self.coordinator.select_kind(sid, "video", snapshot["assetRevision"])
        self.assertEqual(snapshot["media"], video_media)
        snapshot = self.coordinator.select_kind(sid, "image_post", snapshot["assetRevision"])
        self.assertEqual(snapshot["media"], image_media)
        self.assertEqual(set(snapshot["savedMediaKinds"]), {"video", "image_post"})

    def test_started_batches_cannot_be_edited_or_switch_type(self):
        snapshot = self.intake("youtube")
        sid = snapshot["sessionId"]
        review = self.coordinator.prepare_plan(sid, snapshot["assetRevision"], [self.row("youtube")])
        self.coordinator.confirm_plan(sid, review["planId"], review["planHash"], True, trusted_user_event_id="isolated-test")
        with self.assertRaises(CoordinatorError):
            self.coordinator.store_media(sid, "video", [self.video])
        with self.assertRaises(CoordinatorError):
            self.coordinator.select_kind(sid, "image_post", snapshot["assetRevision"])

    def test_required_declarations_and_config_injection_rejected(self):
        snapshot = self.intake("youtube")
        for key, value in [("made_for_kids", None), ("notify_subscribers", 0), ("cdp_url", "http://example.invalid")]:
            row = self.row("youtube")
            row["postSettings"][key] = value
            with self.subTest(key=key), self.assertRaises(CoordinatorError):
                self.coordinator.prepare_plan(snapshot["sessionId"], snapshot["assetRevision"], [row])
        row = self.row("youtube")
        row["accountConfirmed"] = False
        with self.assertRaises(CoordinatorError):
            self.coordinator.prepare_plan(snapshot["sessionId"], snapshot["assetRevision"], [row])

    def test_plan_hash_changes_when_declaration_changes(self):
        snapshot = self.intake("youtube")
        row = self.row("youtube")
        first = self.coordinator.prepare_plan(snapshot["sessionId"], snapshot["assetRevision"], [row])
        row["postSettings"]["made_for_kids"] = True
        second = self.coordinator.prepare_plan(snapshot["sessionId"], snapshot["assetRevision"], [row])
        self.assertNotEqual(first["planHash"], second["planHash"])
        with self.assertRaises(CoordinatorError):
            self.coordinator.confirm_plan(snapshot["sessionId"], first["planId"], first["planHash"], True, trusted_user_event_id="isolated-test-stale")

    def test_youtube_kids_content_cannot_notify_subscribers(self):
        snapshot = self.intake("youtube")
        row = self.row("youtube")
        row["postSettings"].update(made_for_kids=True, notify_subscribers=True)
        with self.assertRaises(CoordinatorError) as caught:
            self.coordinator.prepare_plan(snapshot["sessionId"], snapshot["assetRevision"], [row])
        self.assertEqual(caught.exception.code, "DECLARATION_CONFLICT")
        self.assertIsNone(self.coordinator.get_snapshot(snapshot["sessionId"])["pendingReview"])

    def test_only_confirmed_request_reaches_stub_executor_once(self):
        snapshot = self.intake("youtube")
        row = self.row("youtube")
        review = self.coordinator.prepare_plan(snapshot["sessionId"], snapshot["assetRevision"], [row])
        calls = []
        def stub(request):
            calls.append(request)
            return {"outcome": "success", "status": "TEST_ONLY", "message": "isolated stub, not a platform receipt"}
        self.coordinator.confirm_plan(snapshot["sessionId"], review["planId"], review["planHash"], True, trusted_user_event_id="isolated-test-only", confirmation_source="dashboard")
        self.coordinator.run_next(snapshot["sessionId"], stub)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["decision"], "draft")
        self.assertEqual(calls[0]["accountSettings"], row["postSettings"])
        self.assertEqual(self.coordinator.get_snapshot(snapshot["sessionId"])["stage"], "FINISHED")
        try:
            self.coordinator.run_next(snapshot["sessionId"], stub)
        except CoordinatorError:
            pass
        self.assertEqual(len(calls), 1)

    def test_missing_package_files_disable_config(self):
        config = self.root / "config.json"
        entry = copy.deepcopy(self.caps["youtube"])
        entry.update(platform="youtube", command=[os.path.realpath(os.sys.executable)], requiredFiles=[str(self.root / "missing.py")])
        config.write_text(json.dumps({"platforms": [entry]}))
        caps, commands, *_ = run_dashboard.load_configuration(config)
        self.assertFalse(caps["youtube"]["available"])
        self.assertNotIn("youtube", commands)

    def test_connection_only_forwards_unchanged_request_to_platform_entry(self):
        for platform in ("xiaohongshu", "youtube"):
            skill = self.root / platform
            (skill / "scripts").mkdir(parents=True)
            (skill / "SKILL.md").write_text("test-only stub package")
            (skill / "scripts/upstream_dispatch.py").write_text("# never executed")
            for decision in ("draft", "publish", "schedule"):
                with self.subTest(platform=platform, decision=decision):
                    request = dict(platform=platform, decision=decision, planHash="a" * 64,
                                   authorization={"receiptId": "test-only", "planHash": "a" * 64},
                                   source={"title": "原始中文", "unknownField": ["preserve", 1]},
                                   accountSettings={"platformOwnedOption": "unchanged"},
                                   scheduledAt="2099-01-01T12:00:00+08:00" if decision == "schedule" else None)
                    raw = json.dumps(request, ensure_ascii=False, indent=3)
                    receipt = {"status": "TEST_ONLY", "outcome": "needs_user",
                               "originSkill": platform + "-publish", "message": "platform-owned response"}
                    args = argparse.Namespace(platform=platform, skill_dir=skill,
                                              output_root=self.root / "not-created-by-controller",
                                              node=None, python=None)
                    completed = argparse.Namespace(returncode=0, stdout=json.dumps(receipt))
                    with patch.dict(os.environ, {}, clear=True), patch.object(adapter_runner.subprocess, "run", return_value=completed) as call:
                        result = adapter_runner.forward_request(args, raw)
                    self.assertEqual(result, receipt)
                    call.assert_called_once()
                    self.assertEqual(call.call_args.args[0][1], str(skill / "scripts/upstream_dispatch.py"))
                    self.assertEqual(call.call_args.kwargs["input"], raw)
                    self.assertFalse(call.call_args.kwargs["shell"])
                    self.assertFalse(args.output_root.exists())

    def test_missing_platform_entry_has_no_controller_fallback(self):
        skill = self.root / "old-package"
        skill.mkdir()
        (skill / "SKILL.md").write_text("old package without handoff entry")
        args = argparse.Namespace(platform="youtube", skill_dir=skill, output_root=self.root / "unused")
        raw = json.dumps({"platform": "youtube", "decision": "draft", "planHash": "a" * 64,
                          "authorization": {"receiptId": "test-only", "planHash": "a" * 64}})
        with patch.object(adapter_runner.subprocess, "run") as call:
            with self.assertRaises(adapter_runner.Stop) as error:
                adapter_runner.forward_request(args, raw)
        self.assertEqual(error.exception.status, "PLATFORM_ENTRY_UNAVAILABLE")
        call.assert_not_called()
        self.assertFalse(args.output_root.exists())

    def test_connection_does_not_trigger_without_approved_envelope(self):
        args = argparse.Namespace(platform="youtube", skill_dir=self.root, output_root=self.root / "unused")
        for decision in ("draft", "publish", "schedule"):
            with self.subTest(decision=decision), patch.object(adapter_runner.subprocess, "run") as call:
                with self.assertRaises(adapter_runner.Stop):
                    adapter_runner.forward_request(args, json.dumps({"platform": "youtube", "decision": decision}))
                call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
