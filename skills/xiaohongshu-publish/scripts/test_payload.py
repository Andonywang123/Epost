#!/usr/bin/env python3
"""Smoke tests for the manifest validator without browser or network access."""

from __future__ import annotations

import json
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
VALIDATOR = ROOT / "xhs_payload.py"
PUBLISHER = ROOT / "xhs_publisher.mjs"
BRIDGE = ROOT / "xhs_tool_bridge.mjs"


def run(manifest: dict) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        image = root / "01.png"
        image.write_bytes(b"epost-test-image")
        manifest["images"] = [str(image)]
        path = root / "post.json"
        path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        return subprocess.run(
            ["python3", str(VALIDATOR), "validate", "--manifest", str(path)],
            check=False,
            capture_output=True,
            text=True,
        )


def base_manifest() -> dict:
    return {
        "job_id": "test-job",
        "account_profile": "main",
        "mode": "draft",
        "title": "测试标题",
        "body": "测试正文",
        "tags": ["AI", "#AI", "科研"],
        "auto_tags": True,
        "tag_limit": 8,
        "visibility": "公开可见",
        "activity": None,
        "activity_mode": "auto",
        "schedule_at": None,
    }


def run_dispatch(manifest: dict, decision: str | None) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        image = root / "01.png"
        image.write_bytes(b"epost-test-image")
        manifest["images"] = [str(image)]
        path = root / "post.json"
        path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        command = ["node", str(PUBLISHER), "dispatch", "--manifest", str(path), "--commit"]
        if decision:
            command.extend(["--decision", decision])
        return subprocess.run(command, check=False, capture_output=True, text=True)


def run_direct(manifest: dict, command_name: str) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        image = root / "01.png"
        image.write_bytes(b"epost-test-image")
        manifest["images"] = [str(image)]
        path = root / "post.json"
        path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        return subprocess.run(
            ["node", str(PUBLISHER), command_name, "--manifest", str(path), "--commit"],
            check=False,
            capture_output=True,
            text=True,
        )


def run_bridge(manifest: dict, payload: dict) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        image = root / "01.png"
        image.write_bytes(b"epost-test-image")
        manifest["images"] = [str(image)]
        path = root / "post.json"
        path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        payload["manifest"] = str(path)
        return subprocess.run(
            ["node", str(BRIDGE)],
            input=json.dumps(payload, ensure_ascii=False),
            check=False,
            capture_output=True,
            text=True,
        )


def main() -> None:
    valid = run(base_manifest())
    assert valid.returncode == 0, valid.stdout + valid.stderr
    payload = json.loads(valid.stdout)["payload"]
    assert payload["visibility"] == "公开"
    assert payload["tags"] == ["AI", "科研"]
    assert payload["auto_tags"] is True
    assert payload["activity_mode"] == "auto"
    assert len(payload["content_fingerprint"]) == 64

    long_title = base_manifest()
    long_title["title"] = "这是一条确定超过小红书二十字符限制的测试标题文本"
    invalid = run(long_title)
    assert invalid.returncode == 2
    assert json.loads(invalid.stdout)["error"] == "title_too_long"

    schedule = base_manifest()
    schedule["mode"] = "schedule"
    schedule["schedule_at"] = None
    invalid_schedule = run(schedule)
    assert invalid_schedule.returncode == 2
    assert json.loads(invalid_schedule.stdout)["error"] == "schedule_at_required"

    valid_schedule = base_manifest()
    valid_schedule["mode"] = "schedule"
    valid_schedule["schedule_at"] = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
    scheduled = run(valid_schedule)
    assert scheduled.returncode == 0, scheduled.stdout + scheduled.stderr
    scheduled_payload = json.loads(scheduled.stdout)["payload"]
    assert scheduled_payload["mode"] == "schedule"
    assert scheduled_payload["schedule_at"] is not None

    invalid_activity = base_manifest()
    invalid_activity["activity"] = 123
    activity_result = run(invalid_activity)
    assert activity_result.returncode == 2
    assert json.loads(activity_result.stdout)["error"] == "activity_must_be_string_or_null"

    exact_activity = base_manifest()
    exact_activity["activity_mode"] = "exact"
    exact_result = run(exact_activity)
    assert exact_result.returncode == 2
    assert json.loads(exact_result.stdout)["error"] == "activity_required_for_exact_mode"

    invalid_tag_limit = base_manifest()
    invalid_tag_limit["tag_limit"] = 11
    tag_limit_result = run(invalid_tag_limit)
    assert tag_limit_result.returncode == 2
    assert json.loads(tag_limit_result.stdout)["error"] == "tag_limit_out_of_range"

    mismatch = run_dispatch(base_manifest(), "publish")
    assert mismatch.returncode == 2
    assert json.loads(mismatch.stdout)["error"] == "DECISION_MODE_MISMATCH"

    schedule_mismatch = run_dispatch(base_manifest(), "schedule")
    assert schedule_mismatch.returncode == 2
    assert json.loads(schedule_mismatch.stdout)["error"] == "DECISION_MODE_MISMATCH"

    missing_decision = run_dispatch(base_manifest(), None)
    assert missing_decision.returncode == 2
    assert json.loads(missing_decision.stdout)["error"] == "DISPATCH_DECISION_REQUIRED"

    direct_mismatch = run_direct(base_manifest(), "publish")
    assert direct_mismatch.returncode == 2
    assert json.loads(direct_mismatch.stdout)["error"] == "COMMAND_MODE_MISMATCH"

    direct_schedule_mismatch = run_direct(base_manifest(), "schedule")
    assert direct_schedule_mismatch.returncode == 2
    assert json.loads(direct_schedule_mismatch.stdout)["error"] == "COMMAND_MODE_MISMATCH"

    bridge_validate = run_bridge(base_manifest(), {
        "command": "validate", "decision": None, "profile_dir": None,
        "cdp_url": None, "commit": False, "keep_open": False, "screenshot": None,
    })
    assert bridge_validate.returncode == 0, bridge_validate.stdout + bridge_validate.stderr

    bridge_no_commit = run_bridge(base_manifest(), {
        "command": "dispatch", "decision": "draft", "profile_dir": None,
        "cdp_url": "http://127.0.0.1:9222", "commit": False,
        "keep_open": False, "screenshot": None,
    })
    assert bridge_no_commit.returncode == 2
    assert json.loads(bridge_no_commit.stdout)["error"] == "dispatch_requires_commit_true"

    print("payload and dispatch smoke tests passed (14 cases)")


if __name__ == "__main__":
    main()
