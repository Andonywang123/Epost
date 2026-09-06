#!/usr/bin/env python3
"""Portable, single-user storage and approval UI. Never executes publishing.

Only Python's standard library is used. This is a local desktop helper, not a
public web service. Confirmed work is persisted for an independent Agent runner;
refreshing or restarting this service cannot start, pause, or replay that work.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import shutil
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from coordinator import Coordinator
from agent_receiver import receiver_snapshot


PACKAGE_ROOT = Path(__file__).resolve().parent.parent
CHUNK_LIMIT = 4 * 1024 * 1024
JSON_LIMIT = 1024 * 1024
DEFAULT_FILE_LIMIT = 20 * 1024 * 1024 * 1024
MAX_BATCH_FILES = 50
UPLOAD_ID = re.compile(r"^[a-f0-9]{48}$")
SENSITIVE_KEY = re.compile(r"token|secret|password|authorization|cookie|credential|api.?key", re.I)
STATIC_FILES = {
    "/workflow.js": "workflow.js", "/assets/workflow.js": "workflow.js",
    "/": "dashboard.html",
    "/dashboard.html": "dashboard.html",
    "/dashboard.js": "dashboard.js",
    "/dashboard.css": "dashboard.css",
    "/portable-host.js": "portable-host.js",
    "/assets/dashboard.html": "dashboard.html",
    "/assets/dashboard.js": "dashboard.js",
    "/assets/dashboard.css": "dashboard.css",
    "/assets/portable-host.js": "portable-host.js",
}


class RequestError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        self.code, self.message, self.status = code, message, status


def require_text(value, field: str, max_length: int = 4096) -> str:
    if not isinstance(value, str) or not value or len(value) > max_length:
        raise RequestError("INVALID_INPUT", f"{field} 缺失或格式不正确。")
    return value


def write_json_atomic(path: Path, data: dict) -> None:
    temp = path.with_name(path.name + ".tmp-" + secrets.token_hex(6))
    try:
        with temp.open("x", encoding="utf-8") as handle:
            os.chmod(temp, 0o600)
            json.dump(data, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def public_receipt(value, depth: int = 0):
    """Never return subprocess stderr, config, or credential-shaped fields."""
    if depth > 8:
        return "[嵌套内容省略]"
    if isinstance(value, dict):
        return {
            str(k): public_receipt(v, depth + 1)
            for k, v in value.items()
            if not SENSITIVE_KEY.search(str(k)) and str(k) not in {"stdout", "stderr", "traceback", "command", "environment"}
        }
    if isinstance(value, list):
        return [public_receipt(v, depth + 1) for v in value[:100]]
    if isinstance(value, str):
        return value[:8192]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:1024]


def load_configuration(config_path: Path | None):
    registry = json.loads((PACKAGE_ROOT / "assets" / "platform-registry.json").read_text(encoding="utf-8"))
    config = {}
    if config_path:
        if not config_path.is_absolute():
            raise ValueError("--config 必须使用绝对路径。")
        config = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            raise ValueError("本地配置必须是 JSON 对象。")
    entries = config.get("platforms", [])
    if not isinstance(entries, list):
        raise ValueError("本地配置 platforms 必须是数组。")
    registered = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("platform"), str):
            raise ValueError("本地配置含有无效的平台条目。")
        if entry["platform"] in registered:
            raise ValueError("本地配置有重复的平台标识。")
        registered[entry["platform"]] = entry
    adapters, commands = {}, {}
    allowed = {"platform", "label", "skillName", "mediaKinds", "modes", "draftScope", "minLeadMinutes", "reason", "settingsSchema", "preparationPolicies", "requiredPostSettings", "requireAccountConfirmation"}
    for template in registry["platforms"]:
        platform = template["platform"]
        entry = registered.get(platform, {})
        merged = {**template, **entry}
        capability = {k: merged[k] for k in allowed if k in merged}
        accounts = merged.get("accounts", [])
        capability["accounts"] = [
            {"id": a["id"], "label": a.get("label", a["id"]), "settings": a.get("settings", {})}
            for a in accounts
            if isinstance(a, dict) and isinstance(a.get("id"), str) and a["id"]
        ] if isinstance(accounts, list) else []
        command = entry.get("command")
        required_files = entry.get("requiredFiles", [])
        files_valid = isinstance(required_files, list) and all(
            isinstance(path, str) and Path(path).is_absolute() and Path(path).is_file()
            for path in required_files
        )
        command_valid = (
            isinstance(command, list) and bool(command)
            and all(isinstance(arg, str) and "\0" not in arg for arg in command)
            and Path(command[0]).is_absolute()
            and Path(command[0]).is_file() and os.access(command[0], os.X_OK) and files_valid
        )
        capability["available"] = bool(
            entry.get("available") is True and command_valid
            and capability["accounts"] and capability.get("mediaKinds") and capability.get("modes")
        )
        if not capability["available"]:
            capability["reason"] = "未接入：需在本地配置注册可执行发布包、账号、素材类型和动作支持。"
        else:
            capability["reason"] = str(entry.get("reason", "已通过本地配置注册，尚未验证平台登录状态。"))
            commands[platform] = tuple(command)
        adapters[platform] = capability
    local_timezone = config.get("localTimezone")
    if local_timezone is not None and not isinstance(local_timezone, str):
        raise ValueError("localTimezone 必须是 IANA 时区字符串。")
    timeout = config.get("executionTimeoutSeconds", 7200)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 10 <= timeout <= 86400:
        raise ValueError("executionTimeoutSeconds 必须介于 10 和 86400。")
    file_limit = config.get("maxUploadBytes", DEFAULT_FILE_LIMIT)
    if isinstance(file_limit, bool) or not isinstance(file_limit, int) or not 1 <= file_limit <= 100 * 1024 ** 3:
        raise ValueError("maxUploadBytes 必须为有效文件字节上限（最多 100 GiB）。")
    return adapters, commands, local_timezone, timeout, file_limit


class LocalHost:
    def __init__(self, workspace: Path, config_path: Path | None):
        self.workspace = workspace.resolve()
        self.workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.upload_root = self.workspace / "uploads"
        self.upload_root.mkdir(exist_ok=True, mode=0o700)
        # Validate local package connections, but do not retain an executor or
        # a platform command queue in this HTTP process. Only the Agent runner
        # consumes the commands returned by load_configuration.
        self.adapters, _commands, self.local_timezone, _timeout, self.file_limit = load_configuration(config_path)
        self.coordinator = Coordinator(storage_root=self.workspace / "sessions", adapters=self.adapters, local_timezone=self.local_timezone)
        self.csrf = secrets.token_urlsafe(48)
        self.upload_lock = threading.RLock()
        self.session_lock = threading.RLock()
        self.host_state_path = self.workspace / "dashboard-state.json"

    def decorate_snapshot(self, snapshot: dict):
        """Display-only handoff metadata; never infer approval from UI state."""
        result = {**snapshot, "executionMode": "agent"}
        result["agentReceiver"] = receiver_snapshot(self.coordinator, snapshot)
        result.pop("agentHandoff", None)
        if snapshot.get("handoffConfirmed") is True:
            review = snapshot.get("review") or {}
            status = {"AWAIT_AGENT": "waiting", "EXECUTING": "running",
                      "PAUSED": "needs_user", "FINISHED": "complete"}.get(snapshot.get("stage"), "needs_user")
            if status == "waiting":
                message = ("本次许可已保存，等待 Agent 接收器接手。"
                           if result["agentReceiver"]["status"] in {"ready", "busy"}
                           else "本次许可已保存，接收器未就绪；请回到 Agent 检查并启动本批接收器。")
            else:
                message = {
                    "running": "Agent 正在执行已确认任务；刷新看板不会中断。",
                    "needs_user": "任务已暂停或需要人工核实。请回到 Agent 查看原因，勿重复发送。",
                    "complete": "本次已确认任务已完成；无需再次启动。",
                }[status]
            result["agentHandoff"] = {
                "status": status,
                "workspace": str(self.workspace),
                "sessionId": snapshot["sessionId"],
                "planHash": review.get("planHash"),
                "message": message,
            }
        return result

    def snapshot(self, session_id: str):
        return self.decorate_snapshot(self.coordinator.get_snapshot(require_text(session_id, "sessionId", 200)))

    def _active_session(self):
        if self.host_state_path.exists():
            try:
                state = json.loads(self.host_state_path.read_text(encoding="utf-8"))
                return self.snapshot(state["activeSessionId"])
            except (ValueError, KeyError, TypeError, OSError):
                raise RequestError("HOST_STATE_UNREADABLE", "本地会话索引不可读，请人工恢复；不会创建新批次绕开旧任务。", 409)
        # Recover an existing workspace instead of treating a cleared browser
        # cache or missing index as permission to forget an in-flight batch.
        folders = [p for p in (self.workspace / "sessions").iterdir() if p.is_dir() and re.fullmatch(r"[a-f0-9]{32}", p.name)]
        snapshots = [self.snapshot(p.name) for p in folders]
        unfinished = [s for s in snapshots if s.get("stage") != "FINISHED"]
        if len(unfinished) > 1:
            raise RequestError("MULTIPLE_UNFINISHED_SESSIONS", "工作区含有多个未完成会话，需要人工核实；不能新建批次绕过。", 409)
        active = unfinished[0] if unfinished else (self.snapshot(max(folders, key=lambda p: p.stat().st_mtime).name) if folders else None)
        if active:
            write_json_atomic(self.host_state_path, {"activeSessionId": active["sessionId"]})
        return active

    def create_or_resume_session(self, previous_session_id=None):
        with self.session_lock:
            active = self._active_session()
            if previous_session_id is None:
                if active:
                    return active
            else:
                previous_session_id = require_text(previous_session_id, "previousSessionId", 200)
                if not active or active["sessionId"] != previous_session_id:
                    raise RequestError("STALE_SESSION", "当前批次已变化，请刷新看板后继续。", 409)
                if active.get("stage") != "FINISHED":
                    raise RequestError("PREVIOUS_BATCH_UNFINISHED", "上一批次尚未明确完成；执行中、暂停或结果未知时不能新建批次绕开。", 409)
            created = self.coordinator.create_session()
            write_json_atomic(self.host_state_path, {"activeSessionId": created["sessionId"]})
            return self.decorate_snapshot(created)

    def require_active_session(self, session_id: str):
        with self.session_lock:
            active = self._active_session()
            if not active or active["sessionId"] != session_id:
                raise RequestError("STALE_SESSION", "这不是当前素材批次；旧批次仅供查看，请刷新看板。", 409)
            return active

    def upload_record(self, upload_id: str, session_id: str):
        if not isinstance(upload_id, str) or not UPLOAD_ID.fullmatch(upload_id):
            raise RequestError("UPLOAD_NOT_FOUND", "上传记录不存在。", 404)
        folder = self.upload_root / upload_id
        try:
            record = json.loads((folder / "record.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            raise RequestError("UPLOAD_NOT_FOUND", "上传记录不存在或已损坏，请重新上传。", 404)
        if record.get("sessionId") != session_id:
            raise RequestError("UPLOAD_SESSION_MISMATCH", "该文件不属于当前素材会话。", 403)
        return folder, record

    def begin_upload(self, data: dict):
        session_id = require_text(data.get("sessionId"), "sessionId", 200)
        self.require_active_session(session_id)
        size = data.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= self.file_limit:
            raise RequestError("FILE_TOO_LARGE", "文件为空或超过当前本地服务的文件大小上限。", 413)
        purpose = data.get("purpose")
        if purpose not in {"media", "cover"}:
            raise RequestError("INVALID_PURPOSE", "上传必须明确属于主素材或封面。")
        original = require_text(data.get("filename"), "文件名", 1024)
        name = Path(original.replace("\\", "/")).name
        name = re.sub(r'[\x00-\x1f\x7f<>:"|?*]', "_", name).strip(" .")[:180] or "upload"
        # Names are labels only; all on-disk paths are generated by this host.
        suffix = Path(name).suffix.lower()
        suffix = suffix if re.fullmatch(r"\.[a-z0-9]{1,10}", suffix) else ".bin"
        if shutil.disk_usage(self.workspace).free < size * 2 + 64 * 1024 ** 2:
            raise RequestError("INSUFFICIENT_SPACE", "本地空间不足以储存并归档该文件。")
        with self.upload_lock:
            upload_id = secrets.token_hex(24)
            folder = self.upload_root / upload_id
            folder.mkdir(mode=0o700)
            content_folder = folder / "content"
            content_folder.mkdir(mode=0o700)
            # A separate data directory preserves understandable filenames and
            # prevents a file named record.json from replacing upload state.
            payload = content_folder / (name if Path(name).suffix.lower() == suffix else name + suffix)
            with payload.open("xb"):
                pass
            os.chmod(payload, 0o600)
            record = {"uploadId": upload_id, "sessionId": session_id, "filename": name,
                      "storedName": str(payload.relative_to(folder)), "size": size, "offset": 0,
                      "purpose": purpose, "finished": False, "createdAt": time.time()}
            write_json_atomic(folder / "record.json", record)
        return {"uploadId": upload_id, "offset": 0, "chunkBytes": CHUNK_LIMIT}

    def upload_chunk(self, upload_id: str, session_id: str, offset: int, payload: bytes):
        with self.upload_lock:
            folder, record = self.upload_record(upload_id, session_id)
            path = folder / record["storedName"]
            if record["finished"] or offset != record["offset"] or path.stat().st_size != offset:
                raise RequestError("UPLOAD_OFFSET_MISMATCH", "上传已中断或位置不匹配，请重新选择文件上传；不会当作成功。", 409)
            if not payload or len(payload) > CHUNK_LIMIT or offset + len(payload) > record["size"]:
                raise RequestError("INVALID_CHUNK", "文件分块大小不正确。", 413)
            with path.open("ab") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            record["offset"] += len(payload)
            write_json_atomic(folder / "record.json", record)
            return {"uploadId": upload_id, "offset": record["offset"]}

    def finish_upload(self, data: dict):
        self.require_active_session(data.get("sessionId"))
        with self.upload_lock:
            folder, record = self.upload_record(data.get("uploadId"), data.get("sessionId"))
            path = folder / record["storedName"]
            if record["offset"] != record["size"] or path.stat().st_size != record["size"]:
                raise RequestError("INCOMPLETE_UPLOAD", "该文件尚未完整上传，未计入本批素材。", 409)
            record["finished"] = True
            write_json_atomic(folder / "record.json", record)
            return {"uploadId": record["uploadId"], "filename": record["filename"], "size": record["size"], "finished": True}

    def ready_paths(self, session_id: str, upload_ids: list, purpose: str):
        if not isinstance(upload_ids, list) or not 1 <= len(upload_ids) <= MAX_BATCH_FILES or len(set(upload_ids)) != len(upload_ids):
            raise RequestError("INVALID_BATCH", "素材批次为空、重复或文件过多。")
        paths = []
        with self.upload_lock:
            for upload_id in upload_ids:
                folder, record = self.upload_record(upload_id, session_id)
                path = folder / record["storedName"]
                if record.get("purpose") != purpose or not record.get("finished") or path.stat().st_size != record["size"]:
                    raise RequestError("INCOMPLETE_BATCH", "本批文件未全部完成上传，素材不会确认入库。", 409)
                paths.append(path)
        return paths

class Handler(BaseHTTPRequestHandler):
    server_version = "OneClickPublishLocal/1"

    @property
    def app(self) -> LocalHost:
        return self.server.app

    def log_message(self, format, *args):
        return  # Avoid paths, CSRF material, or uploaded metadata in HTTP logs.

    def _check_origin(self, require_csrf: bool = False):
        expected_host = f"127.0.0.1:{self.server.server_port}"
        if self.headers.get("Host") != expected_host:
            raise RequestError("HOST_REJECTED", "请使用服务输出的 127.0.0.1 地址打开看板。", 403)
        origin = self.headers.get("Origin")
        if origin is not None and origin != "http://" + expected_host:
            raise RequestError("ORIGIN_REJECTED", "不接受其他网页来源的请求。", 403)
        if self.headers.get("Sec-Fetch-Site") not in {None, "same-origin", "none"}:
            raise RequestError("CROSS_SITE_REJECTED", "不接受跨站请求。", 403)
        if require_csrf and not secrets.compare_digest(self.headers.get("X-OCP-CSRF", ""), self.app.csrf):
            raise RequestError("CSRF_REJECTED", "会话校验已失效，请刷新本地看板。", 403)

    def _headers(self, status: int, content_type: str, size: int):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' blob: data:; media-src 'self' blob:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
        self.end_headers()

    def _json(self, data, status: int = 200):
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", len(payload))
        self.wfile.write(payload)

    def _length(self, maximum: int):
        if self.headers.get("Transfer-Encoding"):
            raise RequestError("TRANSFER_ENCODING_REJECTED", "请使用固定长度文件分块。", 400)
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            raise RequestError("LENGTH_REQUIRED", "请求缺少有效长度。", 411)
        if not 0 <= length <= maximum:
            raise RequestError("REQUEST_TOO_LARGE", "请求超过本地服务大小限制。", 413)
        return length

    def _body(self):
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/json":
            raise RequestError("JSON_REQUIRED", "该接口只接受 JSON。", 415)
        raw = self.rfile.read(self._length(JSON_LIMIT))
        try:
            data = json.loads(raw)
        except (UnicodeDecodeError, ValueError):
            raise RequestError("INVALID_JSON", "请求 JSON 不正确。")
        if not isinstance(data, dict):
            raise RequestError("INVALID_JSON", "请求必须是 JSON 对象。")
        return data

    def _handle_error(self, error):
        if isinstance(error, RequestError):
            self._json({"ok": False, "error": {"code": error.code, "message": error.message}}, error.status)
            return
        # Coordinator errors are user-facing and must not expose Python traces
        # or arbitrary subprocess output. Only simple declared fields survive.
        code = getattr(error, "code", None)
        message = getattr(error, "message", None)
        if isinstance(code, str) and isinstance(message, str):
            self._json({"ok": False, "error": {"code": code[:100], "message": message[:1500]}}, 409)
        else:
            self._json({"ok": False, "error": {"code": "LOCAL_OPERATION_FAILED", "message": "本地操作未完成，请检查输入与当前阶段。未自动重试或调用其他平台。"}}, 400)

    def do_GET(self):
        try:
            self._check_origin()
            parsed = urlsplit(self.path)
            if parsed.path == "/api/bootstrap":
                return self._json({"protocolVersion": "1", "executionMode": "agent", "csrfToken": self.app.csrf,
                                   "localTimezone": self.app.local_timezone,
                                   "capabilities": list(self.app.adapters.values()),
                                   "maxUploadBytes": self.app.file_limit, "chunkBytes": CHUNK_LIMIT})
            if parsed.path == "/api/session":
                self._check_origin(require_csrf=True)
                session_id = parse_qs(parsed.query).get("id", [""])[0]
                return self._json(self.app.snapshot(session_id))
            if parsed.path not in STATIC_FILES:
                raise RequestError("NOT_FOUND", "页面或接口不存在。", 404)
            path = PACKAGE_ROOT / "assets" / STATIC_FILES[parsed.path]
            if not path.is_file():
                raise RequestError("ASSET_MISSING", "看板文件缺失，请检查技能包是否完整。", 404)
            payload = path.read_bytes()
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            self._headers(200, mime + ("; charset=utf-8" if mime.startswith("text/") or mime == "application/javascript" else ""), len(payload))
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as error:
            self._handle_error(error)

    def do_POST(self):
        try:
            self._check_origin(require_csrf=True)
            path = urlsplit(self.path).path
            data = self._body()
            if path == "/api/session/create":
                return self._json(self.app.create_or_resume_session(data.get("previousSessionId")))
            if path == "/api/uploads/begin":
                return self._json(self.app.begin_upload(data))
            if path == "/api/uploads/finish":
                return self._json(self.app.finish_upload(data))
            session_id = require_text(data.get("sessionId"), "sessionId", 200)
            self.app.require_active_session(session_id)
            if path == "/api/materials/kind":
                return self._json(self.app.decorate_snapshot(self.app.coordinator.select_kind(session_id, data.get("kind"), data.get("assetRevision"))))
            if path == "/api/materials/media":
                paths = self.app.ready_paths(session_id, data.get("uploadIds"), "media")
                return self._json(self.app.decorate_snapshot(self.app.coordinator.store_media(session_id, data.get("kind"), paths)))
            if path == "/api/materials/metadata":
                cover_id = data.get("coverUploadId")
                cover = self.app.ready_paths(session_id, [cover_id], "cover")[0] if cover_id else None
                return self._json(self.app.decorate_snapshot(self.app.coordinator.store_metadata(session_id, cover, data.get("coverAbsent"), data.get("title"), data.get("body"), keep_cover_revision=data.get("keepCoverRevision"))))
            if path == "/api/materials/confirm":
                if data.get("confirmed") is not True:
                    raise RequestError("MATERIAL_CONFIRMATION_REQUIRED", "只有明确确认素材正确后才能继续。")
                return self._json(self.app.decorate_snapshot(self.app.coordinator.confirm_materials(session_id, data.get("assetRevision"))))
            if path == "/api/plan/prepare":
                return self._json(self.app.coordinator.prepare_plan(session_id, data.get("assetRevision"), data.get("rows")))
            if path == "/api/plan/confirm":
                confirmed = data.get("confirmed")
                if not isinstance(confirmed, bool):
                    raise RequestError("CONFIRMATION_REQUIRED", "请选择是或否。")
                result = self.app.coordinator.confirm_plan(
                    session_id, data.get("planId"), data.get("planHash"), confirmed,
                    trusted_user_event_id="local-browser:" + secrets.token_urlsafe(32),
                    confirmation_source="dashboard",
                )
                # Persist authorization only. An Agent application's independent
                # runner must explicitly take over this exact confirmed plan.
                return self._json(self.app.decorate_snapshot(result))
            raise RequestError("NOT_FOUND", "接口不存在。", 404)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as error:
            self._handle_error(error)

    def do_PUT(self):
        try:
            self._check_origin(require_csrf=True)
            parsed = urlsplit(self.path)
            if parsed.path != "/api/uploads/chunk":
                raise RequestError("NOT_FOUND", "接口不存在。", 404)
            if self.headers.get("Content-Type", "") != "application/octet-stream":
                raise RequestError("RAW_CHUNK_REQUIRED", "上传分块必须使用原始字节。", 415)
            params = parse_qs(parsed.query)
            upload_id = params.get("id", [""])[0]
            session_id = params.get("sessionId", [""])[0]
            self.app.require_active_session(session_id)
            try:
                offset = int(self.headers.get("X-Upload-Offset", ""))
            except ValueError:
                raise RequestError("INVALID_OFFSET", "上传位置不正确。")
            length = self._length(CHUNK_LIMIT)
            payload = self.rfile.read(length)
            if len(payload) != length:
                raise RequestError("INCOMPLETE_CHUNK", "文件分块传输中断，请重新上传。", 409)
            return self._json(self.app.upload_chunk(upload_id, session_id, offset, payload))
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as error:
            self._handle_error(error)

    def do_OPTIONS(self):
        self._json({"ok": False, "error": {"code": "CORS_DISABLED", "message": "本地服务不开放跨域访问。"}}, 405)


def main():
    parser = argparse.ArgumentParser(description="启动跨 Agent 的本地一键发布看板（仅 127.0.0.1）。")
    parser.add_argument("--workspace", required=True, type=Path, help="会话与素材储存的绝对目录")
    parser.add_argument("--config", type=Path, help="可信本地平台配置 JSON 的绝对路径；省略时尝试工作目录内 config.local.json")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if not args.workspace.is_absolute():
        parser.error("--workspace 必须使用绝对路径。")
    if not 1024 <= args.port <= 65535:
        parser.error("--port 必须介于 1024 和 65535。")
    if args.config is None and (args.workspace / "config.local.json").is_file():
        args.config = args.workspace / "config.local.json"
    try:
        app = LocalHost(args.workspace.resolve(), args.config)
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
        server.app = app
        server.daemon_threads = True
    except (OSError, ValueError, TypeError, KeyError):
        print("启动失败：请检查工作目录、本地配置、Python 版本或端口占用。未运行任何平台发布包。", file=sys.stderr)
        return 1
    print(f"本地看板：http://127.0.0.1:{args.port}/", flush=True)
    print("仅本机单用户使用；看板只储存素材与确认计划，不执行平台发布。", flush=True)
    print("接收器就绪时，看板最终确认后由 Agent 自动接收；未连接时请先回 Agent 启动本批接收器。", flush=True)
    print("刷新、关闭或重启看板不会中断独立的 Agent 执行。", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("本地看板已停止。已保存的素材和确认仍保留；Agent 任务不由此服务启动或停止。", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
