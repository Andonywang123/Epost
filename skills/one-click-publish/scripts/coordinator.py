"""Local intake and consent-gated orchestration for an existing authenticated host.

This module does not serve HTTP, discover accounts, call an LLM, open browsers,
translate, render media, or invoke publishing tools.  The host supplies trusted
local file paths, capability records, authenticated user events, and a synchronous
executor callback.  Treat all stored content and callback text as untrusted data.

Use one storage root owned by the host user, not a shared/untrusted directory.
An OS worker lock serializes callbacks while brief state locks allow UI polling.
Callbacks must not re-enter run_next for the same session.  A process crash
after RUNNING is persisted produces an unknown outcome, never an automatic retry.
Host authentication cannot be established by this library: the host MUST mint
trusted_user_event_id from a real authenticated confirmation click, and MUST NOT
let a model, uploaded text, or arbitrary request data provide that identifier.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:  # Windows hosts use msvcrt rather than failing during module import.
    import fcntl
except ImportError:
    fcntl = None
try:
    import msvcrt
except ImportError:
    msvcrt = None


PLATFORMS = (
    ("xiaohongshu", "小红书"),
    ("douyin", "抖音"),
    ("wechat_channels", "微信视频号"),
    ("bilibili", "B站"),
    ("youtube", "YouTube"),
    ("instagram", "Instagram"),
    ("tiktok", "TikTok"),
    ("x", "X"),
)
MAINLAND = {p for p, _ in PLATFORMS[:4]}
REGION_ZONES = {
    "china": {"Asia/Shanghai"},
    "us": {"America/Los_Angeles", "America/New_York", "America/Chicago"},
    "uk_eu": {"Europe/London", "Europe/Paris", "Europe/Berlin"},
    "australia": {"Australia/Sydney", "Australia/Perth", "Australia/Brisbane"},
}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm"}
EDITABLE_STAGES = {
    "AWAIT_KIND", "AWAIT_MEDIA", "AWAIT_METADATA", "AWAIT_MATERIAL_CONFIRMATION",
    "CONFIGURING", "AWAIT_PLAN_CONFIRMATION",
}
OUTCOMES = {"success", "failed", "unknown", "needs_user", "pending", "partial"}
SESSION_PATTERN = re.compile(r"^[a-f0-9]{32}$")
ACCOUNT_SETTING_KEYS = {
    "visibility", "privacy", "made_for_kids", "contains_synthetic_media",
    "locale", "notify_subscribers", "cdp_url", "profile_dir",
    "api_key_env", "translation_model", "thumbnail_no_text", "tag_region", "category_id",
}
POST_SETTING_CHOICES = {
    "visibility": ["公开"], "privacy": ["private", "unlisted", "public"],
    "made_for_kids": [True, False], "contains_synthetic_media": [True, False],
    "notify_subscribers": [True, False],
}


class CoordinatorError(ValueError):
    """A safe, structured error the host can render as data, never instructions."""

    def __init__(self, code: str, message: str, **details: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, **self.details}


def _error(code: str, message: str, **details: Any) -> None:
    raise CoordinatorError(code, message, **details)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_now() -> str:
    return _now().isoformat()


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _plain_text(value: Any, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        _error("INVALID_TEXT", f"{field} 必须是明确提供的文本。", field=field)
    return value


def _secure_path(path: Path, *, require_file: bool = False) -> Path:
    if not path.is_absolute() or ".." in path.parts:
        _error("UNSAFE_PATH", "仅接受不含路径穿越的绝对本地路径。")
    for part in (path, *path.parents):
        if part.is_symlink():
            _error("UNSAFE_PATH", "素材与储存路径不能包含符号链接。")
    if require_file and not path.is_file():
        _error("INVALID_FILE", "素材必须是已存在的普通文件。")
    return path


class Coordinator:
    """Storage-only intake followed by an immutable, explicitly approved batch.

    Capability dictionaries are supplied by the host, keyed by platform ID.  They
    must represent adapters/accounts actually available in that host.  Missing
    platforms are unavailable; neither installed skills nor accounts are inferred.
    local_timezone is the host's trusted IANA zone for the '本地时间' choice.
    """

    def __init__(self, storage_root: Path, adapters: dict[str, dict[str, Any]], *, local_timezone: str | None = None):
        self.storage_root = _secure_path(Path(storage_root))
        self.storage_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.storage_root.is_dir():
            _error("INVALID_STORAGE", "储存根目录必须是本地目录。")
        if local_timezone is not None:
            self._zone(local_timezone)
        self.local_timezone = local_timezone
        self.capabilities = self._capabilities(adapters)

    @staticmethod
    def _zone(name: Any) -> ZoneInfo:
        if not isinstance(name, str) or not name:
            _error("INVALID_TIMEZONE", "请选择明确的 IANA 时区。")
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            _error("INVALID_TIMEZONE", "主机未提供此 IANA 时区数据。", timezone=name)

    @staticmethod
    def _capabilities(adapters: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        if not isinstance(adapters, dict):
            _error("INVALID_CAPABILITIES", "适配器能力必须由宿主应用明确提供。")
        records = {}
        for platform, label in PLATFORMS:
            supplied = adapters.get(platform, {})
            if not isinstance(supplied, dict):
                _error("INVALID_CAPABILITIES", "平台能力配置格式错误。", platform=platform)
            kinds = supplied.get("mediaKinds", [])
            modes = supplied.get("modes", [])
            accounts = supplied.get("accounts", [])
            scope = supplied.get("draftScope")
            lead = supplied.get("minLeadMinutes", 0)
            available = supplied.get("available", False)
            if type(available) is not bool or not isinstance(kinds, list) or not all(isinstance(k, str) for k in kinds) or not set(kinds) <= {"image_post", "video"}:
                _error("INVALID_CAPABILITIES", "平台素材能力配置错误。", platform=platform)
            if not isinstance(modes, list) or not all(isinstance(m, str) for m in modes) or not set(modes) <= {"draft", "publish", "schedule"}:
                _error("INVALID_CAPABILITIES", "平台发送模式配置错误。", platform=platform)
            if scope not in {None, "platform", "local"} or ("draft" in modes and scope is None):
                _error("INVALID_CAPABILITIES", "草稿能力必须声明平台草稿或本地草稿。", platform=platform)
            if type(lead) is not int or lead < 0:
                _error("INVALID_CAPABILITIES", "最短定时提前量必须是非负整数分钟。", platform=platform)
            if not isinstance(accounts, list):
                _error("INVALID_CAPABILITIES", "账号能力配置错误。", platform=platform)
            clean_accounts = []
            for account in accounts:
                if not isinstance(account, dict):
                    _error("INVALID_CAPABILITIES", "账号能力配置错误。", platform=platform)
                settings = account.get("settings", {})
                if not isinstance(settings, dict) or not set(settings) <= ACCOUNT_SETTING_KEYS:
                    _error("INVALID_CAPABILITIES", "账号 settings 只能包含支持的非密钥发布配置；凭据应由宿主管理。", platform=platform)
                for key, value in settings.items():
                    if key in {"made_for_kids", "contains_synthetic_media", "notify_subscribers", "thumbnail_no_text"}:
                        valid = type(value) is bool
                    elif key == "api_key_env":
                        valid = isinstance(value, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value) is not None
                    else:
                        valid = isinstance(value, str) and bool(value.strip())
                    if not valid:
                        _error("INVALID_CAPABILITIES", "账号发布配置值类型错误。", platform=platform, field=key)
                clean_accounts.append({
                    "id": _plain_text(account.get("id"), "account.id"),
                    "label": _plain_text(account.get("label"), "account.label"),
                    "settings": copy.deepcopy(settings),
                })
            if len({a["id"] for a in clean_accounts}) != len(clean_accounts):
                _error("INVALID_CAPABILITIES", "平台账号标识不能重复。", platform=platform)
            fields = supplied.get("requiredPostSettings", [])
            require_confirmation = supplied.get("requireAccountConfirmation", False)
            if (not isinstance(fields, list) or not all(isinstance(k, str) and k in POST_SETTING_CHOICES for k in fields)
                    or len(set(fields)) != len(fields) or type(require_confirmation) is not bool):
                _error("INVALID_CAPABILITIES", "本次发布声明的配置无效。", platform=platform)
            records[platform] = {
                "platform": platform, "label": _plain_text(supplied.get("label", label), "platform.label"),
                "available": available, "mediaKinds": sorted(set(kinds)), "modes": sorted(set(modes)),
                "draftScope": scope, "accounts": clean_accounts, "minLeadMinutes": lead,
                "requiredPostSettings": list(fields), "requireAccountConfirmation": require_confirmation,
                "reason": _plain_text(supplied.get("reason", "" if available else "宿主尚未接入此平台发布包。"), "reason", allow_empty=True),
            }
        return records

    def create_session(self) -> dict[str, Any]:
        session_id = uuid.uuid4().hex
        folder = self.storage_root / session_id
        folder.mkdir(mode=0o700)
        state = {
            "schemaVersion": 1, "sessionId": session_id, "stage": "AWAIT_KIND",
            "contentKind": None, "mediaLibrary": {},
            "assetRevision": 0, "confirmedAssetRevision": None, "media": None,
            "metadata": None, "plan": None, "approval": None, "jobs": [],
            "settings": [], "history": [], "createdAt": _iso_now(), "updatedAt": _iso_now(),
        }
        with self._locked(session_id):
            self._save(session_id, state)
        return self._snapshot(state)

    def get_snapshot(self, session_id: str) -> dict[str, Any]:
        with self._locked(session_id):
            return self._snapshot(self._load(session_id))

    def _folder(self, session_id: str) -> Path:
        if not isinstance(session_id, str) or not SESSION_PATTERN.fullmatch(session_id):
            _error("INVALID_SESSION", "会话标识不合法。")
        folder = _secure_path(self.storage_root / session_id)
        if not folder.is_dir():
            _error("SESSION_NOT_FOUND", "素材会话不存在。")
        return folder

    @contextmanager
    def _locked(self, session_id: str, *, worker: bool = False) -> Iterator[bool]:
        lock_path = self._folder(session_id) / ("worker.lock" if worker else "session.lock")
        _secure_path(lock_path)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "a+b") as lock:
            if fcntl is not None:
                try:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | (fcntl.LOCK_NB if worker else 0))
                except BlockingIOError:
                    yield False
                    return
            elif msvcrt is not None:
                lock.seek(0, os.SEEK_END)
                if lock.tell() == 0:
                    lock.write(b"0")
                    lock.flush()
                lock.seek(0)
                try:
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK if worker else msvcrt.LK_LOCK, 1)
                except OSError:
                    if worker:
                        yield False
                        return
                    _error("SESSION_BUSY", "该素材会话正在被其他操作占用，请稍后刷新状态；不要重试平台提交。")
            else:
                _error("LOCKING_UNSUPPORTED", "宿主系统不支持安全文件锁；此协调器需要 POSIX 或 Windows。")
            try:
                yield True
            finally:
                if fcntl is not None:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
                else:
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)

    def _load(self, session_id: str) -> dict[str, Any]:
        path = _secure_path(self._folder(session_id) / "state.json", require_file=True)
        with path.open("r", encoding="utf-8") as stream:
            state = json.load(stream)
        if state.get("sessionId") != session_id or state.get("schemaVersion") != 1:
            _error("INVALID_STATE", "会话状态不匹配或版本不支持。")
        # Additive migration: keep old material/authorization records untouched.
        state.setdefault("contentKind", state["media"]["kind"] if state.get("media") else None)
        state.setdefault("mediaLibrary", {state["media"]["kind"]: copy.deepcopy(state["media"])} if state.get("media") else {})
        return state

    def _save(self, session_id: str, state: dict[str, Any]) -> None:
        folder = self._folder(session_id)
        destination = _secure_path(folder / "state.json")
        state["updatedAt"] = _iso_now()
        fd, temporary = tempfile.mkstemp(prefix=".state-", suffix=".json", dir=folder)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(_canonical(state))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
            if os.name != "nt":  # Windows does not expose directory fsync here.
                directory_fd = os.open(folder, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _snapshot(self, state: dict[str, Any]) -> dict[str, Any]:
        results = []
        for job in state["jobs"]:
            result = job.get("result")
            if result is not None:
                results.append({"platform": job["platform"], "jobId": job["jobId"], "draftScope": job.get("draftScope"), **result})
            elif job["state"] == "RUNNING":
                results.append({"platform": job["platform"], "jobId": job["jobId"], "status": "RUNNING"})
        return copy.deepcopy({
            "sessionId": state["sessionId"], "stage": state["stage"], "assetRevision": state["assetRevision"],
            "media": state["media"], "metadata": state["metadata"], "capabilities": list(self.capabilities.values()),
            "contentKind": state["contentKind"], "savedMediaKinds": list(state["mediaLibrary"]),
            "localTimezone": self.local_timezone, "results": results, "settings": state["settings"],
            "review": state["plan"]["review"] if state["plan"] else None,
            "pendingReview": state["plan"]["review"] if state["plan"] and state["stage"] == "AWAIT_PLAN_CONFIRMATION" else None,
            "sourceConfirmed": state["confirmedAssetRevision"] == state["assetRevision"] and state["assetRevision"] > 0,
            "handoffConfirmed": state["approval"] is not None,
            "executionOwner": state.get("executionOwner"),
        })

    @staticmethod
    def _editable(state: dict[str, Any]) -> None:
        if state["stage"] not in EDITABLE_STAGES:
            _error("MATERIALS_LOCKED", "正在执行或待人工核对的批次不能修改素材；请先在宿主应用核对结果。")

    @staticmethod
    def _invalidate(state: dict[str, Any]) -> None:
        if state["plan"]:
            state["history"].append({"plan": state["plan"], "approval": state["approval"], "jobs": state["jobs"]})
        state["assetRevision"] += 1
        state["confirmedAssetRevision"] = None
        state["plan"] = None
        state["approval"] = None
        state["jobs"] = []

    def select_kind(self, session_id: str, kind: str, asset_revision: int) -> dict[str, Any]:
        """Select exactly one route; retain both routes' previously stored files."""
        if kind not in {"image_post", "video"}:
            _error("INVALID_KIND", "请先选择图文或视频。")
        with self._locked(session_id):
            state = self._load(session_id)
            self._editable(state)
            if type(asset_revision) is not int or asset_revision != state["assetRevision"]:
                _error("STALE_MATERIALS", "素材已更新，请刷新后再选择内容类型。")
            if state["contentKind"] == kind:
                return self._snapshot(state)
            if state["media"]:
                state["mediaLibrary"][state["media"]["kind"]] = copy.deepcopy(state["media"])
            self._invalidate(state)
            state["contentKind"] = kind
            state["media"] = copy.deepcopy(state["mediaLibrary"].get(kind))
            state["settings"] = []
            state["stage"] = ("AWAIT_MEDIA" if state["media"] is None else
                              "AWAIT_METADATA" if state["metadata"] is None else "AWAIT_MATERIAL_CONFIRMATION")
            self._save(session_id, state)
            return self._snapshot(state)

    def _copy_batch(self, session_id: str, revision: int, named_paths: list[tuple[str, Path]]) -> list[dict[str, Any]]:
        """Copy literal bytes only; source files are never edited or overwritten."""
        folder = self._folder(session_id)
        destination = folder / f"assets-r{revision}-{uuid.uuid4().hex}"
        temporary = Path(tempfile.mkdtemp(prefix=".assets-", dir=folder))
        records = []
        try:
            for index, (role, source) in enumerate(named_paths):
                source = _secure_path(source, require_file=True)
                target_name = f"{index:03d}-{role}{source.suffix.lower()}"
                target = temporary / target_name
                source_fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                digest = hashlib.sha256()
                size = 0
                with os.fdopen(source_fd, "rb") as original:
                    initial = os.fstat(original.fileno())
                    if not stat.S_ISREG(initial.st_mode):
                        _error("INVALID_FILE", "素材必须是普通文件。")
                    with target.open("xb") as copied:
                        os.chmod(target, 0o600)
                        while chunk := original.read(1024 * 1024):
                            copied.write(chunk)
                            digest.update(chunk)
                            size += len(chunk)
                        copied.flush()
                        os.fsync(copied.fileno())
                    final = os.fstat(original.fileno())
                    if (initial.st_size, initial.st_mtime_ns, initial.st_ino) != (final.st_size, final.st_mtime_ns, final.st_ino) or final.st_size != size:
                        _error("SOURCE_CHANGED", "复制过程中源文件发生变化，请重新上传。")
                if size == 0:
                    _error("EMPTY_FILE", "素材文件不能为空。")
                records.append({"name": source.name, "size": size, "path": str(destination / target_name), "sha256": digest.hexdigest()})
            os.rename(temporary, destination)
            return records
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)

    def store_media(self, session_id: str, kind: str, paths: list[str | Path]) -> dict[str, Any]:
        """Step one: store one image-post batch OR one video; perform no analysis."""
        if kind not in {"image_post", "video"} or not isinstance(paths, list) or not paths:
            _error("INVALID_MEDIA", "第一步请上传一组图文图片，或一个视频。")
        if kind == "video" and len(paths) != 1:
            _error("INVALID_MEDIA", "一次素材包只接受一个视频。")
        if any(not isinstance(path, (str, Path)) for path in paths):
            _error("INVALID_FILE", "素材路径须由宿主应用提供。")
        sources = [_secure_path(Path(path), require_file=True) for path in paths]
        allowed = IMAGE_EXTENSIONS if kind == "image_post" else VIDEO_EXTENSIONS
        if any(path.suffix.lower() not in allowed for path in sources):
            _error("INVALID_MEDIA", "素材扩展名不符合选择的图文或视频类型。")
        if len({str(path) for path in sources}) != len(sources):
            _error("DUPLICATE_MEDIA", "同一批次不能重复上传相同素材路径。")
        with self._locked(session_id):
            state = self._load(session_id)
            self._editable(state)
            if state["contentKind"] != kind:
                _error("KIND_MISMATCH", "请先选择对应内容类型，再上传该类型素材；不会自动改变发布路径。")
            records = self._copy_batch(session_id, state["assetRevision"] + 1, [("media", p) for p in sources])
            self._invalidate(state)
            state["media"] = {"kind": kind, "files": records}
            state["mediaLibrary"][kind] = copy.deepcopy(state["media"])
            # Returning to step one keeps separately supplied metadata. It is
            # not approved for the replacement media until the user reconfirms.
            state["stage"] = "AWAIT_MATERIAL_CONFIRMATION" if state["metadata"] is not None else "AWAIT_METADATA"
            self._save(session_id, state)
            return self._snapshot(state)

    def store_metadata(self, session_id: str, cover_path: str | Path | None, cover_absent: bool, title: str, body: str, *, keep_cover_revision: int | None = None) -> dict[str, Any]:
        """Step two: preserve the explicitly labelled cover/title/body verbatim."""
        title = _plain_text(title, "标题")
        body = _plain_text(body, "正文", allow_empty=True)
        keep_cover = keep_cover_revision is not None
        if type(cover_absent) is not bool or (keep_cover and (type(keep_cover_revision) is not int or cover_path is not None or cover_absent)) or (not keep_cover and (cover_path is None) != cover_absent):
            _error("INVALID_COVER", "请上传封面，或明确选择不提供封面；两者不能同时选择。")
        if cover_path is not None:
            if not isinstance(cover_path, (str, Path)):
                _error("INVALID_COVER", "封面路径须由宿主应用提供。")
            cover_source = _secure_path(Path(cover_path), require_file=True)
            if cover_source.suffix.lower() not in IMAGE_EXTENSIONS:
                _error("INVALID_COVER", "封面须为 JPG、PNG 或 WebP 图片。")
        with self._locked(session_id):
            state = self._load(session_id)
            self._editable(state)
            if state["media"] is None:
                _error("MEDIA_REQUIRED", "请先完成第一步素材上传。")
            if keep_cover:
                if keep_cover_revision != state["assetRevision"] or not state["metadata"] or not state["metadata"].get("cover"):
                    _error("STALE_COVER", "原封面已变化或不存在，请刷新后重新确认要保留的封面。")
                cover = copy.deepcopy(state["metadata"]["cover"])
                _secure_path(Path(cover["path"]), require_file=True)
            else:
                cover = self._copy_batch(session_id, state["assetRevision"] + 1, [("cover", cover_source)])[0] if cover_path is not None else None
            metadata = {"cover": cover, "coverAbsent": cover_absent, "title": title, "body": body}
            if metadata == state["metadata"]:
                return self._snapshot(state)
            self._invalidate(state)
            state["metadata"] = metadata
            state["stage"] = "AWAIT_MATERIAL_CONFIRMATION"
            self._save(session_id, state)
            return self._snapshot(state)

    def confirm_materials(self, session_id: str, asset_revision: int) -> dict[str, Any]:
        with self._locked(session_id):
            state = self._load(session_id)
            if type(asset_revision) is not int or asset_revision != state["assetRevision"]:
                _error("STALE_MATERIALS", "素材已更新，请重新查看并确认当前素材。")
            if state["stage"] == "CONFIGURING" and state["confirmedAssetRevision"] == asset_revision:
                return self._snapshot(state)
            if state["stage"] != "AWAIT_MATERIAL_CONFIRMATION" or state["media"] is None or state["metadata"] is None:
                _error("WRONG_STAGE", "完成两步上传后才能确认素材。")
            state["confirmedAssetRevision"] = asset_revision
            state["stage"] = "CONFIGURING"
            self._save(session_id, state)
            return self._snapshot(state)

    def _schedule(self, platform: str, timing: Any, min_lead: int) -> dict[str, Any]:
        if not isinstance(timing, dict) or timing.get("kind") not in {"now", "scheduled"}:
            _error("TIME_REQUIRED", "一键发布必须选择立即发布或明确的定时时间。", platform=platform)
        region = timing.get("region")
        zone_name = timing.get("timezone")
        if platform in MAINLAND:
            if region != "china" or zone_name != "Asia/Shanghai":
                _error("INVALID_TIMEZONE", "中国大陆平台使用中国标准时间 Asia/Shanghai。", platform=platform)
        elif region == "local":
            if self.local_timezone is None or zone_name != self.local_timezone:
                _error("INVALID_TIMEZONE", "本地时间须使用宿主应用确认的 IANA 时区。", platform=platform)
        elif region not in REGION_ZONES or zone_name not in REGION_ZONES[region]:
            _error("INVALID_TIMEZONE", "地区与所选 IANA 时区不匹配。", platform=platform)
        zone = self._zone(zone_name)
        if timing["kind"] == "now":
            if any(timing.get(key) is not None for key in ("date", "minute", "fold")):
                _error("INVALID_TIME", "立即发布不能同时携带定时时间。", platform=platform)
            return {"kind": "now", "region": region, "timezone": zone_name, "scheduledAt": None, "utc": None, "timeLabel": "立即发布"}
        date = timing.get("date")
        minute = timing.get("minute")
        fold = timing.get("fold")
        if not isinstance(date, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) or type(minute) is not int or not 0 <= minute < 1440:
            _error("INVALID_TIME", "定时发布需有效日期和 0–1439 的分钟位置。", platform=platform)
        if fold is not None and (type(fold) is not int or fold not in (0, 1)):
            _error("INVALID_TIME", "夏令时重复时刻选择必须是 0 或 1。", platform=platform)
        try:
            naive = datetime.strptime(date, "%Y-%m-%d").replace(hour=minute // 60, minute=minute % 60)
        except ValueError:
            _error("INVALID_TIME", "定时日期无效。", platform=platform)
        candidates = []
        for variant in (0, 1):
            candidate = naive.replace(tzinfo=zone, fold=variant)
            roundtrip = candidate.astimezone(timezone.utc).astimezone(zone)
            if roundtrip.replace(tzinfo=None) == naive and roundtrip.fold == variant:
                candidates.append(candidate)
        if not candidates:
            _error("NONEXISTENT_LOCAL_TIME", "该时刻因夏令时切换不存在，请重新选择时间。", platform=platform)
        if len(candidates) == 2 and fold is None:
            _error("AMBIGUOUS_LOCAL_TIME", "该时刻因夏令时切换出现两次，请明确选择。", platform=platform, candidates=[{"fold": c.fold, "scheduledAt": c.isoformat(), "utc": c.astimezone(timezone.utc).isoformat()} for c in candidates])
        selected = next((c for c in candidates if fold is None or c.fold == fold), None)
        if selected is None:
            _error("INVALID_TIME", "所选重复时刻标记与该日期不匹配。", platform=platform)
        utc = selected.astimezone(timezone.utc)
        self._check_future(utc, min_lead, platform)
        return {"kind": "scheduled", "region": region, "timezone": zone_name, "date": date, "minute": minute, "fold": selected.fold, "scheduledAt": selected.isoformat(), "utc": utc.isoformat(), "timeLabel": f"{date} {selected:%H:%M} {zone_name} (UTC{selected:%z})"}

    @staticmethod
    def _check_future(instant: datetime, min_lead: int, platform: str) -> None:
        if instant <= _now() + timedelta(minutes=min_lead):
            _error("SCHEDULE_EXPIRED", "定时时间已过或不足平台最短提前量；不会自动改成立即发布。", platform=platform, minLeadMinutes=min_lead)

    @staticmethod
    def _policies(platform: str) -> dict[str, Any]:
        if platform == "xiaohongshu":
            return {"relatedTags": "auto", "activity": "auto_if_relevant", "requiredActivityTags": True, "unmatchedActivity": "skip", "cover": "first_image_deduplicate_same_file_no_crop_or_rewrite"}
        if platform == "youtube":
            return {"englishTitleAndBody": True, "englishVideoSubtitles": True, "relatedTags": "auto", "englishCoverWhenNeeded": True}
        return {}

    def prepare_plan(self, session_id: str, asset_revision: int, rows: list[dict[str, Any]]) -> dict[str, Any]:
        """Compile a local review only. No platform preparation before final Yes."""
        with self._locked(session_id):
            state = self._load(session_id)
            if state["stage"] not in {"CONFIGURING", "AWAIT_PLAN_CONFIRMATION"}:
                _error("WRONG_STAGE", "先完成素材确认，再配置平台分发。")
            if type(asset_revision) is not int or asset_revision != state["assetRevision"] or state["confirmedAssetRevision"] != asset_revision:
                _error("STALE_MATERIALS", "素材版本不匹配，请重新确认。")
            if not isinstance(rows, list) or not rows:
                _error("NO_PLATFORM", "请至少选择一个平台。")
            selected = {}
            seen = set()
            for row in rows:
                if not isinstance(row, dict) or row.get("platform") not in self.capabilities:
                    _error("INVALID_PLATFORM", "平台选项不合法。")
                platform = row["platform"]
                if platform in seen:
                    _error("DUPLICATE_PLATFORM", "每个平台只能配置一次。", platform=platform)
                seen.add(platform)
                if type(row.get("selected")) is not bool:
                    _error("INVALID_SELECTION", "平台选择值必须是布尔值。", platform=platform)
                if row["selected"]:
                    selected[platform] = row
            if not selected:
                _error("NO_PLATFORM", "请至少选择一个平台。")
            targets, review_rows, notices = [], [], []
            for platform, _ in PLATFORMS:
                if platform not in selected:
                    continue
                row = selected[platform]
                cap = self.capabilities[platform]
                action = row.get("action")
                if not cap["available"] or state["media"]["kind"] not in cap["mediaKinds"]:
                    _error("PLATFORM_UNAVAILABLE", "该平台尚未接入，或不支持当前素材类型。", platform=platform, reason=cap["reason"])
                if action not in {"draft", "publish"}:
                    _error("INVALID_ACTION", "请选择一键发布或一键草稿。", platform=platform)
                account_id = row.get("accountProfile")
                account = next((a for a in cap["accounts"] if a["id"] == account_id), None)
                if account is None:
                    _error("ACCOUNT_REQUIRED", "请明确选择已接入的平台账号。", platform=platform)
                if cap["requireAccountConfirmation"] and row.get("accountConfirmed") is not True:
                    _error("ACCOUNT_CONFIRMATION_REQUIRED", "请确认本次使用的本地账号配置；登录状态仍将在执行时检查。", platform=platform)
                account_settings = copy.deepcopy(account["settings"])
                post_settings = row.get("postSettings", {})
                fields = cap["requiredPostSettings"]
                if not isinstance(post_settings, dict) or set(post_settings) != set(fields):
                    _error("DECLARATION_REQUIRED", "请在看板补全本次发布的可见范围与必要声明。", platform=platform)
                for key, value in post_settings.items():
                    if not any(type(value) is type(choice) and value == choice for choice in POST_SETTING_CHOICES[key]):
                        _error("INVALID_DECLARATION", "本次发布声明的值无效。", platform=platform, field=key)
                if (platform == "youtube" and post_settings.get("made_for_kids") is True
                        and post_settings.get("notify_subscribers") is True):
                    _error("DECLARATION_CONFLICT", "YouTube 面向儿童的视频不能通知订阅者；请将通知订阅者改为“否”，或重新确认受众。",
                           platform=platform, fields=["made_for_kids", "notify_subscribers"])
                account_settings.update(copy.deepcopy(post_settings))
                setting_labels = {"visibility": "可见范围", "privacy": "视频公开范围", "made_for_kids": "面向儿童", "contains_synthetic_media": "包含合成媒体", "notify_subscribers": "通知订阅者", "locale": "英文语言地区", "translation_model": "翻译模型"}
                display_settings = [f"{label}：{('是' if value else '否') if type(value) is bool else value}" for key, label in setting_labels.items() if key in account_settings for value in [account_settings[key]]]
                if display_settings:
                    notices.append(f"{cap['label']}（{account['label']}）账号发布设置：" + "；".join(display_settings) + "。")
                if "api_key_env" in account_settings:
                    notices.append(f"{cap['label']} 发布包被触发后自行检查所需翻译服务；总控不读取或检查 API 密钥。")
                if not account_settings:
                    notices.append(f"{cap['label']} 未配置账号发布参数；发布包如需要这些参数会暂停并要求补充，不会自动猜测。")
                scope = cap["draftScope"] if action == "draft" else None
                local_ack = row.get("acceptLocalDraft", False)
                if type(local_ack) is not bool:
                    _error("INVALID_ACKNOWLEDGEMENT", "本地草稿确认须是布尔值。", platform=platform)
                notice = None
                if action == "draft":
                    if row.get("timing") is not None:
                        _error("DRAFT_HAS_TIME", "一键草稿不能携带发布时间。", platform=platform)
                    decision = "draft"
                    schedule = None
                    if scope == "local":
                        notice = f"{cap['label']} 仅保存本地待发布包，不是平台草稿箱；不会上传。"
                        if not local_ack:
                            _error("LOCAL_DRAFT_ACK_REQUIRED", notice, platform=platform)
                        notices.append(notice)
                else:
                    schedule = self._schedule(platform, row.get("timing"), cap["minLeadMinutes"])
                    decision = "publish" if schedule["kind"] == "now" else "schedule"
                if decision not in cap["modes"]:
                    _error("MODE_UNSUPPORTED", "该平台发布包不支持此执行模式。", platform=platform, decision=decision)
                if platform == "youtube" and decision == "schedule" and account_settings.get("privacy") != "public":
                    _error("SCHEDULE_VISIBILITY_REQUIRED", "YouTube 定时发布会到点公开，请明确选择公开范围为 public。", platform=platform)
                policies = self._policies(platform)
                if platform == "xiaohongshu":
                    notices.append("小红书将匹配相关话题、适合的活动及活动必带话题；不匹配则跳过活动。封面作为首图，同文件去重，不裁剪改写。")
                if platform == "youtube":
                    notices.append("YouTube 发布包被触发后，将在包内把标题与正文转为英文、生成英文字幕并匹配相关标签；总控只转交该需求。")
                targets.append({"platform": platform, "decision": decision, "accountProfile": account_id, "accountSettings": account_settings, "draftScope": scope, "acceptLocalDraft": local_ack, "schedule": schedule, "policies": policies, "capability": copy.deepcopy(cap)})
                review_rows.append({"platform": platform, "label": cap["label"], "actionLabel": "一键草稿" if action == "draft" else "一键发布", "timeLabel": "不适用" if schedule is None else schedule["timeLabel"], "accountLabel": account["label"], "draftScope": scope, **({"notice": notice} if notice else {})})
            source = {"assetRevision": asset_revision, "media": copy.deepcopy(state["media"]), "metadata": copy.deepcopy(state["metadata"])}
            plan_id = uuid.uuid4().hex
            payload = {"sessionId": session_id, "planId": plan_id, "assetRevision": asset_revision, "source": source, "sourceHash": _digest(source), "targets": targets, "createdAt": _iso_now()}
            plan_hash = _digest(payload)
            review = {"sessionId": session_id, "planId": plan_id, "planHash": plan_hash, "question": "您确认要在" + "、".join(r["label"] for r in review_rows) + "发布吗？", "rows": review_rows, "notices": notices}
            state["plan"] = {"payload": payload, "hash": plan_hash, "review": review}
            state["settings"] = copy.deepcopy(rows)
            state["approval"] = None
            state["jobs"] = []
            state["stage"] = "AWAIT_PLAN_CONFIRMATION"
            self._save(session_id, state)
            return copy.deepcopy(review)

    def confirm_plan(self, session_id: str, plan_id: str, plan_hash: str, confirmed: bool, *, trusted_user_event_id: str, confirmation_source: str = "unknown") -> dict[str, Any]:
        """Host-only entry point: authenticated real user event, never model data."""
        event_id = _plain_text(trusted_user_event_id, "trusted_user_event_id")
        source = _plain_text(confirmation_source, "confirmation_source")
        if type(confirmed) is not bool:
            _error("INVALID_CONFIRMATION", "确认结果必须为是或否。")
        with self._locked(session_id):
            state = self._load(session_id)
            plan = state["plan"]
            if plan is None or plan["payload"]["planId"] != plan_id or plan["hash"] != plan_hash or _digest(plan["payload"]) != plan_hash:
                _error("STALE_PLAN", "发布设置已变更，请重新查看确认框。")
            if state["approval"] is not None:
                if confirmed:
                    return self._snapshot(state)  # Idempotent: never enqueue a second batch.
                _error("ALREADY_CONFIRMED", "此批次已确认；不能用旧确认框撤回已开始的平台操作。")
            if state["stage"] != "AWAIT_PLAN_CONFIRMATION":
                _error("WRONG_STAGE", "当前没有等待确认的发布计划。")
            if not confirmed:
                state["plan"] = None
                state["stage"] = "CONFIGURING"
                self._save(session_id, state)
                return self._snapshot(state)
            payload = plan["payload"]
            if state["assetRevision"] != payload["assetRevision"] or state["confirmedAssetRevision"] != payload["assetRevision"]:
                _error("STALE_MATERIALS", "素材版本已改变，请重新确认。")
            for target in payload["targets"]:
                if target["capability"] != self.capabilities[target["platform"]]:
                    _error("CAPABILITIES_CHANGED", "平台能力或账号已改变，请重新配置并确认。", platform=target["platform"])
                if target["decision"] == "schedule":
                    self._check_future(datetime.fromisoformat(target["schedule"]["utc"]), target["capability"]["minLeadMinutes"], target["platform"])
            receipt = {"receiptId": uuid.uuid4().hex, "sessionId": session_id, "planId": plan_id, "planHash": plan_hash, "assetRevision": payload["assetRevision"], "sourceHash": payload["sourceHash"], "trustedUserEventId": event_id, "confirmationSource": source, "confirmedAt": _iso_now()}
            state["approval"] = receipt
            state["jobs"] = [{"jobId": hashlib.sha256(f"{plan_hash}:{target['platform']}".encode()).hexdigest(), "platform": target["platform"], "draftScope": target["draftScope"], "state": "QUEUED", "result": None} for target in payload["targets"]]
            # A browser click records consent only. The owning Agent must pick
            # up this durable task separately; HTTP refresh never runs it.
            state["stage"] = "AWAIT_AGENT"
            state["executionOwner"] = None
            self._save(session_id, state)
            return self._snapshot(state)

    def get_agent_task(self, session_id: str) -> dict[str, Any]:
        """Read a confirmed handoff from disk; do not claim, execute or retry it."""
        with self._locked(session_id):
            state = self._load(session_id)
            plan, approval = state["plan"], state["approval"]
            if not plan or not approval:
                _error("NOT_AUTHORIZED", "面板尚未保存用户最终确认，Agent 不能执行。")
            if (_digest(plan["payload"]) != plan["hash"] or approval["planHash"] != plan["hash"]
                    or approval["sourceHash"] != plan["payload"]["sourceHash"]):
                _error("PLAN_CHANGED", "已确认任务发生变化，请人工核实。")
            return copy.deepcopy({
                "sessionId": session_id, "planHash": plan["hash"], "stage": state["stage"],
                "confirmedAt": approval["confirmedAt"], "executionOwner": state.get("executionOwner"),
                "confirmationSource": approval.get("confirmationSource", "unknown"),
                "payload": plan["payload"], "jobs": state["jobs"],
            })

    def reconcile_agent_execution(self, session_id: str, expected_plan_hash: str) -> dict[str, Any]:
        """Agent-only crash check: an owned worker is never interrupted.

        No callback is invoked, even if another worker finished between reads.
        HTTP refresh must not call this method.
        """
        with self._locked(session_id, worker=True) as acquired:
            if not acquired:
                return self.get_snapshot(session_id)
            with self._locked(session_id):
                state = self._load(session_id)
                if not state["plan"] or state["plan"]["hash"] != expected_plan_hash:
                    _error("STALE_PLAN", "Agent 核对的任务与当前计划不一致。")
                running = next((job for job in state["jobs"] if job["state"] == "RUNNING"), None)
                if running and state["stage"] == "EXECUTING":
                    running["state"] = "PAUSED"
                    running["result"] = {"status": "UNKNOWN_AFTER_INTERRUPTION", "outcome": "unknown",
                                         "message": "Agent 执行进程已中断，平台结果需人工核实；未重试。"}
                    state["stage"] = "PAUSED"
                    self._save(session_id, state)
                return self._snapshot(state)

    def _verify_source(self, session_id: str, source: dict[str, Any], expected_hash: str) -> None:
        if _digest(source) != expected_hash:
            _error("SOURCE_CHANGED", "冻结素材清单不一致。")
        records = list(source["media"]["files"])
        if source["metadata"]["cover"]:
            records.append(source["metadata"]["cover"])
        folder = self._folder(session_id)
        for record in records:
            path = _secure_path(Path(record["path"]), require_file=True)
            if not path.is_relative_to(folder):
                _error("SOURCE_CHANGED", "冻结素材不在该会话的储存目录内。")
            digest = hashlib.sha256()
            size = 0
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    size += len(chunk)
                    digest.update(chunk)
            if size != record["size"] or digest.hexdigest() != record["sha256"]:
                _error("SOURCE_CHANGED", "已确认素材的字节发生变化，必须停止分发。")

    @staticmethod
    def _adapter_result(result: Any) -> dict[str, Any]:
        if not isinstance(result, dict) or not isinstance(result.get("status"), str) or not result["status"].strip() or result.get("outcome") not in OUTCOMES:
            return {"status": "INVALID_ADAPTER_RESULT", "outcome": "unknown", "message": "平台返回结果无法可靠判断；请人工核对，禁止盲目重试。"}
        clean = {"status": result["status"], "outcome": result["outcome"]}
        for field in ("message", "url", "externalId", "originSkill"):
            if isinstance(result.get(field), str):
                clean[field] = result[field]
        # Acceptance/processing does not prove publication or a saved draft.
        if clean["outcome"] == "success" and clean["status"].strip().lower() in {"accepted", "queued", "pending", "processing", "submitted"}:
            clean["outcome"] = "pending"
            clean["message"] = "平台仅确认接收或处理中，尚未验证最终结果；请核对后继续。"
        return clean

    def run_next(self, session_id: str, executor_callback: Callable[[dict[str, Any]], dict[str, Any]], *, expected_plan_hash: str | None = None) -> dict[str, Any]:
        """Invoke at most one confirmed target; never silently retry or parallelize.

        Called by the Agent executor, never the HTTP service. The first call
        claims AWAIT_AGENT; further calls are allowed only while EXECUTING.
        Executor must check the exact target mode/account and return verified
        outcomes. Do not wrap this operation in generic automatic retries.
        """
        if not callable(executor_callback):
            _error("EXECUTOR_REQUIRED", "宿主须提供明确的平台执行回调。")
        with self._locked(session_id, worker=True) as acquired:
            if not acquired:
                return self.get_snapshot(session_id)
            return self._run_next_claimed(session_id, executor_callback, expected_plan_hash)

    def _run_next_claimed(self, session_id: str, executor_callback: Callable[[dict[str, Any]], dict[str, Any]], expected_plan_hash: str | None = None) -> dict[str, Any]:
        """Worker lock held, state lock released during slow external work."""
        with self._locked(session_id):
            state = self._load(session_id)
            if expected_plan_hash is not None and (not state["plan"] or state["plan"]["hash"] != expected_plan_hash):
                _error("STALE_PLAN", "Agent 接手的任务与当前确认计划不一致，未执行。")
            if state["stage"] in {"PAUSED", "FINISHED"}:
                return self._snapshot(state)
            if state["stage"] not in {"AWAIT_AGENT", "EXECUTING"} or not state["approval"] or not state["plan"]:
                _error("NOT_AUTHORIZED", "必须先由用户确认冻结计划，才能调用平台发布包。")
            if state["approval"].get("confirmationSource") != "dashboard":
                _error("DASHBOARD_CONFIRMATION_REQUIRED", "本流程只执行用户在看板确认的计划；聊天或旧来源批准不能替代。")
            if state["stage"] == "AWAIT_AGENT":
                state["stage"] = "EXECUTING"
                state["executionOwner"] = "agent"
            unfinished = next((job for job in state["jobs"] if job["state"] != "COMPLETE"), None)
            if unfinished is None:
                state["stage"] = "FINISHED"
                self._save(session_id, state)
                return self._snapshot(state)
            job = unfinished
            if job["state"] == "RUNNING":
                job["state"] = "PAUSED"
                job["result"] = {"status": "UNKNOWN_AFTER_INTERRUPTION", "outcome": "unknown", "message": "上次调用中断，平台可能已收到请求；请人工核对，禁止自动重新发送。"}
                state["stage"] = "PAUSED"
                self._save(session_id, state)
                return self._snapshot(state)
            plan = state["plan"]
            payload = plan["payload"]
            target = next(t for t in payload["targets"] if t["platform"] == job["platform"])
            try:
                if _digest(payload) != plan["hash"] or state["approval"]["planHash"] != plan["hash"] or state["approval"]["sourceHash"] != payload["sourceHash"]:
                    _error("PLAN_CHANGED", "已确认计划或授权回执发生变化。")
                if state["assetRevision"] != payload["assetRevision"]:
                    _error("SOURCE_CHANGED", "执行时素材版本与确认时不一致。")
                if target["capability"] != self.capabilities[target["platform"]]:
                    _error("CAPABILITIES_CHANGED", "平台能力或账号改变，需人工重新确认。")
                self._verify_source(session_id, payload["source"], payload["sourceHash"])
                if target["decision"] == "schedule":
                    self._check_future(datetime.fromisoformat(target["schedule"]["utc"]), target["capability"]["minLeadMinutes"], target["platform"])
            except (CoordinatorError, OSError) as exc:
                job["state"] = "PAUSED"
                job["result"] = {"status": exc.code if isinstance(exc, CoordinatorError) else "SOURCE_UNAVAILABLE", "outcome": "needs_user", "message": str(exc)}
                state["stage"] = "PAUSED"
                self._save(session_id, state)
                return self._snapshot(state)
            schedule = target["schedule"]
            scheduled = target["decision"] == "schedule"
            request = {"sessionId": session_id, "planId": payload["planId"], "planHash": plan["hash"], "jobId": job["jobId"], "platform": target["platform"], "decision": target["decision"], "accountProfile": target["accountProfile"], "accountSettings": copy.deepcopy(target["accountSettings"]), "source": copy.deepcopy(payload["source"]), "scheduledAt": schedule["scheduledAt"] if scheduled else None, "timezone": schedule["timezone"] if scheduled else None, "scheduleUtc": schedule["utc"] if scheduled else None, "draftScope": target["draftScope"], "acceptLocalDraft": target["acceptLocalDraft"], "policies": copy.deepcopy(target["policies"]), "authorization": copy.deepcopy(state["approval"])}
            job["state"] = "RUNNING"
            job["startedAt"] = _iso_now()
            self._save(session_id, state)  # Durable before any external side effect.
        try:
            result = self._adapter_result(executor_callback(copy.deepcopy(request)))
        except Exception:
            # Do not persist arbitrary exception text: it may contain tokens.
            result = {"status": "EXECUTOR_INTERRUPTED", "outcome": "unknown", "message": "执行回调异常，平台结果未知；请人工核对，不自动重试。"}
        with self._locked(session_id):
            state = self._load(session_id)
            job = next((item for item in state["jobs"] if item["jobId"] == request["jobId"]), None)
            if job is None or job["state"] != "RUNNING" or state["stage"] != "EXECUTING" or not state["approval"] or state["approval"]["planHash"] != request["planHash"]:
                _error("STATE_CHANGED_DURING_EXECUTION", "执行过程中状态异常变化；平台可能已执行，必须人工核对，禁止重新提交。")
            job["result"] = result
            job["finishedAt"] = _iso_now()
            job["state"] = "COMPLETE" if result["outcome"] == "success" else "PAUSED"
            if result["outcome"] != "success":
                state["stage"] = "PAUSED"
            elif all(item["state"] == "COMPLETE" for item in state["jobs"]):
                state["stage"] = "FINISHED"
            self._save(session_id, state)
            return self._snapshot(state)
