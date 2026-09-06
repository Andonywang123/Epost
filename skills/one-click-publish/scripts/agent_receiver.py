"""Session-scoped receiver liveness; importing/reading never starts a worker.

The Agent process owns the lease and heartbeat. HTTP only reads its status and
checks the process-held lock. A stale file is not evidence of a live receiver.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import secrets
import tempfile
import threading
import time

from coordinator import CoordinatorError, _secure_path, fcntl, msvcrt


HEARTBEAT_SECONDS = 1.0
HEARTBEAT_TTL = 5.0


@contextmanager
def receiver_lock(coordinator, session_id: str, *, create=False):
    path = _secure_path(coordinator._folder(session_id) / "receiver.lock")
    try:
        fd = os.open(path, os.O_RDWR | (os.O_CREAT if create else 0) | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except FileNotFoundError:
        yield None
        return
    with os.fdopen(fd, "a+b") as handle:
        acquired = False
        try:
            if fcntl is not None:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except BlockingIOError:
                    pass
            elif msvcrt is not None:
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    if not create:
                        yield None
                        return
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    acquired = True
                except OSError:
                    pass
            else:
                raise CoordinatorError("LOCKING_UNSUPPORTED", "本机不支持安全接收器锁，未启动。")
            yield acquired
        finally:
            if acquired:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                else:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def process_alive(pid):
    """True/False when known; None when the OS denies the liveness probe.

    EPERM is not ESRCH. Restricted Agent processes may be unable to signal
    another process even with signal zero. Unknown process status is reported
    as unverifiable, not as a stopped process or a verified ready receiver.
    """
    if type(pid) is not int or pid <= 0:
        return False
    if os.name == "nt":
        # Do not use os.kill(pid, 0) on Windows: unsupported signals may kill.
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return None if ctypes.get_last_error() == 5 else False
        try:
            code = wintypes.DWORD()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return None
    except (OSError, OverflowError):
        return False


def receiver_snapshot(coordinator, snapshot: dict) -> dict:
    """Read-only proof of a recent heartbeat *and* an owned process lock."""
    session_id, revision = snapshot["sessionId"], snapshot["assetRevision"]
    result = {"status": "offline", "sessionId": session_id, "assetRevision": revision,
              "heartbeatAt": None, "expiresAt": None, "reason": "not_connected"}
    try:
        path = _secure_path(coordinator._folder(session_id) / "receiver-state.json", require_file=True)
        record = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(record, dict):
            return result
        result["assetRevision"] = record.get("assetRevision")
        now, heartbeat = time.time(), record.get("heartbeatAt")
        if (type(heartbeat) not in (int, float) or not math.isfinite(heartbeat)
                or not -1 <= now - heartbeat <= HEARTBEAT_TTL):
            result["reason"] = "expired"
            return result
        result.update(heartbeatAt=heartbeat, expiresAt=heartbeat + HEARTBEAT_TTL)
        if record.get("sessionId") != session_id or record.get("assetRevision") != revision:
            result["reason"] = "materials_changed"
            return result
        if (record.get("active") is not True or record.get("status") not in {"ready", "busy"}
                or snapshot.get("stage") in {"PAUSED", "FINISHED"}):
            return result
        process_status = process_alive(record.get("pid"))
        if process_status is None:
            result["reason"] = "process_unverifiable"
            return result
        if process_status is False:
            result["reason"] = "process_stopped"
            return result
        with receiver_lock(coordinator, session_id) as available:
            if available is not False:
                result["reason"] = "not_owned"
                return result
        result["status"] = record["status"]
        result["reason"] = "connected"
        result["livenessEvidence"] = {"heartbeat": "recent", "lock": "held", "process": "verified"}
        return result
    except (CoordinatorError, OSError, ValueError, TypeError):
        return result


class ReceiverLease:
    """Held only by an Agent-initiated one-shot receiver, never by HTTP."""
    def __init__(self, coordinator, session_id: str, asset_revision: int):
        self.coordinator, self.session_id = coordinator, session_id
        self.path = _secure_path(coordinator._folder(session_id) / "receiver-state.json")
        self.record = {"sessionId": session_id, "assetRevision": asset_revision,
                       "receiverId": secrets.token_hex(16), "pid": os.getpid(),
                       "active": True, "status": "ready"}
        self.mutex = threading.Lock()
        self.stopped = threading.Event()
        self.failed = threading.Event()
        self.thread = None
        self.lock = None

    def _write(self):
        self.record["heartbeatAt"] = time.time()
        self.record["expiresAt"] = self.record["heartbeatAt"] + HEARTBEAT_TTL
        fd, temporary = tempfile.mkstemp(prefix=".receiver-", suffix=".json", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(self.record, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _heartbeat(self):
        while not self.stopped.wait(HEARTBEAT_SECONDS):
            try:
                with self.mutex:
                    self._write()
            except (OSError, ValueError):
                self.failed.set()
                return

    def __enter__(self):
        self.lock = receiver_lock(self.coordinator, self.session_id, create=True)
        acquired = self.lock.__enter__()
        if acquired is not True:
            self.lock.__exit__(None, None, None)
            self.lock = None
            raise CoordinatorError("RECEIVER_ALREADY_ACTIVE", "本批素材已有接收器，未启动第二个，也不会重发。")
        try:
            with self.mutex:
                self._write()
            self.thread = threading.Thread(target=self._heartbeat, name="agent-receiver-heartbeat", daemon=True)
            self.thread.start()
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def busy(self):
        with self.mutex:
            self.record["status"] = "busy"
            self._write()

    def __exit__(self, exc_type, exc, traceback):
        self.stopped.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2)
        try:
            with self.mutex:
                self.record.update(active=False, status="offline")
                try:
                    self._write()
                except OSError:
                    pass  # Released lock still prevents stale files appearing online.
        finally:
            if self.lock:
                self.lock.__exit__(exc_type, exc, traceback)
                self.lock = None
