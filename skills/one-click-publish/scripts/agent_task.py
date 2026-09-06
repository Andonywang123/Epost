#!/usr/bin/env python3
"""Agent-side intake and platform triggering, never started by the HTTP service.

The durable consent record is the source of truth, not text copied from a page.
Only the owning Agent, with local tool permission, invokes this CLI. Reading a
task never executes it; execute requires its exact confirmed plan hash. Existing
PAUSED/RUNNING attempts are never retried automatically.
Platform-owned entry points handle all business preparation and publishing;
this module only forwards the frozen request and collects the result.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
from pathlib import Path
from typing import Callable

from coordinator import CoordinatorError, OUTCOMES
from run_dashboard import LocalHost, RequestError, load_configuration, public_receipt
from agent_receiver import ReceiverLease


def context(workspace: Path, config: Path | None) -> LocalHost:
    if not workspace.is_absolute() or not workspace.is_dir():
        raise ValueError("请提供看板使用的现有工作目录绝对路径。")
    config = config or (workspace / "config.local.json")
    config_path = config if config.is_file() else None
    host = LocalHost(workspace, config_path)
    # Execution configuration exists only in this Agent process. LocalHost
    # intentionally has no commands/timeout/executor on the HTTP side.
    _, host.commands, _, host.timeout, _ = load_configuration(config_path)
    return host


def execute_command(host: LocalHost, request: dict) -> dict:
    """Trigger one platform entry with frozen data; no platform preparation here."""
    command = host.commands.get(request["platform"])
    if not command:
        return {"status": "ADAPTER_UNAVAILABLE", "outcome": "needs_user", "message": "Agent 未找到已注册的对应发布包，未执行。"}
    try:
        completed = subprocess.run(
            list(command), input=json.dumps(request, ensure_ascii=False),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            shell=False, timeout=host.timeout, cwd=str(host.workspace),
        )
    except subprocess.TimeoutExpired:
        return {"status": "ADAPTER_TIMEOUT", "outcome": "unknown", "message": "Agent 执行发布包超时，先核实平台结果，不能自动重发。"}
    except (OSError, ValueError):
        return {"status": "ADAPTER_START_FAILED", "outcome": "unknown", "message": "Agent 执行环境异常，请核实，不自动重试。"}
    if completed.returncode or len(completed.stdout) > 2 * 1024 ** 2:
        return {"status": "ADAPTER_ABNORMAL_EXIT", "outcome": "unknown", "message": "发布包没有正常返回，请人工核实结果。"}
    try:
        result = json.loads(completed.stdout)
    except (ValueError, TypeError):
        result = None
    if not isinstance(result, dict) or result.get("outcome") not in OUTCOMES:
        return {"status": "INVALID_ADAPTER_RECEIPT", "outcome": "unknown", "message": "发布包回执无效，不能假定失败后重复发送。"}
    return public_receipt(result)


def execute_task(host: LocalHost, session_id: str, plan_hash: str,
                 executor: Callable[[dict], dict] | None = None) -> dict:
    """Consume a persisted, confirmed task. No caller-created authorization."""
    host.require_active_session(session_id)
    task = host.coordinator.get_agent_task(session_id)
    if not re.fullmatch(r"[a-f0-9]{64}", plan_hash or "") or task["planHash"] != plan_hash:
        raise CoordinatorError("STALE_PLAN", "请重新读取当前任务，不能执行旧计划。")
    # An active Agent may already be doing slow translation or uploading.
    # A second invocation only reports that state, never marks it interrupted.
    if task["stage"] in {"PAUSED", "FINISHED"}:
        return host.snapshot(session_id)
    if task.get("confirmationSource") != "dashboard":
        raise CoordinatorError("DASHBOARD_CONFIRMATION_REQUIRED", "只能执行用户在看板真实确认的计划；聊天批准不能替代。")
    if any(job["state"] == "RUNNING" for job in task["jobs"]):
        host.coordinator.reconcile_agent_execution(session_id, plan_hash)
        return host.snapshot(session_id)
    if task["stage"] not in {"AWAIT_AGENT", "EXECUTING"}:
        raise CoordinatorError("NOT_AUTHORIZED", "任务未等待 Agent 接手，未执行。")
    callback = executor or (lambda request: execute_command(host, request))
    while True:
        snapshot = host.coordinator.run_next(session_id, callback, expected_plan_hash=plan_hash)
        # If another process owns the worker lock, do not spin or take over.
        if snapshot["stage"] != "EXECUTING" or any(r.get("status") == "RUNNING" for r in snapshot["results"]):
            return host.snapshot(session_id)


def receive_task(host: LocalHost, session_id: str, asset_revision: int, timeout: int,
                 executor: Callable[[dict], dict] | None = None, *,
                 clock=time.monotonic, sleeper=time.sleep) -> dict:
    """Wait for this exact batch's dashboard consent, then consume it once.

    The wait timeout does not turn into a platform schedule. HTTP never calls
    this function. The Agent's normal long-task tool owns its process lifetime.
    Injected clock/sleeper/executor are solely for isolated deterministic tests.
    """
    if not isinstance(session_id, str) or not re.fullmatch(r"[a-f0-9]{32}", session_id):
        raise CoordinatorError("SESSION_REQUIRED", "接收器必须明确指定当前素材会话。")
    if type(asset_revision) is not int or asset_revision <= 0:
        raise CoordinatorError("ASSET_REVISION_REQUIRED", "接收器必须绑定已收齐素材的准确版本。")
    if type(timeout) is not int or not 1 <= timeout <= 3600:
        raise CoordinatorError("INVALID_RECEIVER_TIMEOUT", "接收器等待时限必须为 1 到 3600 秒。")

    def current():
        host.require_active_session(session_id)
        snapshot = host.coordinator.get_snapshot(session_id)
        if snapshot["assetRevision"] != asset_revision:
            raise CoordinatorError("RECEIVER_MATERIALS_CHANGED", "素材版本已修改，旧接收器已停止；请由 Agent 为新版本重新接收。")
        if not snapshot.get("media") or not snapshot.get("metadata"):
            raise CoordinatorError("MATERIALS_INCOMPLETE", "请先在聊天收齐主素材、封面与文案，再启动接收器。")
        if snapshot["stage"] in {"EXECUTING", "PAUSED", "FINISHED"}:
            raise CoordinatorError("RECEIVER_TASK_ALREADY_STARTED", "此任务已执行或需要核实；接收器不会再次接手或重发。")
        if snapshot["stage"] not in {"AWAIT_MATERIAL_CONFIRMATION", "CONFIGURING", "AWAIT_PLAN_CONFIRMATION", "AWAIT_AGENT"}:
            raise CoordinatorError("RECEIVER_WRONG_STAGE", "当前阶段不能等待看板确认。")
        return snapshot

    current()
    deadline = clock() + timeout
    with ReceiverLease(host.coordinator, session_id, asset_revision) as lease:
        while True:
            if clock() >= deadline:
                raise CoordinatorError("RECEIVER_TIMEOUT", "等待看板确认已超时，未调用平台；由 Agent 重新启动接收器后再继续。")
            snapshot = current()
            if lease.failed.is_set():
                raise CoordinatorError("RECEIVER_HEARTBEAT_FAILED", "接收器状态无法可靠保存，已停止等待，未调用平台。")
            if snapshot["stage"] == "AWAIT_AGENT":
                task = host.coordinator.get_agent_task(session_id)
                if task.get("confirmationSource") != "dashboard":
                    raise CoordinatorError("DASHBOARD_CONFIRMATION_REQUIRED", "该许可不是来自看板，接收器未调用平台。")
                if task["payload"]["assetRevision"] != asset_revision:
                    raise CoordinatorError("RECEIVER_MATERIALS_CHANGED", "已批准的素材版本与接收器不同，未执行。")
                if task["stage"] != "AWAIT_AGENT" or any(job["state"] != "QUEUED" for job in task["jobs"]):
                    raise CoordinatorError("RECEIVER_TASK_ALREADY_STARTED", "任务已有执行记录，接收器不会重发。")
                # These are the persisted exact selections from the dashboard;
                # no model, transformation, new plan, or caller-defined rows.
                lease.busy()
                execute_task(host, session_id, task["planHash"], executor)
                break
            sleeper(min(1.0, max(0.0, deadline - clock())))
    return host.snapshot(session_id)


def main() -> None:
    parser = argparse.ArgumentParser(description="由 Agent 接手已确认任务；看板本身不发布")
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--session")
    subs = parser.add_subparsers(dest="action", required=True)
    subs.add_parser("status", help="只读当前收件、确认或执行状态")
    subs.add_parser("inspect", help="只读已确认计划、素材位置和执行状态")
    execute = subs.add_parser("execute", help="Agent 按已确认计划触发平台包并收集回执，不重放暂停任务")
    execute.add_argument("--plan-hash", required=True)
    receive = subs.add_parser("receive", help="Agent 一次性接收当前版本的看板确认并执行；网页不启动")
    receive.add_argument("--asset-revision", type=int, required=True)
    receive.add_argument("--timeout", type=int, required=True, help="等待真实看板许可的秒数，1 到 3600")
    media = subs.add_parser("import-media", help="只保存用户直接交给 Agent 的第一批附件")
    media.add_argument("--kind", required=True, choices=("image_post", "video"))
    media.add_argument("--asset-revision", type=int, required=True)
    media.add_argument("--files", type=Path, nargs="+", required=True)
    metadata = subs.add_parser("import-metadata", help="只保存第二批封面及原文")
    metadata.add_argument("--title-file", type=Path, required=True)
    metadata.add_argument("--body-file", type=Path, required=True)
    metadata.add_argument("--asset-revision", type=int, required=True)
    cover = metadata.add_mutually_exclusive_group(required=True)
    cover.add_argument("--cover", type=Path)
    cover.add_argument("--no-cover", action="store_true")
    cover.add_argument("--keep-cover", action="store_true")
    args = parser.parse_args()
    if args.action == "receive" and not args.session:
        parser.error("receive 必须明确提供 --session，不自动选择会话。")
    try:
        host = context(args.workspace, args.config)
        snapshot = host.create_or_resume_session()
        session_id = args.session or snapshot["sessionId"]
        host.require_active_session(session_id)
        if args.action == "status":
            result = host.snapshot(session_id)
        elif args.action == "inspect":
            result = host.coordinator.get_agent_task(session_id)
        elif args.action == "execute":
            result = execute_task(host, session_id, args.plan_hash)
        elif args.action == "receive":
            result = receive_task(host, session_id, args.asset_revision, args.timeout)
        else:
            if snapshot["assetRevision"] != args.asset_revision:
                raise CoordinatorError("STALE_MATERIALS", "素材已变更，请先读取当前版本。")
            if args.action == "import-media":
                host.coordinator.select_kind(session_id, args.kind, args.asset_revision)
                result = host.coordinator.store_media(session_id, args.kind, args.files)
            else:
                for path in (args.title_file, args.body_file):
                    if not path.is_absolute() or not path.is_file():
                        raise ValueError("标题与正文必须使用 Agent 已保存的 UTF-8 文本文件绝对路径。")
                result = host.coordinator.store_metadata(
                    session_id, args.cover, args.no_cover,
                    args.title_file.read_text(encoding="utf-8-sig"), args.body_file.read_text(encoding="utf-8-sig"),
                    keep_cover_revision=args.asset_revision if args.keep_cover else None,
                )
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False))
    except (CoordinatorError, RequestError, ValueError, OSError) as exc:
        # Do not expose exception payloads that could contain uploaded text.
        code = exc.code if isinstance(exc, (CoordinatorError, RequestError)) else "AGENT_TASK_UNAVAILABLE"
        message = exc.message if isinstance(exc, (CoordinatorError, RequestError)) else "任务或本地素材不可读，请核对工作目录与文件。"
        print(json.dumps({"ok": False, "error": {"code": code, "message": message}}, ensure_ascii=False))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
