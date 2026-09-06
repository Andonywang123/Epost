#!/usr/bin/env python3
"""Official YouTube uploader for Epost.

Credentials are kept in the OS keychain. Job metadata and resumable upload URIs
are kept in a small local state file so a retry with the same job ID is safe.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
import os
import random
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


KEYRING_SERVICE = "epost-youtube"
SCOPES = (
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.force-ssl",
)
RETRIABLE_STATUS = {429, 500, 502, 503, 504}
REGION_TIMEZONES = {
    "china": "Asia/Shanghai",
    "us": "America/Los_Angeles",
    "uk-europe": "Europe/London",
    "australia": "Australia/Sydney",
}
CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")


def emit(event: str, **data: Any) -> None:
    print(json.dumps({"event": event, **data}, ensure_ascii=False), flush=True)


def fail(message: str, code: int = 2, **data: Any) -> None:
    emit("error", message=message, **data)
    raise SystemExit(code)


def google_modules() -> dict[str, Any]:
    try:
        import keyring
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaFileUpload
    except ImportError as exc:
        fail(
            "Missing dependency. Install scripts/requirements.txt before using the skill.",
            detail=str(exc),
        )
    return {
        "keyring": keyring,
        "Request": Request,
        "Credentials": Credentials,
        "InstalledAppFlow": InstalledAppFlow,
        "build": build,
        "HttpError": HttpError,
        "MediaFileUpload": MediaFileUpload,
    }


def state_path(profile: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", profile):
        fail("profile must contain only letters, numbers, dot, underscore, or hyphen.")
    root = Path(os.environ.get("EPOST_STATE_DIR", "~/.local/state/epost")).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    return root / f"youtube-{profile}.json"


def load_state(profile: str) -> dict[str, Any]:
    path = state_path(profile)
    if not path.exists():
        return {"jobs": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail("Cannot read YouTube job state.", detail=str(exc), path=str(path))
    data.setdefault("jobs", {})
    return data


def save_state(profile: str, data: dict[str, Any]) -> None:
    path = state_path(profile)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def merge_youtube_tags(*groups: list[Any], limit: int = 15) -> list[str]:
    tags: list[str] = []
    seen: set[str] = set()
    for value in [item for group in groups for item in group]:
        tag = re.sub(r"\s+", " ", str(value)).strip().lstrip("#").strip()
        if not tag or len(tag) > 40 or CJK_RE.search(tag):
            continue
        key = tag.casefold()
        if key in seen:
            continue
        seen.add(key)
        tags.append(tag)
        if len(tags) >= limit:
            break
    while sum(len(tag) for tag in tags) + max(0, len(tags) - 1) > 450:
        tags.pop()
    return tags


def tag_tokens(value: str) -> set[str]:
    stop = {"the", "and", "for", "with", "from", "that", "this", "your", "you", "how", "video"}
    return {token for token in re.findall(r"[a-z0-9]{2,}", value.casefold()) if token not in stop}


def related_popular_tags(yt: Any, title: str, description: str, region_code: str,
                         category_id: str, max_results: int = 25) -> list[str]:
    region = region_code.upper()
    if not re.fullmatch(r"[A-Z]{2}", region):
        raise ValueError("tag region must be a two-letter ISO country code")
    published_after = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat().replace("+00:00", "Z")
    query = re.sub(r"\s+", " ", title).strip()[:160]
    if not query:
        return []
    search_args: dict[str, Any] = {
        "part": "id",
        "q": query,
        "type": "video",
        "order": "viewCount",
        "regionCode": region,
        "publishedAfter": published_after,
        "maxResults": max(1, min(25, max_results)),
    }
    if category_id and category_id != "0":
        search_args["videoCategoryId"] = category_id
    response = yt.search().list(**search_args).execute()
    video_ids = [item.get("id", {}).get("videoId") for item in response.get("items", [])]
    video_ids = [video_id for video_id in video_ids if video_id]
    if not video_ids:
        return []
    videos = yt.videos().list(
        part="snippet,statistics",
        id=",".join(video_ids),
        maxResults=len(video_ids),
    ).execute().get("items", [])
    context_tokens = tag_tokens(f"{title} {description}")
    scores: Counter[str] = Counter()
    frequencies: Counter[str] = Counter()
    display: dict[str, str] = {}
    relevance: dict[str, bool] = {}
    for video in videos:
        snippet = video.get("snippet", {})
        try:
            views = int(video.get("statistics", {}).get("viewCount", 0))
        except (TypeError, ValueError):
            views = 0
        weight = max(1.0, math.log10(views + 10))
        for raw_tag in snippet.get("tags", []) or []:
            normalized = merge_youtube_tags([raw_tag], limit=1)
            if not normalized:
                continue
            tag = normalized[0]
            key = tag.casefold()
            display.setdefault(key, tag)
            frequencies[key] += 1
            overlap = bool(tag_tokens(tag) & context_tokens)
            relevance[key] = relevance.get(key, False) or overlap
            scores[key] += weight + (3 if overlap else 0)
    eligible = [key for key in scores if relevance.get(key, False)]
    eligible.sort(key=lambda key: (scores[key] + frequencies[key] * 1.5, frequencies[key]), reverse=True)
    return merge_youtube_tags([display[key] for key in eligible], limit=15)


def parse_bool(value: str) -> bool:
    lowered = value.lower()
    if lowered in {"true", "1", "yes"}:
        return True
    if lowered in {"false", "0", "no"}:
        return False
    raise argparse.ArgumentTypeError("expected true or false")


def parse_publish_at(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("publish-at must be ISO 8601") from exc
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("publish-at must include a timezone")
    if parsed.astimezone(timezone.utc) <= datetime.now(timezone.utc):
        raise argparse.ArgumentTypeError("publish-at must be in the future")
    return parsed.isoformat()


def schedule_from_slider(args: argparse.Namespace) -> str | None:
    supplied = [args.schedule_region is not None, args.schedule_date is not None,
                args.schedule_minute is not None]
    if not any(supplied):
        return parse_publish_at(args.publish_at)
    if args.publish_at:
        fail("Use either publish-at or the region/date/24-hour slider fields, not both.")
    if not all(supplied):
        fail("schedule-region, schedule-date and schedule-minute must be supplied together.")
    if not 0 <= args.schedule_minute <= 1439:
        fail("schedule-minute must be between 0 and 1439.")
    timezone_name = REGION_TIMEZONES.get(args.schedule_region)
    if args.schedule_region == "local":
        timezone_name = args.local_timezone
        if not timezone_name:
            fail("local-timezone is required for the local schedule region.")
    try:
        zone = ZoneInfo(str(timezone_name))
        date_value = datetime.strptime(args.schedule_date, "%Y-%m-%d")
    except (ZoneInfoNotFoundError, ValueError) as exc:
        fail("Invalid schedule date or IANA timezone.", detail=str(exc))
    local = date_value.replace(
        hour=args.schedule_minute // 60,
        minute=args.schedule_minute % 60,
        second=0,
        microsecond=0,
        tzinfo=zone,
    )
    round_trip = local.astimezone(timezone.utc).astimezone(zone)
    if round_trip.replace(tzinfo=None) != local.replace(tzinfo=None):
        fail("The selected local time does not exist because of daylight-saving time.")
    return parse_publish_at(local.isoformat())


def schedule_context(args: argparse.Namespace, publish_at: str | None) -> dict[str, Any] | None:
    if not publish_at:
        return None
    if args.schedule_region:
        timezone_name = (args.local_timezone if args.schedule_region == "local"
                         else REGION_TIMEZONES[args.schedule_region])
        return {
            "region": args.schedule_region,
            "timezone": timezone_name,
            "local_date": args.schedule_date,
            "minute": args.schedule_minute,
            "publish_at": publish_at,
        }
    return {"region": "custom", "timezone": None, "local_date": None,
            "minute": None, "publish_at": publish_at}


def apply_localization_manifest(args: argparse.Namespace) -> None:
    if not args.localization_manifest:
        if not args.video or not args.title:
            fail("video and title are required when no localization manifest is supplied.")
        return
    path = Path(args.localization_manifest).expanduser().resolve()
    if not path.is_file():
        fail("Localization manifest does not exist.", path=str(path))
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail("Cannot read localization manifest.", detail=str(exc))
    if manifest.get("status") != "READY":
        fail("Localization manifest is not READY.", status=manifest.get("status"))
    args.video = manifest.get("localized_video")
    args.title = manifest.get("english_title")
    args.description = manifest.get("english_description", "")
    localized_tags = manifest.get("english_tags", [])
    if localized_tags is not None and not isinstance(localized_tags, list):
        fail("Localization manifest english_tags must be an array.")
    args.tag = merge_youtube_tags(list(localized_tags or []), list(args.tag or []))
    args.thumbnail = manifest.get("localized_thumbnail") or args.thumbnail
    args.caption = manifest.get("english_caption")
    args.caption_language = "en-GB"
    args.caption_name = args.caption_name or "English"
    args.default_language = "en-GB"
    for field in ("video", "title", "caption"):
        if not getattr(args, field, None):
            fail("Localization manifest is missing a required field.", field=field)
    localized_video = Path(args.video).expanduser().resolve()
    expected_hash = manifest.get("localized_video_sha256")
    if expected_hash and localized_video.is_file() and sha256(localized_video) != expected_hash:
        fail("Localized video changed after generation; regenerate the platform variant.")


def store_credentials(profile: str, creds: Any, modules: dict[str, Any]) -> None:
    modules["keyring"].set_password(KEYRING_SERVICE, profile, creds.to_json())


def load_credentials(profile: str, modules: dict[str, Any]) -> Any:
    raw = modules["keyring"].get_password(KEYRING_SERVICE, profile)
    if not raw:
        fail("No stored YouTube authorization. Run the auth command first.", profile=profile)
    creds = modules["Credentials"].from_authorized_user_info(json.loads(raw), SCOPES)
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(modules["Request"]())
            store_credentials(profile, creds, modules)
        except Exception as exc:
            fail("YouTube authorization can no longer be refreshed.", detail=str(exc), reconnect=True)
    if not creds.valid:
        fail("Stored YouTube authorization is invalid.", reconnect=True)
    return creds


def service(profile: str, modules: dict[str, Any]) -> Any:
    creds = load_credentials(profile, modules)
    return modules["build"]("youtube", "v3", credentials=creds, cache_discovery=False)


def auth_command(args: argparse.Namespace) -> None:
    modules = google_modules()
    path = Path(args.client_secrets).expanduser().resolve()
    if not path.is_file():
        fail("OAuth client secrets file does not exist.", path=str(path))
    try:
        client_config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail("Cannot read OAuth client secrets JSON.", detail=str(exc), path=str(path))
    if "installed" not in client_config:
        fail(
            "OAuth client must be a Google Desktop app client (JSON root key: installed).",
            detected_type="web" if "web" in client_config else "unknown",
        )
    flow = modules["InstalledAppFlow"].from_client_secrets_file(str(path), SCOPES)
    creds = flow.run_local_server(
        port=0,
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
        open_browser=True,
    )
    if not creds.refresh_token:
        fail("Google did not return a refresh token. Revoke the old grant and authorize again.")
    store_credentials(args.profile, creds, modules)
    emit("authorized", profile=args.profile, scopes=list(creds.scopes or SCOPES))


def disconnect_command(args: argparse.Namespace) -> None:
    modules = google_modules()
    try:
        modules["keyring"].delete_password(KEYRING_SERVICE, args.profile)
    except Exception:
        pass
    emit("disconnected", profile=args.profile)


def preflight_command(args: argparse.Namespace) -> None:
    modules = google_modules()
    yt = service(args.profile, modules)
    response = yt.channels().list(part="id,snippet,status", mine=True).execute()
    items = response.get("items", [])
    if not items:
        fail("The authorized Google account has no YouTube channel.")
    channel = items[0]
    emit(
        "preflight_ok",
        profile=args.profile,
        channel_id=channel.get("id"),
        channel_title=channel.get("snippet", {}).get("title"),
        public_upload_requires_api_audit=True,
    )


def build_body(args: argparse.Namespace) -> dict[str, Any]:
    publish_at = schedule_from_slider(args)
    if publish_at and args.privacy != "private":
        fail("Scheduled videos must use privacy=private until publishAt.")
    snippet: dict[str, Any] = {
        "title": args.title,
        "description": args.description,
        "categoryId": args.category_id,
    }
    if args.tag:
        snippet["tags"] = args.tag
    if args.default_language:
        snippet["defaultLanguage"] = args.default_language
    status: dict[str, Any] = {
        "privacyStatus": args.privacy,
        "selfDeclaredMadeForKids": args.made_for_kids,
    }
    if publish_at:
        status["publishAt"] = publish_at
    if args.contains_synthetic_media is not None:
        status["containsSyntheticMedia"] = args.contains_synthetic_media
    return {"snippet": snippet, "status": status}


def validate_files(args: argparse.Namespace) -> tuple[Path, Path | None, Path | None]:
    video = Path(args.video).expanduser().resolve()
    if not video.is_file():
        fail("Video file does not exist.", path=str(video))
    thumbnail = Path(args.thumbnail).expanduser().resolve() if args.thumbnail else None
    caption = Path(args.caption).expanduser().resolve() if args.caption else None
    for label, item in (("thumbnail", thumbnail), ("caption", caption)):
        if item and not item.is_file():
            fail(f"{label} file does not exist.", path=str(item))
    if caption and not args.caption_language:
        fail("caption-language is required when a caption file is supplied.")
    return video, thumbnail, caption


def execute_with_retry(call: Any, http_error: Any, attempts: int = 5) -> Any:
    for attempt in range(attempts):
        try:
            return call.execute()
        except http_error as exc:
            status = getattr(exc.resp, "status", None)
            if status not in RETRIABLE_STATUS or attempt == attempts - 1:
                raise
        except OSError:
            if attempt == attempts - 1:
                raise
        delay = min(32, 2**attempt) + random.random()
        emit("retrying", attempt=attempt + 1, delay_seconds=round(delay, 2))
        time.sleep(delay)


def upload_video(args: argparse.Namespace, yt: Any, modules: dict[str, Any], video: Path,
                 body: dict[str, Any], job: dict[str, Any], all_state: dict[str, Any]) -> str:
    media = modules["MediaFileUpload"](
        str(video), mimetype="video/*", chunksize=args.chunk_size_mb * 1024 * 1024, resumable=True
    )
    request = yt.videos().insert(
        part="snippet,status",
        body=body,
        notifySubscribers=args.notify_subscribers,
        media_body=media,
    )
    if job.get("resumable_uri"):
        request.resumable_uri = job["resumable_uri"]
    response = None
    failures = 0
    while response is None:
        try:
            progress, response = request.next_chunk()
            if getattr(request, "resumable_uri", None):
                job["resumable_uri"] = request.resumable_uri
                save_state(args.profile, all_state)
            if progress:
                emit("upload_progress", fraction=round(progress.progress(), 4))
        except modules["HttpError"] as exc:
            status = getattr(exc.resp, "status", None)
            if status not in RETRIABLE_STATUS or failures >= args.max_retries:
                raise
            failures += 1
            delay = min(32, 2**failures) + random.random()
            emit("upload_retry", status=status, attempt=failures, delay_seconds=round(delay, 2))
            time.sleep(delay)
        except OSError as exc:
            if failures >= args.max_retries:
                raise
            failures += 1
            delay = min(32, 2**failures) + random.random()
            emit("upload_retry", detail=str(exc), attempt=failures, delay_seconds=round(delay, 2))
            time.sleep(delay)
    video_id = response["id"]
    job.update({"video_id": video_id, "status": "uploaded", "resumable_uri": None})
    save_state(args.profile, all_state)
    return video_id


def set_thumbnail(yt: Any, modules: dict[str, Any], video_id: str, thumbnail: Path) -> None:
    call = yt.thumbnails().set(
        videoId=video_id,
        media_body=modules["MediaFileUpload"](str(thumbnail), resumable=False),
    )
    execute_with_retry(call, modules["HttpError"])


def insert_caption(args: argparse.Namespace, yt: Any, modules: dict[str, Any],
                   video_id: str, caption: Path) -> None:
    body = {
        "snippet": {
            "videoId": video_id,
            "language": args.caption_language,
            "name": args.caption_name or args.caption_language,
            "isDraft": False,
        }
    }
    call = yt.captions().insert(
        part="snippet",
        body=body,
        media_body=modules["MediaFileUpload"](str(caption), resumable=False),
    )
    execute_with_retry(call, modules["HttpError"])


def poll_processing(args: argparse.Namespace, yt: Any, video_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + args.poll_timeout
    latest: dict[str, Any] = {}
    while time.monotonic() < deadline:
        response = yt.videos().list(part="status,processingDetails", id=video_id).execute()
        items = response.get("items", [])
        if not items:
            fail("Uploaded video disappeared while polling.", video_id=video_id)
        latest = items[0]
        upload_status = latest.get("status", {}).get("uploadStatus")
        processing_status = latest.get("processingDetails", {}).get("processingStatus")
        emit("processing", upload_status=upload_status, processing_status=processing_status)
        if upload_status in {"failed", "rejected", "deleted"} or processing_status in {"failed", "terminated"}:
            return latest
        if upload_status == "processed" or processing_status == "succeeded":
            return latest
        time.sleep(args.poll_interval)
    emit("processing_timeout", video_id=video_id)
    return latest


def publish_command(args: argparse.Namespace) -> None:
    apply_localization_manifest(args)
    video, thumbnail, caption = validate_files(args)
    args.tag = merge_youtube_tags(list(args.tag or []))
    body = build_body(args)
    fingerprint = sha256(video)
    if args.dry_run:
        emit(
            "dry_run_ok",
            body=body,
            video=str(video),
            video_sha256=fingerprint,
            thumbnail=str(thumbnail) if thumbnail else None,
            caption=str(caption) if caption else None,
            schedule=schedule_context(args, body["status"].get("publishAt")),
        )
        return

    if not args.commit:
        fail(
            "Refusing to upload without --commit. Validation and dry-run never contact YouTube.",
            required_flag="--commit",
        )

    modules = google_modules()
    yt = service(args.profile, modules)
    all_state = load_state(args.profile)
    jobs = all_state["jobs"]
    existing_job = jobs.get(args.job_id)
    if existing_job and existing_job.get("video_sha256") != fingerprint:
        fail("The job ID is already associated with a different video.", job_id=args.job_id)

    frozen_tags = existing_job.get("resolved_tags") if existing_job else None
    if isinstance(frozen_tags, list):
        args.tag = merge_youtube_tags(frozen_tags)
        emit("tag_recommendation_reused", tags=args.tag)
    elif args.auto_tags and not (existing_job or {}).get("video_id"):
        try:
            popular_tags = related_popular_tags(
                yt, args.title, args.description, args.tag_region, args.category_id
            )
            args.tag = merge_youtube_tags(args.tag, popular_tags)
            emit(
                "tag_recommendation",
                source="related_recent_high_view_videos",
                region=args.tag_region.upper(),
                tags=args.tag,
            )
        except Exception as exc:
            emit(
                "tag_recommendation_skipped",
                reason=str(exc),
                fallback_tags=args.tag,
            )
    body = build_body(args)
    request_fingerprint = hashlib.sha256(
        (fingerprint + json.dumps(body, ensure_ascii=False, sort_keys=True)).encode("utf-8")
    ).hexdigest()
    job = jobs.setdefault(args.job_id, {
        "status": "new",
        "video_sha256": fingerprint,
        "request_fingerprint": request_fingerprint,
    })
    job["schedule"] = schedule_context(args, body["status"].get("publishAt"))
    if job.get("request_fingerprint") not in {None, request_fingerprint}:
        fail("The job ID is already associated with different metadata or visibility.", job_id=args.job_id)
    job["request_fingerprint"] = request_fingerprint
    job["resolved_tags"] = args.tag
    save_state(args.profile, all_state)
    if job.get("video_id"):
        emit("idempotent_reuse", job_id=args.job_id, video_id=job["video_id"])
        video_id = job["video_id"]
    else:
        emit("upload_started", job_id=args.job_id, bytes=video.stat().st_size)
        try:
            video_id = upload_video(args, yt, modules, video, body, job, all_state)
        except modules["HttpError"] as exc:
            fail("YouTube rejected the upload.", status=getattr(exc.resp, "status", None), detail=str(exc))

    partial_errors: list[dict[str, Any]] = []
    if thumbnail and not job.get("thumbnail_done"):
        try:
            set_thumbnail(yt, modules, video_id, thumbnail)
            job["thumbnail_done"] = True
            save_state(args.profile, all_state)
        except Exception as exc:
            partial_errors.append({"step": "thumbnail", "detail": str(exc)})
    if caption and not job.get("caption_done"):
        try:
            insert_caption(args, yt, modules, video_id, caption)
            job["caption_done"] = True
            save_state(args.profile, all_state)
        except Exception as exc:
            partial_errors.append({"step": "caption", "detail": str(exc)})

    processing = poll_processing(args, yt, video_id) if args.poll else None
    job["status"] = "partial_success" if partial_errors else "accepted"
    save_state(args.profile, all_state)
    emit(
        job["status"],
        job_id=args.job_id,
        video_id=video_id,
        url=f"https://youtu.be/{video_id}",
        requested_privacy=body["status"]["privacyStatus"],
        requested_publish_at=body["status"].get("publishAt"),
        schedule=job.get("schedule"),
        partial_errors=partial_errors,
        processing=processing,
    )


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Epost YouTube official API publisher")
    sub = root.add_subparsers(dest="command", required=True)

    auth = sub.add_parser("auth", help="perform one-time OAuth authorization")
    auth.add_argument("--client-secrets", required=True)
    auth.add_argument("--profile", required=True)
    auth.set_defaults(func=auth_command)

    disconnect = sub.add_parser("disconnect", help="remove the locally stored authorization")
    disconnect.add_argument("--profile", required=True)
    disconnect.set_defaults(func=disconnect_command)

    preflight = sub.add_parser("preflight", help="verify stored authorization and channel access")
    preflight.add_argument("--profile", required=True)
    preflight.set_defaults(func=preflight_command)

    publish = sub.add_parser("publish", help="upload and optionally schedule a video")
    publish.add_argument("--profile", required=True)
    publish.add_argument("--job-id", required=True)
    publish.add_argument("--video")
    publish.add_argument("--title")
    publish.add_argument("--description", default="")
    publish.add_argument("--tag", action="append", default=[])
    publish.add_argument("--auto-tags", action=argparse.BooleanOptionalAction, default=True)
    publish.add_argument("--tag-region", default="US")
    publish.add_argument("--category-id", default="22")
    publish.add_argument("--default-language", default="en-GB")
    publish.add_argument("--privacy", choices=("private", "unlisted", "public"), required=True)
    publish.add_argument("--publish-at")
    publish.add_argument("--schedule-region", choices=("china", "us", "uk-europe", "australia", "local"))
    publish.add_argument("--schedule-date", help="target-region calendar date in YYYY-MM-DD")
    publish.add_argument("--schedule-minute", type=int, help="24-hour slider value from 0 to 1439")
    publish.add_argument("--local-timezone", help="IANA timezone used when schedule-region=local")
    publish.add_argument("--made-for-kids", type=parse_bool, required=True)
    publish.add_argument("--contains-synthetic-media", type=parse_bool)
    publish.add_argument("--notify-subscribers", action="store_true")
    publish.add_argument("--thumbnail")
    publish.add_argument("--caption")
    publish.add_argument("--caption-language")
    publish.add_argument("--caption-name")
    publish.add_argument("--localization-manifest",
                         help="READY manifest produced by youtube_localize.py")
    publish.add_argument("--chunk-size-mb", type=int, default=8)
    publish.add_argument("--max-retries", type=int, default=5)
    publish.add_argument("--poll", action="store_true")
    publish.add_argument("--poll-timeout", type=int, default=900)
    publish.add_argument("--poll-interval", type=int, default=10)
    publish.add_argument("--dry-run", action="store_true")
    publish.add_argument(
        "--commit",
        action="store_true",
        help="required acknowledgement for the external video upload",
    )
    publish.set_defaults(func=publish_command)
    return root


def main() -> None:
    args = parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
