#!/usr/bin/env python3
"""Local manifest bridge to the Chrome/Studio publisher. No Google API calls."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

from youtube_manifest import ManifestError, file_sha256, validate_manifest

ROOT = Path(__file__).resolve().parents[1]


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def record(value):
    path = Path(value)
    if not path.is_absolute() or not path.is_file() or path.is_symlink():
        raise ManifestError("Prepared assets must be existing absolute local files")
    return {"path": str(path), "size": path.stat().st_size, "sha256": file_sha256(path)}


def prepare(manifest_path):
    # Reuse the original preparation/validation functions unchanged.
    # This bridge only translates their effective output into DOM input fields.
    from youtube_publish import apply_localization_manifest, validate_files, merge_youtube_tags, build_body
    from contextlib import redirect_stdout
    from io import StringIO
    validated = validate_manifest(str(manifest_path))
    data = dict(validated["manifest"])
    args = argparse.Namespace(**data)
    args.tag = data["tags"]
    args.caption_language = data.get("caption_language")
    args.caption_name = data.get("caption_name")
    args.schedule_region = None
    args.schedule_date = None
    args.schedule_minute = None
    try:
        with redirect_stdout(StringIO()):
            apply_localization_manifest(args)
            validate_files(args)
            args.tag = merge_youtube_tags(list(args.tag or []))
            build_body(args)
    except SystemExit as exc:
        raise ManifestError("Original YouTube preparation checks did not pass") from exc
    assets = {key: record(getattr(args, key)) if getattr(args, key, None) else None
              for key in ("video", "thumbnail", "caption")}
    payload = {"version": 1, "job_id": data["job_id"], "account_profile": data["account_profile"],
               "mode": data["mode"], "title": args.title, "description": args.description,
               "tags": args.tag, "auto_tags": data["auto_tags"], "tag_region": data["tag_region"],
               "category_id": data["category_id"],
               "default_language": args.default_language, "privacy": data["privacy"],
               "publish_at": data["publish_at"], "made_for_kids": data["made_for_kids"],
               "contains_synthetic_media": data["contains_synthetic_media"],
               "notify_subscribers": data["notify_subscribers"], "assets": assets,
               "source_manifest": validated["manifest_path"],
               "source_fingerprint": validated["content_fingerprint"]}
    payload["fingerprint"] = fingerprint(payload)
    return payload


def connection(args):
    cdp = args.cdp_url or os.environ.get("EPOST_YOUTUBE_CDP_URL")
    channel = args.channel_id or os.environ.get("EPOST_YOUTUBE_CHANNEL_ID")
    config = args.browser_config or os.environ.get("EPOST_YOUTUBE_BROWSER_CONFIG")
    if config:
        path = Path(config)
        if not path.is_absolute():
            raise ManifestError("Browser config must be an absolute local path")
        settings = json.loads(path.read_text(encoding="utf-8"))["profiles"][args.profile]
        cdp, channel = cdp or settings.get("cdp_url"), channel or settings.get("channel_id")
    if not isinstance(cdp, str) or not re.fullmatch(r"http://(?:127\.0\.0\.1|localhost):[0-9]{1,5}", cdp):
        raise ManifestError("Configure a loopback Chrome CDP URL; browser login is not an OAuth API grant")
    if not 1 <= int(cdp.rsplit(":", 1)[1]) <= 65535:
        raise ManifestError("Invalid Chrome port")
    if channel is not None and not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", channel):
        raise ManifestError("Expected channel_id must be an actual YouTube channel ID")
    if args.command in {"dispatch", "resume"} and not channel:
        raise ManifestError("Inspect Chrome, then confirm and configure the exact channel ID before uploading")
    return cdp, channel


def run(args):
    payload = None
    if args.command in {"dry-run", "dispatch", "resume"}:
        payload = prepare(args.manifest)
        if args.command in {"dispatch", "resume"} and (not args.commit or args.decision != payload["mode"]):
            raise ManifestError("Exact decision/mode and --commit are required before any browser action")
        if args.command == "dry-run":
            return {"event": "dry_run_ok", "status": "BROWSER_MANIFEST_READY", "outcome": "success",
                    "transport": "browser", "youtube_contacted": False, "fingerprint": payload["fingerprint"]}
        if args.command == "dispatch" and args.decision == "draft":
            # Preserve the upstream contract: a local draft does not upload even privately.
            output = Path(args.manifest).with_suffix(".browser-draft.json")
            with output.open("x", encoding="utf-8") as stream:
                json.dump({"status": "DRAFT_SAVED", "payload": payload}, stream, ensure_ascii=False, indent=2)
            return {"event": "draft_saved", "status": "DRAFT_SAVED", "outcome": "success",
                    "draft": str(output), "youtube_contacted": False, "transport": "local"}
        args.profile = payload["account_profile"]
    cdp, channel = connection(args)
    node = args.node or shutil.which("node")
    if not node:
        raise ManifestError("Node.js is required for the Chrome script")
    command = [node, str(ROOT / "scripts/youtube_browser.mjs"), args.command, "--cdp-url", cdp]
    if channel:
        command.extend(["--channel-id", channel])
    if args.command in {"dispatch", "resume"}:
        command.extend(["--decision", args.decision, "--state-dir", str(Path(args.manifest).parent), "--commit"])
    if args.inspect_editor:
        command.append("--inspect-editor")
    if args.video_id:
        command.extend(["--video-id", args.video_id])
    try:
        result = subprocess.run(command, input=json.dumps(payload, ensure_ascii=False) if payload else "",
                                capture_output=True, text=True, shell=False, timeout=7200)
        value = json.loads(result.stdout)
        if result.returncode or not isinstance(value, dict) or not value.get("status"):
            raise ValueError("invalid browser receipt")
        return value
    except (ValueError, OSError, subprocess.TimeoutExpired):
        return {"status": "BROWSER_RESULT_UNKNOWN", "outcome": "unknown",
                "message": "网页脚本未返回可靠结果；请检查专用 Chrome 和本地记录，不要重新上传。"}


def parser():
    root = argparse.ArgumentParser(description="YouTube Studio Chrome script; no YouTube Data API")
    root.add_argument("command", choices=("inspect", "preflight", "dry-run", "dispatch", "resume", "verify"))
    root.add_argument("--manifest", type=Path)
    root.add_argument("--decision", choices=("draft", "publish", "schedule"))
    root.add_argument("--commit", action="store_true")
    root.add_argument("--cdp-url")
    root.add_argument("--channel-id")
    root.add_argument("--browser-config")
    root.add_argument("--profile", default="main")
    root.add_argument("--node")
    root.add_argument("--inspect-editor", action="store_true")
    root.add_argument("--video-id")
    return root


def main():
    args = parser().parse_args()
    try:
        output = run(args)
    except (ManifestError, OSError, ValueError, KeyError, TypeError):
        output = {"status": "BROWSER_SETUP_OR_MANIFEST_REQUIRED", "outcome": "needs_user",
                  "message": "请检查 YouTube 包的 Chrome 连接、已确认频道和英文素材；尚未触发网页上传。"}
    output["originSkill"] = "youtube-publish"
    print(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    main()
