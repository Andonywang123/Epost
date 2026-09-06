#!/usr/bin/env python3
"""YouTube-owned execution entry for an Agent's frozen, approved JSON task.

This is a local trusted-Agent interface, not a public endpoint. Matching receipt
fields prevent accidental misrouting; they are NOT a signature or new permission.
The calling Agent must first verify the actual persisted dashboard approval and
execution history. This module deliberately imports no coordinator code.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


SKILL_ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "youtube-publish"


class Stop(Exception):
    def __init__(self, status: str, message: str, outcome: str = "needs_user"):
        self.status, self.message, self.outcome = status, message, outcome


def digest(value: Any) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def events(text: str) -> list[dict[str, Any]]:
    decoder, result, cursor = json.JSONDecoder(), [], 0
    while cursor < len(text):
        while cursor < len(text) and text[cursor].isspace():
            cursor += 1
        if cursor == len(text):
            break
        try:
            value, cursor = decoder.raw_decode(text, cursor)
        except ValueError as exc:
            raise Stop("UNREADABLE_RESULT", "YouTube 发布包返回无法解析的结果，需核实，不能自动重试。", "unknown") from exc
        if isinstance(value, dict):
            result.append(value)
    if not result:
        raise Stop("EMPTY_RESULT", "YouTube 发布包未返回结果，请人工核实。", "unknown")
    return result


def invoke(argv: list[str], *, effect: bool = False, timeout: int = 7200) -> dict[str, Any]:
    try:
        completed = subprocess.run(argv, shell=False, capture_output=True, text=True,
                                   encoding="utf-8", timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise Stop("EXECUTION_TIMEOUT", "YouTube 执行超时；先核实当前任务，禁止自动重发。",
                   "unknown" if effect else "needs_user") from exc
    except OSError as exc:
        raise Stop("RUNTIME_UNAVAILABLE", "YouTube 发布包运行环境不可用，请配置对应运行时。") from exc
    try:
        value = events(completed.stdout)[-1]
    except Stop as exc:
        if not effect:
            exc.outcome = "needs_user"
        raise
    if completed.returncode:
        raise Stop(str(value.get("status") or value.get("event") or "PREPARATION_FAILED"),
                   "YouTube 执行返回错误，请核实结果；不会自动重试。" if effect else
                   "YouTube 准备未完成，请检查本发布包配置；未执行最终分发。",
                   "unknown" if effect else "needs_user")
    return value


def require_file(value: Any) -> Path:
    if not isinstance(value, str):
        raise Stop("SOURCE_MISSING", "YouTube 素材文件缺失。")
    path = Path(value)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise Stop("SOURCE_MISSING", "YouTube 素材路径必须是已储存的本地普通文件。")
    return path


def verify_source(request: dict[str, Any]) -> None:
    source, receipt = request["source"], request["authorization"]
    if digest(source) != receipt.get("sourceHash"):
        raise Stop("SOURCE_CHANGED", "YouTube 收到的素材清单与确认回执不一致。")
    records = list(source["media"]["files"])
    if source["metadata"].get("cover"):
        records.append(source["metadata"]["cover"])
    for record in records:
        if not isinstance(record, dict) or type(record.get("size")) is not int or record["size"] < 1:
            raise Stop("SOURCE_CHANGED", "YouTube 素材缺少有效的文件大小记录。")
        if not isinstance(record.get("sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", record["sha256"]):
            raise Stop("SOURCE_CHANGED", "YouTube 素材缺少有效的内容校验记录。")
        path = require_file(record.get("path"))
        hashed, size = hashlib.sha256(), 0
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                size += len(chunk)
                hashed.update(chunk)
        if size != record["size"] or hashed.hexdigest() != record["sha256"]:
            raise Stop("SOURCE_CHANGED", "已确认的 YouTube 视频或封面发生变化，停止执行。")


def check_time(request: dict[str, Any]) -> None:
    if request["decision"] != "schedule":
        if any(request.get(field) is not None for field in ("scheduledAt", "timezone", "scheduleUtc")):
            raise Stop("DECISION_TIME_MISMATCH", "草稿或立即发布不能保留定时时间或时区。")
        return
    try:
        scheduled = datetime.fromisoformat(request["scheduledAt"].replace("Z", "+00:00"))
        utc = datetime.fromisoformat(request["scheduleUtc"].replace("Z", "+00:00"))
        zone = ZoneInfo(request["timezone"])
    except (ValueError, TypeError, KeyError, AttributeError, ZoneInfoNotFoundError) as exc:
        raise Stop("INVALID_TIME", "YouTube 定时必须包含有效时间、IANA 时区和对应 UTC 时间。") from exc
    if scheduled.tzinfo is None or utc.tzinfo is None:
        raise Stop("INVALID_TIME", "YouTube 定时时间必须明确包含时区。")
    if scheduled <= datetime.now(timezone.utc):
        raise Stop("SCHEDULE_EXPIRED", "YouTube 定时时间已过，请返回看板重新确认。")
    if scheduled != utc or scheduled.utcoffset() != scheduled.astimezone(zone).utcoffset():
        raise Stop("TIMEZONE_MISMATCH", "YouTube 已确认的时间、时区与 UTC 时间不一致。")


def validate_request(request: Any) -> None:
    if not isinstance(request, dict) or request.get("platform") != "youtube" or request.get("decision") not in {"draft", "publish", "schedule"}:
        raise Stop("INVALID_REQUEST", "请求必须明确指定 YouTube 及草稿、发布或定时操作。")
    receipt = request.get("authorization")
    if not isinstance(receipt, dict):
        raise Stop("CONFIRMATION_REQUIRED", "YouTube 未收到已确认任务回执。")
    for field in ("sessionId", "planId", "planHash"):
        value = request.get(field)
        if not isinstance(value, str) or not value.strip() or value != receipt.get(field):
            raise Stop("CONFIRMATION_REQUIRED", "YouTube 任务与用户确认回执不匹配。")
    if not re.fullmatch(r"[a-f0-9]{64}", request["planHash"]):
        raise Stop("CONFIRMATION_REQUIRED", "YouTube 任务缺少有效的已确认计划校验值。")
    if (not isinstance(receipt.get("receiptId"), str) or not receipt["receiptId"].strip()
            or receipt.get("confirmationSource") != "dashboard"
            or not isinstance(receipt.get("trustedUserEventId"), str) or not receipt["trustedUserEventId"].strip()
            or not isinstance(receipt.get("confirmedAt"), str) or not receipt["confirmedAt"].strip()):
        raise Stop("CONFIRMATION_REQUIRED", "YouTube 缺少完整的看板确认来源；定位信息不是新授权。")
    job_id = request.get("jobId")
    if not isinstance(job_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", job_id) or job_id in {".", ".."}:
        raise Stop("INVALID_JOB", "YouTube 任务标识不正确。")
    if not isinstance(request.get("accountProfile"), str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", request["accountProfile"]):
        raise Stop("INVALID_ACCOUNT", "YouTube 任务缺少明确的本地账号配置标识。")
    source = request.get("source")
    if not isinstance(source, dict) or type(source.get("assetRevision")) is not int or source["assetRevision"] < 1 or type(receipt.get("assetRevision")) is not int or source["assetRevision"] != receipt["assetRevision"]:
        raise Stop("SOURCE_CHANGED", "YouTube 素材版本与确认回执不一致。")
    media, meta = source.get("media"), source.get("metadata")
    if not isinstance(media, dict) or media.get("kind") != "video":
        raise Stop("UNSUPPORTED_MEDIA", "YouTube 发布包需要一个视频，不会自动把图文变成视频。")
    if not isinstance(media.get("files"), list) or len(media["files"]) != 1:
        raise Stop("INVALID_SOURCE", "YouTube 视频路径一次必须且只能包含一个主视频。")
    if not isinstance(meta, dict) or not isinstance(meta.get("title"), str) or not meta["title"].strip() or not isinstance(meta.get("body"), str):
        raise Stop("INVALID_SOURCE", "YouTube 视频缺少已确认的标题或正文。")
    if meta.get("cover") is not None and not isinstance(meta["cover"], dict):
        raise Stop("INVALID_SOURCE", "YouTube 封面必须是已确认的文件记录或明确不提供。")
    if not isinstance(request.get("accountSettings"), dict):
        raise Stop("INVALID_ACCOUNT", "YouTube 缺少已确认的账号设置。")
    account = request["accountSettings"]
    for field in ("made_for_kids", "contains_synthetic_media", "notify_subscribers"):
        if not isinstance(account.get(field), bool):
            raise Stop("DECLARATION_REQUIRED", "YouTube 需要明确受众、合成内容和订阅通知声明，请返回看板确认。")
    if account["made_for_kids"] and account["notify_subscribers"]:
        raise Stop("DECLARATION_CONFLICT", "YouTube 面向儿童的视频不能通知订阅者；请返回看板修改并重新确认。")
    verify_source(request)
    check_time(request)


def write_json(path: Path, value: dict[str, Any]) -> None:
    with path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def youtube(args: argparse.Namespace, request: dict[str, Any], job_dir: Path) -> dict[str, Any]:
    source, account = request["source"], request["accountSettings"]
    for field in ("made_for_kids", "contains_synthetic_media", "notify_subscribers"):
        if not isinstance(account.get(field), bool):
            raise Stop("DECLARATION_REQUIRED", "YouTube 需要明确受众、合成内容和订阅通知声明，请返回看板确认。")
    if account["made_for_kids"] and account["notify_subscribers"]:
        raise Stop("DECLARATION_CONFLICT", "YouTube 面向儿童的视频不能通知订阅者；请返回看板修改并重新确认。")
    if account.get("privacy") not in {"private", "unlisted", "public"}:
        raise Stop("PRIVACY_REQUIRED", "YouTube 需要明确已确认的公开范围。")
    if request["decision"] == "schedule" and account["privacy"] != "public":
        raise Stop("SCHEDULE_VISIBILITY_REQUIRED", "YouTube 定时会到点公开，需要已确认的最终范围为 public。")
    if request["decision"] == "draft" and (request.get("acceptLocalDraft") is not True or request.get("draftScope") != "local"):
        raise Stop("LOCAL_DRAFT_ACK_REQUIRED", "YouTube 只支持本地草稿，需要用户明确接受；不会改为私密上传。")
    python = getattr(args, "python", None) or str(SKILL_ROOT / (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python"))
    video, meta = require_file(source["media"]["files"][0]["path"]), source["metadata"]
    cover = require_file(meta["cover"]["path"]) if meta.get("cover") else None
    provider = account.get("translation_provider", "argos")
    if provider not in {"argos", "openai"}:
        raise Stop("LOCALIZATION_PROVIDER_INVALID", "YouTube 英化服务必须使用本地 Argos 或已配置的 OpenAI。")
    localizer = [str(python), str(SKILL_ROOT / "scripts/youtube_localize.py"),
                 "--translation-provider", provider]
    if provider == "openai":
        env_name = account.get("api_key_env", "OPENAI_API_KEY")
        if (not isinstance(env_name, str)
                or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env_name)
                or not os.environ.get(env_name)):
            raise Stop("LOCALIZATION_KEY_REQUIRED", "YouTube 发布包的 OpenAI 英化凭据未配置；不会使用聊天会员额度。")
        localizer += ["--api-key-env", env_name]
        if account.get("translation_model"):
            localizer += ["--translation-model", str(account["translation_model"])]
    elif account.get("local_asr_model"):
        localizer += ["--local-asr-model", str(account["local_asr_model"])]
    preflight = invoke(localizer + ["preflight"])
    if preflight.get("event") != "localization_preflight_ok":
        raise Stop("LOCALIZATION_PREFLIGHT_REQUIRED", "YouTube 英化环境未就绪，未开始翻译或最终分发。")
    command = localizer + ["localize", "--video", str(video), "--source-title=" + meta["title"],
                           "--source-description=" + meta["body"], "--output-dir", str(job_dir / "youtube-en"),
                           "--locale", account.get("locale", "British English")]
    if cover:
        command += ["--thumbnail", str(cover)]
        if account.get("thumbnail_no_text") is True:
            command += ["--thumbnail-no-text"]
    localized = invoke(command)
    if localized.get("status") != "READY" or not localized.get("manifest"):
        raise Stop("LOCALIZATION_NEEDS_REVIEW", "YouTube 英文文案、字幕或封面尚未准备完成，未上传。")
    data = {"job_id": request["jobId"], "account_profile": request["accountProfile"], "mode": request["decision"],
            "video": str(video), "title": meta["title"], "description": meta["body"], "tags": [],
            "auto_tags": True, "tag_region": account.get("tag_region", "US"),
            "category_id": str(account.get("category_id", "22")), "default_language": "zh-CN",
            "privacy": "private" if request["decision"] == "schedule" else account["privacy"],
            "publish_at": request.get("scheduledAt"), "made_for_kids": account["made_for_kids"],
            "contains_synthetic_media": account["contains_synthetic_media"],
            "notify_subscribers": account["notify_subscribers"], "thumbnail": str(cover) if cover else None,
            "caption": None, "caption_language": None, "caption_name": None,
            "localization_manifest": str(require_file(localized["manifest"])), "poll_processing": False}
    manifest = job_dir / "youtube.json"
    write_json(manifest, data)
    script = SKILL_ROOT / "scripts/youtube_manifest.py"
    dry_run = invoke([str(python), str(script), "dry-run", "--manifest", str(manifest)])
    if dry_run.get("event") != "dry_run_ok":
        raise Stop("PREPARATION_FAILED", "YouTube 发布清单未通过准备检查，未执行最终分发。")
    verify_source(request)
    check_time(request)
    # Only the final upload control changes. Preparation and local drafts retain
    # their original scripts and semantics; API use is now an explicit fallback.
    transport = getattr(args, "transport", "browser")
    if request["decision"] == "draft" or transport == "api":
        command = [str(python), str(script), "dispatch", "--decision", request["decision"],
                   "--manifest", str(manifest), "--commit"]
    else:
        command = [str(python), str(SKILL_ROOT / "scripts/youtube_browser.py"), "dispatch",
                   "--decision", request["decision"], "--manifest", str(manifest)]
        for field in ("browser_config", "cdp_url", "channel_id", "node"):
            value = getattr(args, field, None)
            if value:
                command += ["--" + field.replace("_", "-"), str(value)]
        command.append("--commit")
    result = invoke(command, effect=True)
    if request["decision"] != "draft" and transport == "browser":
        return {"status": result.get("status", "BROWSER_RESULT_UNKNOWN"),
                "outcome": result.get("outcome", "unknown"),
                "message": result.get("message", "请核对 Studio 返回结果，不自动重试。"),
                "url": result.get("url"), "videoId": result.get("video_id"),
                "manifest": str(manifest), "transport": "browser",
                "youtubeContacted": result.get("youtube_contacted"),
                "requestedPublishAt": request.get("scheduledAt"),
                "warnings": result.get("warnings", [])}
    status = result.get("event", result.get("status", "unknown"))
    labels = {"draft_saved": "本地草稿已保存（非 YouTube 草稿箱）",
              "accepted": "YouTube 已接收；尚不代表公开可见", "partial_success": "YouTube 视频已接收，但有子步骤未完成"}
    outcome = "success" if status in {"draft_saved", "accepted"} else "partial" if status == "partial_success" else "unknown"
    if request["decision"] == "draft" and (status != "draft_saved" or result.get("youtube_contacted") is not False):
        outcome = "unknown"
    if request["decision"] != "draft" and status == "draft_saved":
        outcome = "unknown"
    if request["decision"] != "draft" and status in {"accepted", "partial_success"} and not result.get("video_id"):
        outcome = "unknown"
    return {"status": status, "outcome": outcome, "message": labels.get(status, "需人工核实 YouTube 结果，未自动重试。") if outcome != "unknown" else "需人工核实 YouTube 结果，未自动重试。",
            "url": result.get("url"), "videoId": result.get("video_id"), "manifest": str(manifest),
            "draft": result.get("draft"), "requestedPublishAt": result.get("requested_publish_at"),
            "youtubeContacted": result.get("youtube_contacted"), "partialErrors": result.get("partial_errors", [])}


def dispatch_request(args: argparse.Namespace, request: Any) -> dict[str, Any]:
    """Return one receipt; never retry an existing job, including failed attempts."""
    job_dir = None
    try:
        validate_request(request)
        root = Path(args.output_root)
        if not root.is_absolute() or root.is_symlink():
            raise Stop("ADAPTER_NOT_CONFIGURED", "YouTube 发布包需要独立的绝对路径输出目录。")
        root.mkdir(parents=True, exist_ok=True)
        candidate = root / request["jobId"]
        try:
            candidate.mkdir()
        except FileExistsError as exc:
            raise Stop("JOB_ALREADY_ATTEMPTED", "YouTube 该任务已有执行记录，先核实；不会自动重试。", "unknown") from exc
        job_dir = candidate
        write_json(job_dir / "attempt.json", {
            "originSkill": ORIGIN, "jobId": request["jobId"], "sessionId": request["sessionId"],
            "planId": request["planId"], "planHash": request["planHash"],
            "receiptId": request["authorization"]["receiptId"], "requestHash": digest(request),
            "decision": request["decision"], "startedAt": datetime.now(timezone.utc).isoformat()})
        output = youtube(args, request, job_dir)
    except Stop as exc:
        output = {"status": exc.status, "outcome": exc.outcome, "message": exc.message}
    except Exception:
        output = {"status": "PLATFORM_EXCEPTION", "outcome": "unknown", "message": "YouTube 发布包异常，请核实已有操作；不会自动重发。"}
    output["originSkill"] = ORIGIN
    if job_dir is not None:
        try:
            write_json(job_dir / "result.json", output)
        except (OSError, ValueError):
            output.setdefault("warnings", []).append("YouTube 本地回执保存失败，请保留本次返回结果，不要自动重试。")
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description="Agent 触发的 YouTube 发布包入口")
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--python")
    parser.add_argument("--transport", choices=("browser", "api"), default="browser")
    parser.add_argument("--browser-config")
    parser.add_argument("--cdp-url")
    parser.add_argument("--channel-id")
    parser.add_argument("--node")
    args = parser.parse_args()
    try:
        request = json.load(sys.stdin)
    except (ValueError, OSError):
        output = {"originSkill": ORIGIN, "status": "INVALID_REQUEST", "outcome": "needs_user", "message": "YouTube 请求不是有效的 JSON 任务。"}
    else:
        output = dispatch_request(args, request)
    print(json.dumps(output, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
