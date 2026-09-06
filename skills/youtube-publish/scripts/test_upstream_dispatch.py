#!/usr/bin/env python3
"""Isolated tests: fake files, fake credentials, mocked platform subprocesses."""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import upstream_dispatch as dispatch


class UpstreamDispatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.video = self.root / "test.mp4"
        self.video.write_bytes(b"fake video: never upload")
        self.cover = self.root / "test.jpg"
        self.cover.write_bytes(b"fake cover: never upload")
        self.localized = self.root / "localized.json"
        self.localized.write_text("{}", encoding="utf-8")
        self.args = argparse.Namespace(output_root=self.root / "jobs", python="fake-python", transport="api")
        self.env = patch.dict("os.environ", {"YOUTUBE_FAKE_TEST_KEY": "fake-only-no-network"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def record(self, path):
        value = path.read_bytes()
        return {"path": str(path), "name": path.name, "size": len(value),
                "sha256": hashlib.sha256(value).hexdigest()}

    def request(self, decision="draft"):
        source = {"assetRevision": 3, "media": {"kind": "video", "files": [self.record(self.video)]},
                  "metadata": {"title": "测试标题", "body": "仅隔离模拟，不进行真实发布", "cover": self.record(self.cover)}}
        receipt = {"receiptId": "test-receipt", "sessionId": "test-session", "planId": "test-plan",
                   "planHash": "a" * 64, "sourceHash": dispatch.digest(source), "assetRevision": 3,
                   "confirmationSource": "dashboard", "trustedUserEventId": "fake-user-event",
                   "confirmedAt": datetime.now(timezone.utc).isoformat()}
        request = {"platform": "youtube", "decision": decision, "jobId": "test-" + decision,
                   "accountProfile": "fake-account", "source": source,
                   "sessionId": receipt["sessionId"], "planId": receipt["planId"], "planHash": receipt["planHash"],
                   "authorization": receipt, "accountSettings": {"made_for_kids": False,
                       "contains_synthetic_media": False, "notify_subscribers": False,
                       "privacy": "public", "api_key_env": "YOUTUBE_FAKE_TEST_KEY", "locale": "British English"},
                   "scheduledAt": None, "scheduleUtc": None, "timezone": None,
                   "draftScope": "local", "acceptLocalDraft": True}
        if decision == "schedule":
            moment = datetime.now(timezone.utc) + timedelta(days=1)
            request.update(scheduledAt=moment.isoformat(), scheduleUtc=moment.isoformat(), timezone="UTC")
        return request

    def successful_events(self, decision):
        final = {"event": "draft_saved", "youtube_contacted": False, "draft": "fake-local-receipt"} if decision == "draft" else {"event": "accepted", "video_id": "fake-video-id"}
        return [{"event": "localization_preflight_ok"}, {"status": "READY", "manifest": str(self.localized)},
                {"event": "dry_run_ok"}, final]

    def test_three_literal_decisions_keep_fields_and_only_draft_is_local(self):
        for decision in ("draft", "publish", "schedule"):
            with self.subTest(decision=decision):
                request = self.request(decision)
                frozen = copy.deepcopy(request)
                with patch.object(dispatch, "invoke", side_effect=self.successful_events(decision)) as call:
                    result = dispatch.dispatch_request(self.args, request)
                self.assertEqual(result["outcome"], "success", result)
                self.assertEqual(result["originSkill"], "youtube-publish")
                self.assertEqual(request, frozen)
                self.assertEqual(call.call_count, 4)
                calls = [args.args[0] for args in call.call_args_list]
                self.assertEqual(calls[0][-1], "preflight")
                self.assertIn("--translation-provider", calls[0])
                self.assertEqual(calls[0][calls[0].index("--translation-provider") + 1], "argos")
                localizer = calls[1]
                self.assertIn("--source-title=" + request["source"]["metadata"]["title"], localizer)
                self.assertIn("--source-description=" + request["source"]["metadata"]["body"], localizer)
                self.assertEqual(localizer[localizer.index("--thumbnail") + 1], str(self.cover))
                final = calls[-1]
                self.assertEqual(final[final.index("--decision") + 1], decision)
                self.assertEqual(final[-1], "--commit")
                self.assertTrue(call.call_args_list[-1].kwargs["effect"])
                manifest = json.loads(Path(result["manifest"]).read_text())
                self.assertEqual(manifest["mode"], decision)
                self.assertEqual(manifest["account_profile"], "fake-account")
                self.assertEqual(manifest["publish_at"], request["scheduledAt"])
                self.assertEqual(manifest["privacy"], "private" if decision == "schedule" else "public")
                if decision == "draft":
                    self.assertFalse(result["youtubeContacted"])

    def test_browser_replaces_only_final_upload_command(self):
        self.args.transport = "browser"
        self.args.browser_config = "/fake/config.json"
        request = self.request("publish")
        replies = self.successful_events("publish")
        replies[-1] = {"status": "SUBMITTED", "outcome": "pending", "video_id": "abcdefghijk",
                       "youtube_contacted": True, "message": "Studio receipt"}
        with patch.object(dispatch, "invoke", side_effect=replies) as call:
            result = dispatch.dispatch_request(self.args, request)
        commands = [item.args[0] for item in call.call_args_list]
        self.assertTrue(commands[1][1].endswith("youtube_localize.py"))
        self.assertTrue(commands[2][1].endswith("youtube_manifest.py"))
        self.assertTrue(commands[3][1].endswith("youtube_browser.py"))
        self.assertIn("/fake/config.json", commands[3])
        self.assertEqual(result["outcome"], "pending")
        self.assertEqual(result["transport"], "browser")

    def test_browser_route_preserves_local_draft_without_chrome(self):
        self.args.transport = "browser"
        with patch.object(dispatch, "invoke", side_effect=self.successful_events("draft")) as call:
            result = dispatch.dispatch_request(self.args, self.request("draft"))
        self.assertTrue(call.call_args.args[0][1].endswith("youtube_manifest.py"))
        self.assertFalse(result["youtubeContacted"])

    def test_missing_openai_key_is_youtube_owned_stop_after_validated_trigger(self):
        request = self.request()
        request["accountSettings"]["translation_provider"] = "openai"
        with patch.dict("os.environ", {}, clear=True), patch.object(dispatch, "invoke") as call:
            result = dispatch.dispatch_request(self.args, request)
        self.assertEqual(result["status"], "LOCALIZATION_KEY_REQUIRED")
        self.assertEqual(result["originSkill"], "youtube-publish")
        self.assertEqual(result["outcome"], "needs_user")
        call.assert_not_called()
        self.assertTrue((self.args.output_root / request["jobId"] / "attempt.json").is_file())
        self.assertFalse((self.args.output_root / request["jobId"] / "youtube.json").exists())

    def test_no_authorization_never_prepares_or_checks_credentials(self):
        request = self.request()
        request.pop("authorization")
        with patch.object(dispatch, "youtube") as execute, patch.dict("os.environ", {}, clear=True):
            result = dispatch.dispatch_request(self.args, request)
        self.assertEqual(result["status"], "CONFIRMATION_REQUIRED")
        execute.assert_not_called()
        self.assertFalse(self.args.output_root.exists())

    def test_kids_content_with_notifications_is_rejected_before_job_creation(self):
        request = self.request("publish")
        request["accountSettings"].update(made_for_kids=True, notify_subscribers=True)
        with patch.object(dispatch, "invoke") as call:
            result = dispatch.dispatch_request(self.args, request)
        self.assertEqual(result["status"], "DECLARATION_CONFLICT")
        self.assertEqual(result["outcome"], "needs_user")
        call.assert_not_called()
        self.assertFalse(self.args.output_root.exists())

    def test_missing_or_mismatched_receipt_fields_never_execute(self):
        for field in ("receiptId", "sessionId", "planId", "planHash", "trustedUserEventId", "confirmationSource", "confirmedAt"):
            for action in ("missing", "mismatch"):
                if action == "mismatch" and field not in {"sessionId", "planId", "planHash", "confirmationSource"}:
                    continue
                with self.subTest(field=field, action=action):
                    request = self.request()
                    if action == "missing":
                        request["authorization"].pop(field)
                    else:
                        request["authorization"][field] = "not-the-confirmation"
                    with patch.object(dispatch, "invoke") as call:
                        result = dispatch.dispatch_request(self.args, request)
                    self.assertEqual(result["status"], "CONFIRMATION_REQUIRED", result)
                    call.assert_not_called()
                    self.assertFalse(self.args.output_root.exists())

    def test_source_hash_or_video_or_cover_changed_never_executes(self):
        for target in ("hash", "video", "cover"):
            with self.subTest(target=target):
                request = self.request()
                if target == "hash":
                    request["authorization"]["sourceHash"] = "b" * 64
                else:
                    path = self.video if target == "video" else self.cover
                    path.write_bytes(path.read_bytes() + b"changed")
                with patch.object(dispatch, "invoke") as call:
                    result = dispatch.dispatch_request(self.args, request)
                self.assertEqual(result["status"], "SOURCE_CHANGED")
                call.assert_not_called()
                self.assertFalse(self.args.output_root.exists())

    def test_source_size_and_revision_are_verified(self):
        for target in ("size", "revision"):
            request = self.request()
            if target == "size":
                request["source"]["media"]["files"][0]["size"] += 1
                request["authorization"]["sourceHash"] = dispatch.digest(request["source"])
            else:
                request["authorization"]["assetRevision"] += 1
            with patch.object(dispatch, "invoke") as call:
                result = dispatch.dispatch_request(self.args, request)
            self.assertEqual(result["status"], "SOURCE_CHANGED")
            call.assert_not_called()

    def test_existing_job_is_not_replayed_even_after_preparation_failure(self):
        request = self.request()
        request["accountSettings"]["translation_provider"] = "openai"
        with patch.dict("os.environ", {}, clear=True):
            first = dispatch.dispatch_request(self.args, request)
        self.assertEqual(first["status"], "LOCALIZATION_KEY_REQUIRED")
        receipt = (self.args.output_root / request["jobId"] / "result.json").read_bytes()
        with patch.object(dispatch, "invoke") as call:
            second = dispatch.dispatch_request(self.args, request)
        self.assertEqual(second["status"], "JOB_ALREADY_ATTEMPTED")
        self.assertEqual(second["outcome"], "unknown")
        call.assert_not_called()
        self.assertEqual((self.args.output_root / request["jobId"] / "result.json").read_bytes(), receipt)

    def test_preparation_failure_never_commits(self):
        for stage in ("preflight", "localize", "dry-run"):
            with self.subTest(stage=stage):
                request = self.request()
                request["jobId"] = "test-failed-" + stage
                responses = self.successful_events("draft")
                position = {"preflight": 0, "localize": 1, "dry-run": 2}[stage]
                responses[position] = {"event": "error", "status": "NEEDS_REVIEW"}
                with patch.object(dispatch, "invoke", side_effect=responses) as call:
                    result = dispatch.dispatch_request(self.args, request)
                self.assertEqual(result["outcome"], "needs_user")
                self.assertEqual(call.call_count, position + 1)
                self.assertFalse(any("--commit" in item.args[0] for item in call.call_args_list))

    def test_schedule_requires_matching_zone_and_utc_and_future(self):
        for kind, expected in (("missing_zone", "INVALID_TIME"), ("wrong_utc", "TIMEZONE_MISMATCH"),
                               ("wrong_zone", "TIMEZONE_MISMATCH"), ("expired", "SCHEDULE_EXPIRED")):
            request = self.request("schedule")
            if kind == "missing_zone":
                request["timezone"] = None
            elif kind == "wrong_zone":
                request["timezone"] = "Asia/Shanghai"
            elif kind == "wrong_utc":
                request["scheduleUtc"] = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
            else:
                request["scheduledAt"] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
                request["scheduleUtc"] = request["scheduledAt"]
            with patch.object(dispatch, "invoke") as call:
                result = dispatch.dispatch_request(self.args, request)
            self.assertEqual(result["status"], expected, result)
            call.assert_not_called()

    def test_publish_or_draft_cannot_inherit_schedule(self):
        for decision in ("draft", "publish"):
            request = self.request(decision)
            request["timezone"] = "UTC"
            with patch.object(dispatch, "invoke") as call:
                result = dispatch.dispatch_request(self.args, request)
            self.assertEqual(result["status"], "DECISION_TIME_MISMATCH")
            call.assert_not_called()

    def test_draft_requires_explicit_local_scope_and_acceptance(self):
        for field, value in (("acceptLocalDraft", False), ("draftScope", "platform")):
            request = self.request()
            request["jobId"] = "local-ack-" + field
            request[field] = value
            with patch.object(dispatch, "invoke") as call:
                result = dispatch.dispatch_request(self.args, request)
            self.assertEqual(result["status"], "LOCAL_DRAFT_ACK_REQUIRED")
            call.assert_not_called()

    def test_source_modified_during_localization_is_not_submitted(self):
        request = self.request()
        responses = self.successful_events("draft")
        def fake_call(command, **kwargs):
            result = responses.pop(0)
            if result.get("event") == "dry_run_ok":
                self.video.write_bytes(b"changed while preparing")
            return result
        with patch.object(dispatch, "invoke", side_effect=fake_call) as call:
            result = dispatch.dispatch_request(self.args, request)
        self.assertEqual(result["status"], "SOURCE_CHANGED")
        self.assertEqual(call.call_count, 3)
        self.assertFalse(any("--commit" in item.args[0] for item in call.call_args_list))

    def test_mismatched_or_unproven_final_results_are_not_success(self):
        cases = [("draft", {"event": "accepted", "video_id": "fake"}),
                 ("draft", {"event": "draft_saved"}),
                 ("publish", {"event": "draft_saved", "youtube_contacted": False}),
                 ("publish", {"event": "accepted"})]
        for index, (decision, final) in enumerate(cases):
            request = self.request(decision)
            request["jobId"] = "bad-final-" + str(index)
            responses = self.successful_events(decision)
            responses[-1] = final
            with patch.object(dispatch, "invoke", side_effect=responses):
                result = dispatch.dispatch_request(self.args, request)
            self.assertEqual(result["outcome"], "unknown")

    def test_partial_result_preserves_video_id_and_failed_substeps(self):
        request = self.request("publish")
        responses = self.successful_events("publish")
        responses[-1] = {"event": "partial_success", "video_id": "fake-id", "partial_errors": [{"step": "caption", "detail": "fake-error"}]}
        with patch.object(dispatch, "invoke", side_effect=responses):
            result = dispatch.dispatch_request(self.args, request)
        self.assertEqual(result["outcome"], "partial")
        self.assertEqual(result["videoId"], "fake-id")
        self.assertEqual(result["partialErrors"][0]["step"], "caption")

    def test_localization_receives_option_like_text_verbatim(self):
        from youtube_localize import parser
        request = self.request()
        request["source"]["metadata"].update(title="--title", body="--help")
        request["authorization"]["sourceHash"] = dispatch.digest(request["source"])
        with patch.object(dispatch, "invoke", side_effect=self.successful_events("draft")) as call:
            result = dispatch.dispatch_request(self.args, request)
        self.assertEqual(result["outcome"], "success")
        parsed = parser().parse_args(call.call_args_list[1].args[0][2:])
        self.assertEqual(parsed.source_title, "--title")
        self.assertEqual(parsed.source_description, "--help")


if __name__ == "__main__":
    unittest.main()
