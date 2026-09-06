#!/usr/bin/env python3
"""Strict stdin/stdout bridge for function-calling models."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
MANIFEST_SCRIPT = ROOT / "youtube_manifest.py"
PUBLISH_SCRIPT = ROOT / "youtube_publish.py"
ALLOWED_KEYS = {"command", "decision", "manifest", "profile", "client_secrets", "commit"}


def reject(message: str) -> None:
    print(json.dumps({"event": "error", "message": message}, ensure_ascii=False))
    raise SystemExit(2)


def absolute_file(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        reject(f"{field} is required")
    path = Path(value).expanduser()
    if not path.is_absolute() or not path.is_file():
        reject(f"{field} must be an existing absolute file path")
    return str(path.resolve())


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        reject(f"invalid JSON input: {exc}")
    if not isinstance(payload, dict):
        reject("input must be a JSON object")
    unknown = sorted(set(payload) - ALLOWED_KEYS)
    if unknown:
        reject(f"unknown fields: {', '.join(unknown)}")
    command = payload.get("command")
    if command not in {
        "validate", "dry_run", "auth", "preflight", "draft", "publish", "schedule",
        "dispatch", "upload", "disconnect",
    }:
        reject("unsupported command")
    decision = payload.get("decision")
    if command == "dispatch" and decision not in {"draft", "publish", "schedule"}:
        reject("dispatch requires decision=draft, decision=publish, or decision=schedule")
    if command != "dispatch" and decision is not None:
        reject("decision is valid only for dispatch")
    commit = payload.get("commit", False)
    if not isinstance(commit, bool):
        reject("commit must be boolean")
    mutating = {"draft", "publish", "schedule", "dispatch", "upload"}
    if command in mutating and not commit:
        reject(f"{command} requires commit=true")
    if command not in mutating and commit:
        reject("commit=true is valid only for draft, publish, schedule, dispatch, or legacy upload")

    if command in {"validate", "dry_run", "draft", "publish", "schedule", "dispatch", "upload"}:
        manifest = absolute_file(payload.get("manifest"), "manifest")
        mapped = "dry-run" if command == "dry_run" else command
        argv = [sys.executable, str(MANIFEST_SCRIPT), mapped, "--manifest", manifest]
        if command == "dispatch":
            argv.extend(["--decision", str(decision)])
        if command in mutating:
            argv.append("--commit")
    else:
        profile = payload.get("profile")
        if not isinstance(profile, str) or not profile.strip():
            reject("profile is required")
        argv = [sys.executable, str(PUBLISH_SCRIPT), command]
        if command == "auth":
            argv.extend(["--client-secrets", absolute_file(payload.get("client_secrets"), "client_secrets")])
        argv.extend(["--profile", profile])

    completed = subprocess.run(argv)
    raise SystemExit(completed.returncode)


if __name__ == "__main__":
    main()
