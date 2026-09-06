#!/usr/bin/env python3
"""Dependency-free regression tests for manifest validation and safety gates."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from youtube_manifest import ManifestError, uploader_command, validate_manifest


BRIDGE = Path(__file__).with_name("youtube_tool_bridge.py")


def expect_error(manifest: Path, message: str) -> None:
    try:
        validate_manifest(str(manifest))
    except ManifestError as exc:
        assert message in str(exc), (message, str(exc))
    else:
        raise AssertionError(f"expected error containing {message!r}")


def main() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        video = root / "video.mp4"
        video.write_bytes(b"test-video")
        manifest = root / "youtube.json"
        base = {
            "job_id": "test-1",
            "account_profile": "main",
            "mode": "publish",
            "video": str(video),
            "title": "Private test",
            "description": "",
            "tags": [],
            "auto_tags": True,
            "tag_region": "US",
            "privacy": "private",
            "publish_at": None,
            "made_for_kids": False,
            "contains_synthetic_media": None,
            "notify_subscribers": False,
        }
        manifest.write_text(json.dumps(base), encoding="utf-8")
        result = validate_manifest(str(manifest))
        assert result["manifest"]["privacy"] == "private"
        assert result["manifest"]["mode"] == "publish"
        assert result["manifest"]["auto_tags"] is True
        assert len(result["content_fingerprint"]) == 64

        invalid = dict(base, made_for_kids=None)
        manifest.write_text(json.dumps(invalid), encoding="utf-8")
        expect_error(manifest, "made_for_kids")

        invalid = dict(base, made_for_kids=True, notify_subscribers=True)
        manifest.write_text(json.dumps(invalid), encoding="utf-8")
        expect_error(manifest, "notify_subscribers must be false")

        invalid = dict(base, mode="schedule", publish_at=None)
        manifest.write_text(json.dumps(invalid), encoding="utf-8")
        expect_error(manifest, "publish_at is required")

        future = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
        scheduled = dict(base, mode="schedule", privacy="private", publish_at=future)
        manifest.write_text(json.dumps(scheduled), encoding="utf-8")
        scheduled_result = validate_manifest(str(manifest))
        assert scheduled_result["manifest"]["mode"] == "schedule"
        schedule_upload = uploader_command(scheduled_result, commit=True)
        assert schedule_upload[schedule_upload.index("--publish-at") + 1] == scheduled_result["manifest"]["publish_at"]
        assert schedule_upload[-1] == "--commit"

        scheduled_public = dict(scheduled, privacy="public")
        manifest.write_text(json.dumps(scheduled_public), encoding="utf-8")
        expect_error(manifest, "scheduled uploads must use privacy=private")

        manifest.write_text(json.dumps(scheduled), encoding="utf-8")
        no_schedule_commit = [
            sys.executable, str(Path(__file__).with_name("youtube_manifest.py")),
            "schedule", "--manifest", str(manifest),
        ]
        completed = subprocess.run(no_schedule_commit, capture_output=True, text=True)
        assert completed.returncode != 0
        assert "--commit" in completed.stdout

        manifest.write_text(json.dumps(base), encoding="utf-8")
        command = [sys.executable, str(Path(__file__).with_name("youtube_manifest.py")),
                   "upload", "--manifest", str(manifest)]
        completed = subprocess.run(command, capture_output=True, text=True)
        assert completed.returncode != 0
        assert "--commit" in completed.stdout

        draft_payload = dict(base, mode="draft")
        manifest.write_text(json.dumps(draft_payload), encoding="utf-8")
        draft_path = root / "saved-draft.json"
        command = [
            sys.executable, str(Path(__file__).with_name("youtube_manifest.py")),
            "dispatch", "--decision", "draft", "--manifest", str(manifest),
            "--output", str(draft_path), "--commit",
        ]
        completed = subprocess.run(command, capture_output=True, text=True)
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert json.loads(draft_path.read_text())["status"] == "DRAFT_SAVED"
        assert json.loads(completed.stdout)["youtube_contacted"] is False

        mismatch = [
            sys.executable, str(Path(__file__).with_name("youtube_manifest.py")),
            "dispatch", "--decision", "publish", "--manifest", str(manifest), "--commit",
        ]
        completed = subprocess.run(mismatch, capture_output=True, text=True)
        assert completed.returncode != 0
        assert "DECISION_MODE_MISMATCH" in completed.stdout

        manifest.write_text(json.dumps(draft_payload), encoding="utf-8")
        schedule_mismatch = [
            sys.executable, str(Path(__file__).with_name("youtube_manifest.py")),
            "dispatch", "--decision", "schedule", "--manifest", str(manifest), "--commit",
        ]
        completed = subprocess.run(schedule_mismatch, capture_output=True, text=True)
        assert completed.returncode != 0
        assert "DECISION_MODE_MISMATCH" in completed.stdout

        bridge_validate = subprocess.run(
            [sys.executable, str(BRIDGE)],
            input=json.dumps({
                "command": "validate", "decision": None, "manifest": str(manifest),
                "profile": None, "client_secrets": None, "commit": False,
            }),
            capture_output=True,
            text=True,
        )
        assert bridge_validate.returncode == 0, bridge_validate.stdout + bridge_validate.stderr

        bridge_no_decision = subprocess.run(
            [sys.executable, str(BRIDGE)],
            input=json.dumps({
                "command": "dispatch", "decision": None, "manifest": str(manifest),
                "profile": None, "client_secrets": None, "commit": True,
            }),
            capture_output=True,
            text=True,
        )
        assert bridge_no_decision.returncode == 2
        assert "requires decision" in bridge_no_decision.stdout

        invalid_region = dict(base, tag_region="USA")
        manifest.write_text(json.dumps(invalid_region), encoding="utf-8")
        expect_error(manifest, "tag_region")

    print(json.dumps({"event": "tests_passed", "count": 13}))


if __name__ == "__main__":
    main()
