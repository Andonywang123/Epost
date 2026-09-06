#!/usr/bin/env python3
"""Xiaohongshu-owned entry point for a frozen Agent-approved task.

The Agent must verify its durable approval/execution record before invoking this
local capability. Matching stdin metadata is defense in depth, not a signature
or independent authorization. Never expose this entry point as a public API.
All platform preparation starts here, not in the intake/dashboard package.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


SKILL_ROOT = Path(__file__).resolve().parents[1]
ORIGIN_SKILL = "xiaohongshu-publish"


class Stop(Exception):
    def __init__(self, status: str, message: str, outcome: str = "needs_user"):
        self.status, self.message, self.outcome = status, message, outcome


def digest(value: Any) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


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
            raise Stop("UNREADABLE_RESULT", "小红书发布器返回无法解析的结果，请核实，不能自动重试。", "unknown") from exc
        if not isinstance(value, dict):
            raise Stop("UNREADABLE_RESULT", "小红书发布器返回格式无效，请核实。", "unknown")
        result.append(value)
    if not result:
        raise Stop("EMPTY_RESULT", "小红书发布器未返回结果，请人工核实。", "unknown")
    return result


def invoke(argv: list[str], *, effect: bool = False, timeout: int = 7200) -> dict[str, Any]:
    try:
        completed = subprocess.run(argv, shell=False, capture_output=True, text=True,
                                   encoding="utf-8", timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise Stop("EXECUTION_TIMEOUT", "小红书执行超时；先核实当前任务，禁止自动重发。",
                   "unknown" if effect else "needs_user") from exc
    except OSError as exc:
        raise Stop("RUNTIME_UNAVAILABLE", "小红书发布包运行环境不可用，请配置对应运行时。") from exc
    try:
        value = events(completed.stdout)[-1]
    except Stop as exc:
        if not effect:
            exc.outcome = "needs_user"
        raise
    if completed.returncode:
        raise Stop(str(value.get("status") or value.get("error") or "EXECUTION_FAILED"),
                   "小红书发布器执行未完成；请核实已有记录，不会自动重试。",
                   "unknown" if effect else "needs_user")
    return value


def local_path(value: Any, status: str) -> Path:
    if not isinstance(value, (str, Path)):
        raise Stop(status, "需要明确的本地绝对路径。")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise Stop(status, "需要不含父目录跳转的本地绝对路径。")
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise Stop(status, "素材和任务输出目录不能使用符号链接。")
    return path


def verify_source(request: dict[str, Any]) -> None:
    source, receipt = request.get("source"), request["authorization"]
    if not isinstance(source, dict) or digest(source) != receipt.get("sourceHash"):
        raise Stop("SOURCE_CHANGED", "已确认素材清单与授权回执不一致。")
    if type(source.get("assetRevision")) is not int or type(receipt.get("assetRevision")) is not int or source["assetRevision"] < 1 or source["assetRevision"] != receipt.get("assetRevision"):
        raise Stop("SOURCE_CHANGED", "已确认素材版本与授权回执不一致。")
    media, metadata = source.get("media"), source.get("metadata")
    if not isinstance(media, dict) or media.get("kind") != "image_post":
        raise Stop("UNSUPPORTED_MEDIA", "当前小红书发布包只支持图文，不会将视频改成图片。")
    if not isinstance(metadata, dict) or not isinstance(metadata.get("title"), str) or not metadata["title"].strip() or not isinstance(metadata.get("body"), str):
        raise Stop("INVALID_METADATA", "需要已确认的标题和正文。")
    files = media.get("files")
    if not isinstance(files, list) or not files:
        raise Stop("SOURCE_MISSING", "图文素材文件缺失。")
    records = [*files, *([metadata["cover"]] if metadata.get("cover") is not None else [])]
    for record in records:
        if not isinstance(record, dict) or type(record.get("size")) is not int or record["size"] < 0 or not isinstance(record.get("sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", record["sha256"]):
            raise Stop("SOURCE_CHANGED", "素材大小或摘要记录无效。")
        path = local_path(record.get("path"), "SOURCE_MISSING")
        if path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"} or not path.is_file():
            raise Stop("SOURCE_MISSING", "图文素材必须是已储存的本地图片文件。")
        hashed, size = hashlib.sha256(), 0
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise Stop("SOURCE_MISSING", "素材必须是普通文件。")
                while chunk := stream.read(1024 * 1024):
                    size += len(chunk)
                    hashed.update(chunk)
        except OSError as exc:
            raise Stop("SOURCE_MISSING", "已确认素材暂时不可读取。") from exc
        if size != record["size"] or hashed.hexdigest() != record["sha256"]:
            raise Stop("SOURCE_CHANGED", "已确认素材的字节发生变化，停止分发。")


def check_time(request: dict[str, Any]) -> None:
    scheduled = request.get("scheduledAt")
    if request["decision"] != "schedule":
        if scheduled is not None or request.get("scheduleUtc") is not None:
            raise Stop("DECISION_TIME_MISMATCH", "草稿或立即发布不能保留定时时间。")
        return
    try:
        moment = datetime.fromisoformat(scheduled.replace("Z", "+00:00"))
        utc = datetime.fromisoformat(request["scheduleUtc"].replace("Z", "+00:00"))
        zone = ZoneInfo(request["timezone"])
    except (ValueError, AttributeError, KeyError, TypeError, ZoneInfoNotFoundError) as exc:
        raise Stop("INVALID_TIME", "小红书定时任务需要明确的时区、时间和对应 UTC 时间。") from exc
    if moment.tzinfo is None or utc.tzinfo is None:
        raise Stop("INVALID_TIME", "定时时间必须包含时区偏移。")
    if moment != utc or moment.utcoffset() != moment.astimezone(zone).utcoffset():
        raise Stop("TIMEZONE_MISMATCH", "定时时间、时区与 UTC 时间不一致，需重新确认。")
    if moment <= datetime.now(timezone.utc):
        raise Stop("SCHEDULE_EXPIRED", "定时时间已过，请重新选择并确认。")


def validate_request(request: Any) -> None:
    if not isinstance(request, dict) or request.get("platform") != "xiaohongshu" or request.get("decision") not in {"draft", "publish", "schedule"}:
        raise Stop("INVALID_REQUEST", "分发请求与小红书发布包不匹配。")
    receipt = request.get("authorization")
    if not isinstance(receipt, dict) or not isinstance(receipt.get("receiptId"), str) or not receipt["receiptId"].strip():
        raise Stop("CONFIRMATION_REQUIRED", "没有匹配的 Agent 用户确认回执。")
    for key in ("planHash", "sessionId", "planId"):
        value = request.get(key)
        if not isinstance(value, str) or not value.strip() or receipt.get(key) != value:
            raise Stop("CONFIRMATION_REQUIRED", "发布计划、会话与授权回执不匹配。")
    if not re.fullmatch(r"[a-f0-9]{64}", request["planHash"]) or receipt.get("confirmationSource") != "dashboard":
        raise Stop("CONFIRMATION_REQUIRED", "需要来自看板、且已经 Agent 核验持久记录的确认许可。")
    job_id = request.get("jobId")
    if not isinstance(job_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", job_id) or job_id in {".", ".."}:
        raise Stop("INVALID_JOB", "任务标识不正确。")
    if not isinstance(request.get("accountProfile"), str) or not request["accountProfile"].strip() or not isinstance(request.get("accountSettings"), dict):
        raise Stop("ACCOUNT_REQUIRED", "需要已确认的平台账号和对应本地配置。")
    verify_source(request)
    check_time(request)


def write_manifest(job_dir: Path, value: dict[str, Any]) -> Path:
    path = job_dir / "xiaohongshu.json"
    data = json.dumps(value, ensure_ascii=False, indent=2)
    try:
        with path.open("x", encoding="utf-8") as stream:
            stream.write(data + "\n")
        with (job_dir / "xiaohongshu.json.sha256").open("x", encoding="ascii") as stream:
            stream.write(hashlib.sha256(data.encode("utf-8")).hexdigest() + "\n")
    except FileExistsError as exc:
        raise Stop("JOB_ALREADY_PREPARED", "已有同一任务清单，请先核实；不会覆盖后重发。", "unknown") from exc
    return path


def prepare_and_execute(args: argparse.Namespace, request: dict[str, Any], job_dir: Path) -> dict[str, Any]:
    source, account = request["source"], request["accountSettings"]
    if account.get("visibility") != "公开":
        raise Stop("VISIBILITY_REQUIRED", "现有小红书发布器仅支持账号配置明确为“公开”。")
    cdp, profile = account.get("cdp_url"), account.get("profile_dir")
    if bool(cdp) == bool(profile):
        raise Stop("BROWSER_SETUP_REQUIRED", "小红书账号需要且只能配置 cdp_url 或专用 profile_dir 之一。")
    if cdp:
        match = re.fullmatch(r"http://(?:127\.0\.0\.1|localhost):(\d{1,5})", cdp) if isinstance(cdp, str) else None
        if not match or not 1 <= int(match.group(1)) <= 65535:
            raise Stop("LOCAL_BROWSER_REQUIRED", "仅接受本机 Chrome 调试地址。")
    if profile:
        profile_path = local_path(profile, "BROWSER_SETUP_REQUIRED")
        ordinary_profiles = {
            Path.home() / "Library/Application Support/Google/Chrome",
            Path.home() / ".config/google-chrome",
            Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "Google/Chrome/User Data",
        }
        if any(profile_path == base or base in profile_path.parents for base in ordinary_profiles):
            raise Stop("DEDICATED_PROFILE_REQUIRED", "不能使用日常 Chrome 用户目录，请配置专用目录。")
    node = getattr(args, "node", None) or shutil.which("node")
    if not node:
        raise Stop("NODE_REQUIRED", "需要 Node.js 才能运行小红书发布包。")
    files = source["media"]["files"]
    cover = source["metadata"].get("cover")
    images = ([cover] if cover else []) + [item for item in files if not cover or item["sha256"] != cover["sha256"]]
    data = {
        "job_id": request["jobId"], "account_profile": request["accountProfile"],
        "mode": request["decision"], "title": source["metadata"]["title"],
        "body": source["metadata"]["body"], "images": [item["path"] for item in images],
        "tags": [], "auto_tags": True, "tag_limit": 8, "hot_tag": None,
        "visibility": "公开", "activity": None, "activity_mode": "auto",
        "schedule_at": request.get("scheduledAt"),
    }
    manifest = write_manifest(job_dir, data)
    script = SKILL_ROOT / "scripts/xhs_publisher.mjs"
    check = invoke([node, str(script), "validate", "--manifest", str(manifest)])
    if check.get("status") != "VALID":
        raise Stop("VALIDATION_FAILED", "小红书发布清单校验未通过，未访问平台。")
    check_time(request)
    connection = ["--cdp-url", cdp] if cdp else ["--profile-dir", str(profile)]
    check = invoke([node, str(script), "preflight", *connection])
    if check.get("status") != "PREFLIGHT_OK":
        raise Stop(str(check.get("status") or "NEEDS_USER"), "小红书登录或安全检查需要用户处理。")
    verify_source(request)
    check_time(request)
    if json.loads(manifest.read_text(encoding="utf-8")) != data:
        raise Stop("MANIFEST_CHANGED", "发布前清单发生变化，停止分发。")
    result = invoke([node, str(script), "dispatch", "--decision", request["decision"],
                     "--manifest", str(manifest), *connection, "--commit"], effect=True)
    status = result.get("status") or "UNKNOWN"
    expected = {"draft": {"DRAFT_SAVED"}, "schedule": {"SCHEDULED"}, "publish": {"PUBLISHED", "SUBMITTED", "UNDER_REVIEW"}}
    outcome = "success" if status in expected[request["decision"]] else "unknown"
    if status in {"NEEDS_USER", "NEEDS_AUTHORIZATION", "VALIDATION_FAILED", "ADAPTER_OUTDATED", "FILL_FAILED", "SCHEDULE_FAILED"}:
        outcome = "needs_user"
    labels = {"DRAFT_SAVED": "平台草稿已保存", "SCHEDULED": "定时任务已提交",
              "PUBLISHED": "平台已确认发布", "SUBMITTED": "平台已受理，尚未确认公开", "UNDER_REVIEW": "平台审核中"}
    return {"status": status, "outcome": outcome,
            "message": labels[status] if outcome == "success" else "需要核实小红书执行结果，未自动重试。",
            "url": result.get("url"), "warnings": result.get("warnings", []), "manifest": str(manifest)}


def dispatch_request(args: argparse.Namespace, request: Any) -> dict[str, Any]:
    """Validate one approved task, claim it once, then prepare inside this skill."""
    try:
        validate_request(request)
        root = local_path(args.output_root, "OUTPUT_ROOT_REQUIRED")
        root.mkdir(parents=True, exist_ok=True)
        job_dir = root / request["jobId"]
        try:
            job_dir.mkdir()
        except FileExistsError as exc:
            raise Stop("JOB_ALREADY_ATTEMPTED", "该任务已有执行目录，需先核实；不会自动重试。", "unknown") from exc
        output = prepare_and_execute(args, request, job_dir)
    except Stop as exc:
        output = {"status": exc.status, "outcome": exc.outcome, "message": exc.message}
    except Exception:
        output = {"status": "PLATFORM_ENTRY_EXCEPTION", "outcome": "unknown", "message": "小红书发布包入口异常，请核实已有记录；不会自动重发。"}
    return {**output, "originSkill": ORIGIN_SKILL}


def main() -> None:
    parser = argparse.ArgumentParser(description="接收 Agent 已核验许可的小红书发布包入口")
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--node")
    args = parser.parse_args()
    try:
        request = json.load(sys.stdin)
    except (ValueError, OSError):
        output = {"status": "INVALID_REQUEST", "outcome": "needs_user", "message": "需要完整的已确认 JSON 请求。", "originSkill": ORIGIN_SKILL}
    else:
        output = dispatch_request(args, request)
    print(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    main()
