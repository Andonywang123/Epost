#!/usr/bin/env python3
"""Validate and normalize an Epost Xiaohongshu image-post manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
VISIBILITY = {
    "公开": "公开",
    "公开可见": "公开",
    "私密": "私密",
    "仅自己可见": "私密",
    "仅互关好友可见": "仅互关好友可见",
    "部分可见": "部分可见",
    "不给谁看": "不给谁看",
}


def fail(message: str, **data: Any) -> None:
    print(json.dumps({"ok": False, "error": message, **data}, ensure_ascii=False))
    raise SystemExit(2)


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        fail("manifest_not_found", path=str(path))
    except json.JSONDecodeError as exc:
        fail("invalid_json", detail=str(exc))
    if not isinstance(value, dict):
        fail("manifest_must_be_object")
    return value


def required_text(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        fail("required_text_missing", field=key)
    return value.strip()


def schedule_value(data: dict[str, Any], min_lead_minutes: int) -> str | None:
    mode = data["mode"]
    raw = data.get("schedule_at")
    if mode != "schedule":
        return None
    if not isinstance(raw, str) or not raw:
        fail("schedule_at_required")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        fail("schedule_at_must_be_iso8601")
    if parsed.tzinfo is None:
        fail("schedule_at_requires_timezone")
    lead = (parsed.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds() / 60
    if lead < min_lead_minutes:
        fail("schedule_lead_time_too_short", required_minutes=min_lead_minutes, actual_minutes=round(lead, 1))
    return parsed.isoformat()


def file_digest(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.name.encode("utf-8"))
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def validate(data: dict[str, Any], min_lead_minutes: int) -> dict[str, Any]:
    job_id = required_text(data, "job_id")
    account_profile = required_text(data, "account_profile")
    title = required_text(data, "title")
    body = data.get("body", "")
    if not isinstance(body, str):
        fail("body_must_be_string")
    if len(title) > 20:
        fail("title_too_long", maximum=20, actual=len(title))
    if len(body) > 1000:
        fail("body_too_long", maximum=1000, actual=len(body))

    mode = data.get("mode")
    if mode not in {"draft", "publish", "schedule"}:
        fail("invalid_mode", allowed=["draft", "publish", "schedule"])
    data["mode"] = mode

    raw_images = data.get("images")
    if not isinstance(raw_images, list) or not 1 <= len(raw_images) <= 18:
        fail("invalid_image_count", minimum=1, maximum=18)
    images: list[Path] = []
    for raw in raw_images:
        if not isinstance(raw, str) or not os.path.isabs(raw):
            fail("image_path_must_be_absolute", path=raw)
        path = Path(raw).expanduser().resolve()
        if not path.is_file():
            fail("image_not_found", path=str(path))
        if path.suffix.lower() not in IMAGE_EXTENSIONS:
            fail("unsupported_image_format", path=str(path), allowed=sorted(IMAGE_EXTENSIONS))
        if path.stat().st_size > 32 * 1024 * 1024:
            fail("image_too_large", path=str(path), maximum_mb=32)
        images.append(path)

    raw_tags = data.get("tags", [])
    if not isinstance(raw_tags, list) or any(not isinstance(tag, str) for tag in raw_tags):
        fail("tags_must_be_string_array")
    tags = []
    for tag in raw_tags:
        clean = tag.strip().lstrip("#")
        if clean and clean not in tags:
            tags.append(clean)
    hot_tag = data.get("hot_tag")
    if hot_tag is not None:
        if not isinstance(hot_tag, str):
            fail("hot_tag_must_be_string")
        hot_tag = hot_tag.strip().lstrip("#") or None

    auto_tags = data.get("auto_tags", True)
    if not isinstance(auto_tags, bool):
        fail("auto_tags_must_be_boolean")
    tag_limit = data.get("tag_limit", 8)
    if not isinstance(tag_limit, int) or isinstance(tag_limit, bool) or not 1 <= tag_limit <= 10:
        fail("tag_limit_out_of_range", minimum=1, maximum=10)

    visibility = data.get("visibility", "公开")
    if visibility not in VISIBILITY:
        fail("unsupported_visibility", allowed=sorted(VISIBILITY.keys()), actual=visibility)
    visibility = VISIBILITY[visibility]

    activity = data.get("activity")
    if activity is not None:
        if not isinstance(activity, str):
            fail("activity_must_be_string_or_null")
        activity = activity.strip() or None
    activity_mode = data.get("activity_mode")
    if activity_mode is None:
        activity_mode = "exact" if activity else "auto"
    if activity_mode not in {"auto", "exact", "none"}:
        fail("invalid_activity_mode", allowed=["auto", "exact", "none"])
    if activity_mode == "exact" and not activity:
        fail("activity_required_for_exact_mode")
    if activity_mode == "none" and activity:
        fail("activity_must_be_null_for_none_mode")

    normalized = {
        "job_id": job_id,
        "account_profile": account_profile,
        "mode": mode,
        "title": title,
        "body": body,
        "images": [str(path) for path in images],
        "tags": tags,
        "hot_tag": hot_tag,
        "auto_tags": auto_tags,
        "tag_limit": tag_limit,
        "visibility": visibility,
        "activity": activity,
        "activity_mode": activity_mode,
        "schedule_at": schedule_value(data, min_lead_minutes),
    }
    logical = json.dumps(normalized, ensure_ascii=False, sort_keys=True).encode("utf-8")
    normalized["content_fingerprint"] = hashlib.sha256(logical + file_digest(images).encode()).hexdigest()
    return normalized


def command_validate(args: argparse.Namespace) -> None:
    manifest = load_manifest(Path(args.manifest).expanduser().resolve())
    normalized = validate(manifest, args.min_lead_minutes)
    result = json.dumps({"ok": True, "payload": normalized}, ensure_ascii=False, indent=2)
    if args.output:
        output = Path(args.output).expanduser().resolve()
        output.write_text(result + "\n", encoding="utf-8")
    print(result)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Validate an Epost Xiaohongshu image-post manifest")
    sub = root.add_subparsers(dest="command", required=True)
    validate_parser = sub.add_parser("validate")
    validate_parser.add_argument("--manifest", required=True)
    validate_parser.add_argument("--output")
    validate_parser.add_argument("--min-lead-minutes", type=int, default=60)
    validate_parser.set_defaults(func=command_validate)
    return root


def main() -> None:
    args = parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
