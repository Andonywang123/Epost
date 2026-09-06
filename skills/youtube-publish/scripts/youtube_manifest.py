#!/usr/bin/env python3
"""Manifest-first, safety-gated front end for the YouTube API uploader."""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ALLOWED_FIELDS = {
    "job_id", "account_profile", "mode", "video", "title", "description",
    "tags", "category_id", "default_language", "privacy", "publish_at",
    "made_for_kids", "contains_synthetic_media", "notify_subscribers",
    "thumbnail", "caption", "caption_language", "caption_name",
    "localization_manifest", "poll_processing", "auto_tags", "tag_region",
}
VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".avi", ".wmv", ".flv", ".webm", ".mpeg", ".mpg"}
CAPTION_SUFFIXES = {".srt", ".vtt", ".sbv", ".sub", ".ttml"}
PRIVACY = {"private", "unlisted", "public"}
MODES = {"draft", "publish", "schedule", "upload"}


class ManifestError(ValueError):
    pass


def emit(event: str, **data: Any) -> None:
    print(json.dumps({"event": event, **data}, ensure_ascii=False), flush=True)


def fail(message: str, code: int = 2, **data: Any) -> None:
    emit("error", message=message, **data)
    raise SystemExit(code)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require_string(data: dict[str, Any], name: str, *, allow_empty: bool = False) -> str:
    value = data.get(name)
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ManifestError(f"{name} must be a non-empty string")
    return value


def optional_file(data: dict[str, Any], name: str, suffixes: set[str] | None = None) -> Path | None:
    value = data.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ManifestError(f"{name} must be null or an absolute file path")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ManifestError(f"{name} must use an absolute path")
    path = path.resolve()
    if not path.is_file():
        raise ManifestError(f"{name} does not exist: {path}")
    if suffixes and path.suffix.lower() not in suffixes:
        raise ManifestError(f"{name} has an unsupported extension: {path.suffix}")
    return path


def parse_future_time(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ManifestError("publish_at must be null or an ISO 8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ManifestError("publish_at must be valid ISO 8601") from exc
    if parsed.tzinfo is None:
        raise ManifestError("publish_at must include a timezone")
    if parsed.astimezone(timezone.utc) <= datetime.now(timezone.utc):
        raise ManifestError("publish_at must be in the future")
    return parsed.isoformat()


def validate_manifest(path_value: str) -> dict[str, Any]:
    path = Path(path_value).expanduser().resolve()
    if not path.is_file():
        raise ManifestError(f"manifest does not exist: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"cannot read manifest: {exc}") from exc
    if not isinstance(data, dict):
        raise ManifestError("manifest root must be a JSON object")
    unknown = sorted(set(data) - ALLOWED_FIELDS)
    if unknown:
        raise ManifestError(f"unknown fields: {', '.join(unknown)}")

    job_id = require_string(data, "job_id")
    if len(job_id) > 128:
        raise ManifestError("job_id must not exceed 128 characters")
    account_profile = require_string(data, "account_profile")
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", account_profile):
        raise ManifestError("account_profile may contain only letters, numbers, dot, underscore, or hyphen")
    raw_mode = require_string(data, "mode")
    if raw_mode not in MODES:
        raise ManifestError(f"mode must be one of: {', '.join(sorted(MODES))}")
    mode = "publish" if raw_mode == "upload" else raw_mode

    video = optional_file(data, "video", VIDEO_SUFFIXES)
    if video is None:
        raise ManifestError("video is required")
    title = require_string(data, "title")
    if len(title) > 100:
        raise ManifestError("title must not exceed 100 characters")
    description = data.get("description", "")
    if not isinstance(description, str):
        raise ManifestError("description must be a string")
    if len(description) > 5000:
        raise ManifestError("description must not exceed 5000 characters")
    tags = data.get("tags", [])
    if not isinstance(tags, list) or any(not isinstance(item, str) or not item.strip() for item in tags):
        raise ManifestError("tags must be an array of non-empty strings")
    tags = list(dict.fromkeys(item.strip().lstrip("#") for item in tags))
    auto_tags = data.get("auto_tags", True)
    if not isinstance(auto_tags, bool):
        raise ManifestError("auto_tags must be boolean")
    tag_region = data.get("tag_region", "US")
    if not isinstance(tag_region, str) or not re.fullmatch(r"[A-Za-z]{2}", tag_region):
        raise ManifestError("tag_region must be a two-letter ISO country code")
    tag_region = tag_region.upper()
    category_id = str(data.get("category_id", "22"))
    if not category_id.isdigit():
        raise ManifestError("category_id must contain digits only")
    default_language = data.get("default_language", "zh-CN")
    if not isinstance(default_language, str) or not default_language.strip():
        raise ManifestError("default_language must be a non-empty string")

    privacy = require_string(data, "privacy")
    if privacy not in PRIVACY:
        raise ManifestError(f"privacy must be one of: {', '.join(sorted(PRIVACY))}")
    publish_at = parse_future_time(data.get("publish_at"))
    if mode == "schedule" and publish_at is None:
        raise ManifestError("publish_at is required when mode=schedule")
    if mode in {"draft", "publish"} and publish_at is not None:
        raise ManifestError("publish_at must be null when mode=draft or mode=publish")
    if publish_at and privacy != "private":
        raise ManifestError("scheduled uploads must use privacy=private")

    made_for_kids = data.get("made_for_kids")
    if not isinstance(made_for_kids, bool):
        raise ManifestError("made_for_kids must be an explicit boolean")
    synthetic = data.get("contains_synthetic_media")
    if synthetic is not None and not isinstance(synthetic, bool):
        raise ManifestError("contains_synthetic_media must be boolean or null")
    notify = data.get("notify_subscribers", False)
    if not isinstance(notify, bool):
        raise ManifestError("notify_subscribers must be boolean")
    if made_for_kids and notify:
        raise ManifestError("notify_subscribers must be false when made_for_kids is true")
    poll = data.get("poll_processing", False)
    if not isinstance(poll, bool):
        raise ManifestError("poll_processing must be boolean")

    thumbnail = optional_file(data, "thumbnail")
    caption = optional_file(data, "caption", CAPTION_SUFFIXES)
    caption_language = data.get("caption_language")
    if caption and (not isinstance(caption_language, str) or not caption_language.strip()):
        raise ManifestError("caption_language is required when caption is supplied")
    caption_name = data.get("caption_name")
    if caption_name is not None and not isinstance(caption_name, str):
        raise ManifestError("caption_name must be a string or null")
    localization = optional_file(data, "localization_manifest")

    normalized = dict(data)
    normalized.update({
        "video": str(video),
        "description": description,
        "tags": tags,
        "auto_tags": auto_tags,
        "tag_region": tag_region,
        "mode": mode,
        "category_id": category_id,
        "default_language": default_language,
        "publish_at": publish_at,
        "notify_subscribers": notify,
        "thumbnail": str(thumbnail) if thumbnail else None,
        "caption": str(caption) if caption else None,
        "localization_manifest": str(localization) if localization else None,
        "poll_processing": poll,
    })
    canonical = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    video_sha256 = file_sha256(video)
    return {
        "manifest_path": str(path),
        "manifest": normalized,
        "video_sha256": video_sha256,
        "content_fingerprint": hashlib.sha256(
            canonical.encode("utf-8") + video_sha256.encode("ascii")
        ).hexdigest(),
        "video_bytes": video.stat().st_size,
        "video_mime": mimetypes.guess_type(video.name)[0] or "application/octet-stream",
    }


def uploader_command(validated: dict[str, Any], commit: bool) -> list[str]:
    data = validated["manifest"]
    script = Path(__file__).with_name("youtube_publish.py")
    command = [
        sys.executable, str(script), "publish",
        "--profile", data["account_profile"],
        "--job-id", data["job_id"],
        "--video", data["video"],
        "--title", data["title"],
        "--description", data["description"],
        "--category-id", data["category_id"],
        "--default-language", data["default_language"],
        "--privacy", data["privacy"],
        "--made-for-kids", str(data["made_for_kids"]).lower(),
    ]
    for tag in data["tags"]:
        command.extend(["--tag", tag])
    command.extend(["--tag-region", data["tag_region"]])
    command.append("--auto-tags" if data["auto_tags"] else "--no-auto-tags")
    scalar_flags = {
        "publish_at": "--publish-at",
        "thumbnail": "--thumbnail",
        "caption": "--caption",
        "caption_language": "--caption-language",
        "caption_name": "--caption-name",
        "localization_manifest": "--localization-manifest",
    }
    for field, flag in scalar_flags.items():
        if data.get(field):
            command.extend([flag, str(data[field])])
    if data.get("contains_synthetic_media") is not None:
        command.extend(["--contains-synthetic-media", str(data["contains_synthetic_media"]).lower()])
    if data["notify_subscribers"]:
        command.append("--notify-subscribers")
    if data["poll_processing"]:
        command.append("--poll")
    command.append("--commit" if commit else "--dry-run")
    return command


def validate_command(args: argparse.Namespace) -> None:
    try:
        result = validate_manifest(args.manifest)
    except ManifestError as exc:
        fail(str(exc))
    emit("validated", **{key: value for key, value in result.items() if key != "manifest"},
         mode=result["manifest"]["mode"], privacy=result["manifest"]["privacy"])


def require_mode(result: dict[str, Any], allowed: set[str], command: str) -> None:
    mode = result["manifest"]["mode"]
    if mode not in allowed:
        fail(
            "Command does not match the manifest mode.",
            command=command,
            manifest_mode=mode,
            allowed_modes=sorted(allowed),
        )


def publish_command(args: argparse.Namespace) -> None:
    if not args.commit:
        fail("Refusing to publish without --commit.", required_flag="--commit")
    try:
        result = validate_manifest(args.manifest)
    except ManifestError as exc:
        fail(str(exc))
    require_mode(result, {"publish"}, "publish")
    completed = subprocess.run(uploader_command(result, commit=True))
    raise SystemExit(completed.returncode)


def schedule_command(args: argparse.Namespace) -> None:
    if not args.commit:
        fail("Refusing to schedule without --commit.", required_flag="--commit")
    try:
        result = validate_manifest(args.manifest)
    except ManifestError as exc:
        fail(str(exc))
    require_mode(result, {"schedule"}, "schedule")
    completed = subprocess.run(uploader_command(result, commit=True))
    raise SystemExit(completed.returncode)


def legacy_upload_command(args: argparse.Namespace) -> None:
    if not args.commit:
        fail("Refusing to upload without --commit.", required_flag="--commit")
    try:
        result = validate_manifest(args.manifest)
    except ManifestError as exc:
        fail(str(exc))
    require_mode(result, {"publish", "schedule"}, "upload")
    completed = subprocess.run(uploader_command(result, commit=True))
    raise SystemExit(completed.returncode)


def save_draft(result: dict[str, Any], output_value: str | None) -> None:
    manifest_path = Path(result["manifest_path"])
    output = Path(output_value).expanduser().resolve() if output_value else manifest_path.with_suffix(".draft.json")
    if output == manifest_path:
        fail("Draft receipt must not overwrite the source manifest.")
    output.parent.mkdir(parents=True, exist_ok=True)
    receipt = {
        "status": "DRAFT_SAVED",
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "source_manifest": str(manifest_path),
        "content_fingerprint": result["content_fingerprint"],
        "payload": result["manifest"],
    }
    temp = output.with_suffix(output.suffix + ".tmp")
    temp.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(output)
    emit(
        "draft_saved",
        status="DRAFT_SAVED",
        draft=str(output),
        source_manifest=str(manifest_path),
        content_fingerprint=result["content_fingerprint"],
        youtube_contacted=False,
    )


def draft_command(args: argparse.Namespace) -> None:
    if not args.commit:
        fail("Refusing to save a draft without --commit.", required_flag="--commit")
    try:
        result = validate_manifest(args.manifest)
    except ManifestError as exc:
        fail(str(exc))
    require_mode(result, {"draft"}, "draft")
    save_draft(result, args.output)


def dispatch_command(args: argparse.Namespace) -> None:
    if not args.commit:
        fail("Refusing to execute a dispatch decision without --commit.", required_flag="--commit")
    try:
        result = validate_manifest(args.manifest)
    except ManifestError as exc:
        fail(str(exc))
    if result["manifest"]["mode"] != args.decision:
        fail(
            "Dispatch decision does not match the manifest mode.",
            error="DECISION_MODE_MISMATCH",
            decision=args.decision,
            manifest_mode=result["manifest"]["mode"],
        )
    if args.decision == "draft":
        save_draft(result, args.output)
        return
    completed = subprocess.run(uploader_command(result, commit=True))
    raise SystemExit(completed.returncode)


def dry_run_command(args: argparse.Namespace) -> None:
    try:
        result = validate_manifest(args.manifest)
    except ManifestError as exc:
        fail(str(exc))
    completed = subprocess.run(uploader_command(result, commit=False))
    raise SystemExit(completed.returncode)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Manifest-first YouTube publisher")
    sub = root.add_subparsers(dest="command", required=True)
    for name, handler in (("validate", validate_command), ("dry-run", dry_run_command)):
        item = sub.add_parser(name)
        item.add_argument("--manifest", required=True)
        item.set_defaults(func=handler)
    for name, handler in (
        ("publish", publish_command),
        ("schedule", schedule_command),
        ("upload", legacy_upload_command),
    ):
        item = sub.add_parser(name)
        item.add_argument("--manifest", required=True)
        item.add_argument("--commit", action="store_true")
        item.set_defaults(func=handler)
    draft = sub.add_parser("draft")
    draft.add_argument("--manifest", required=True)
    draft.add_argument("--output")
    draft.add_argument("--commit", action="store_true")
    draft.set_defaults(func=draft_command)
    dispatch = sub.add_parser("dispatch")
    dispatch.add_argument("--decision", choices=("draft", "publish", "schedule"), required=True)
    dispatch.add_argument("--manifest", required=True)
    dispatch.add_argument("--output")
    dispatch.add_argument("--commit", action="store_true")
    dispatch.set_defaults(func=dispatch_command)
    return root


def main() -> None:
    args = parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
