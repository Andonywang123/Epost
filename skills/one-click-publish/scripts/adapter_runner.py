#!/usr/bin/env python3
"""Legacy connection shim: forward one approved request to its platform skill.

No credential checks, manifest construction, media transformation, platform
preflight, or job directories belong here. New configurations invoke the
platform-owned upstream_dispatch.py directly from the Agent command tool.
This compatibility shim never falls back to doing platform work itself.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

OUTCOMES = {"success", "failed", "unknown", "needs_user", "pending", "partial"}


class Stop(Exception):
    def __init__(self, status: str, message: str, outcome: str = "needs_user"):
        self.status, self.message, self.outcome = status, message, outcome


def forward_request(args: argparse.Namespace, raw: str) -> dict:
    """Forward original JSON verbatim, without interpreting source/settings."""
    try:
        request = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise Stop("INVALID_REQUEST", "交接任务不是有效 JSON，未触发分发包。") from exc
    if (not isinstance(request, dict) or request.get("platform") != args.platform
            or request.get("decision") not in {"draft", "publish", "schedule"}):
        raise Stop("INVALID_REQUEST", "任务目标或操作不匹配，未触发分发包。")
    receipt = request.get("authorization")
    if (not isinstance(receipt, dict) or not receipt.get("receiptId")
            or not request.get("planHash") or receipt.get("planHash") != request["planHash"]):
        raise Stop("CONFIRMATION_REQUIRED", "没有匹配的已确认任务回执。")
    entrypoint = args.skill_dir / "scripts" / "upstream_dispatch.py"
    if (not args.skill_dir.is_absolute() or not (args.skill_dir / "SKILL.md").is_file()
            or not entrypoint.is_file() or not args.output_root.is_absolute()):
        raise Stop("PLATFORM_ENTRY_UNAVAILABLE", "对应分发包尚未提供统一入口，请更新该包；总控不会代替执行。")
    command = [sys.executable, str(entrypoint), "--output-root", str(args.output_root)]
    for flag in ("node", "python"):
        value = getattr(args, flag, None)
        if value:
            command.extend(["--" + flag, value])
    try:
        result = subprocess.run(command, input=raw, shell=False, capture_output=True,
                                text=True, encoding="utf-8", errors="replace")
    except OSError as exc:
        raise Stop("PLATFORM_START_FAILED", "未能触发对应分发包，请检查本机运行环境。") from exc
    try:
        if len(result.stdout) > 2 * 1024 * 1024:
            raise ValueError("oversized receipt")
        receipt = json.loads(result.stdout)
        if (result.returncode or not isinstance(receipt, dict)
                or receipt.get("outcome") not in OUTCOMES or not receipt.get("status")):
            raise ValueError("invalid receipt")
    except (ValueError, TypeError) as exc:
        raise Stop("INVALID_PLATFORM_RECEIPT", "分发包回执无法确认，请核实已有结果，勿重复触发。", "unknown") from exc
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(description="兼容旧连接配置：只转交原任务给平台包")
    parser.add_argument("--platform", choices=("xiaohongshu", "youtube"), required=True)
    parser.add_argument("--skill-dir", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--node")
    parser.add_argument("--python")
    args = parser.parse_args()
    try:
        raw = sys.stdin.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise Stop("INVALID_REQUEST", "交接任务超过大小限制。")
        result = forward_request(args, raw)
    except Stop as exc:
        result = {"status": exc.status, "outcome": exc.outcome, "message": exc.message}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
