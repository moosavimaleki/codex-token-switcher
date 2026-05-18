from __future__ import annotations

import json
import os
import select
import shutil
import subprocess
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Callable

from .errors import ManagerError

AuthRefreshHandler = Callable[[dict[str, Any]], dict[str, Any]]


class CodexAppServer:
    def __init__(
        self,
        codex_home: Path,
        codex_bin: str | None = None,
        auth_refresh_handler: AuthRefreshHandler | None = None,
        proxy_url: str | None = None,
    ) -> None:
        self.codex_home = codex_home
        self.codex_bin = codex_bin or shutil.which("codex")
        if not self.codex_bin:
            raise ManagerError("codex executable not found in PATH")
        self.auth_refresh_handler = auth_refresh_handler
        self.proxy_url = proxy_url
        self._proc: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()
        self._stderr_lines: deque[str] = deque(maxlen=80)
        self._notifications: deque[dict[str, Any]] = deque()
        self._stderr_thread: threading.Thread | None = None

    def __enter__(self) -> "CodexAppServer":
        self.start()
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()

    def start(self) -> None:
        if self._proc is not None:
            return
        env = os.environ.copy()
        env["CODEX_HOME"] = str(self.codex_home)
        env.pop("OPENAI_API_KEY", None)
        env.pop("CODEX_API_KEY", None)
        if self.proxy_url:
            for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
                env[key] = self.proxy_url
        self._proc = subprocess.Popen(
            [self.codex_bin, "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env=env,
            bufsize=1,
        )
        self._start_stderr_drain_thread()

    def close(self) -> None:
        if self._proc is None:
            return
        proc = self._proc
        self._proc = None
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=2)
            except Exception:
                pass
        if self._stderr_thread and self._stderr_thread.is_alive():
            self._stderr_thread.join(timeout=0.5)

    def initialize(self) -> dict[str, Any]:
        result = self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "codex_manager",
                    "title": "Codex Manager",
                    "version": "0.1.0",
                },
                "capabilities": {"experimentalApi": True},
            },
            timeout=30,
        )
        self.notify("initialized")
        return result

    def compact_thread(self, thread_id: str) -> dict[str, Any]:
        return self.request(
            "thread/compact/start",
            {"threadId": thread_id},
            timeout=60,
        )

    def wait_for_compaction(self, thread_id: str, timeout: float) -> dict[str, str]:
        deadline = time.monotonic() + timeout
        started_turn_id: str | None = None
        started_item_id: str | None = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ManagerError(f"timed out waiting for compaction to finish after {int(timeout)}s")
            msg = self.next_notification(timeout=remaining)
            method = msg.get("method")
            params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
            if method == "error":
                message = params.get("message") or params.get("error") or params
                raise ManagerError(f"app-server error during compaction: {message}")
            if params.get("threadId") != thread_id:
                continue
            if method == "thread/compacted":
                return {
                    "thread_id": thread_id,
                    "turn_id": _string(params.get("turnId")) or "",
                    "item_id": "",
                }
            if method == "item/started" and _is_context_compaction(params.get("item")):
                started_turn_id = _string(params.get("turnId"))
                started_item_id = _item_id(params.get("item"))
                continue
            if method == "item/completed" and _is_context_compaction(params.get("item")):
                return {
                    "thread_id": thread_id,
                    "turn_id": _string(params.get("turnId")) or started_turn_id or "",
                    "item_id": _item_id(params.get("item")) or started_item_id or "",
                }
            if method == "turn/completed":
                turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
                if turn.get("status") == "failed":
                    error = turn.get("error") if isinstance(turn.get("error"), dict) else {}
                    message = error.get("message") or error or "unknown failure"
                    raise ManagerError(f"compaction failed: {message}")

    def request(self, method: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        request_id = str(uuid.uuid4())
        self._write({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ManagerError(f"timed out waiting for app-server response to {method}")
            msg = self._read(timeout=remaining)
            if self._handle_server_request(msg):
                continue
            if msg.get("id") != request_id:
                if "method" in msg and "id" not in msg:
                    self._notifications.append(msg)
                continue
            if "error" in msg:
                error = msg.get("error")
                if isinstance(error, dict):
                    code = error.get("code")
                    message = error.get("message") or "unknown app-server error"
                    raise ManagerError(f"{method} failed: {message} (code {code})")
                raise ManagerError(f"{method} failed: {error}")
            result = msg.get("result")
            if isinstance(result, dict):
                return result
            raise ManagerError(f"{method} returned a non-object response")

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._write({"method": method, "params": params or {}})

    def next_notification(self, timeout: float) -> dict[str, Any]:
        if self._notifications:
            return self._notifications.popleft()
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ManagerError("timed out waiting for app-server notification")
            msg = self._read(timeout=remaining)
            if self._handle_server_request(msg):
                continue
            if "method" in msg and "id" not in msg:
                return msg

    def _write(self, payload: dict[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise ManagerError("app-server is not running")
        with self._lock:
            self._proc.stdin.write(json.dumps(payload) + "\n")
            self._proc.stdin.flush()

    def _read(self, timeout: float) -> dict[str, Any]:
        if self._proc is None or self._proc.stdout is None:
            raise ManagerError("app-server is not running")
        ready, _, _ = select.select([self._proc.stdout], [], [], max(0.0, timeout))
        if not ready:
            raise ManagerError("timed out reading from app-server")
        line = self._proc.stdout.readline()
        if not line:
            tail = "\n".join(self._stderr_lines)
            raise ManagerError(f"app-server closed stdout. stderr: {tail[:1600]}")
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ManagerError(f"invalid app-server JSON-RPC line: {line!r}") from exc
        if not isinstance(message, dict):
            raise ManagerError(f"invalid app-server JSON-RPC payload: {message!r}")
        return message

    def _handle_server_request(self, msg: dict[str, Any]) -> bool:
        if "method" not in msg or "id" not in msg:
            return False
        method = msg.get("method")
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        if method == "account/chatgptAuthTokens/refresh" and self.auth_refresh_handler:
            try:
                result = self.auth_refresh_handler(params)
                self._write({"id": msg["id"], "result": result})
            except Exception as exc:
                self._write({"id": msg["id"], "error": {"code": -32000, "message": str(exc)}})
        else:
            self._write({"id": msg["id"], "result": {}})
        return True

    def _start_stderr_drain_thread(self) -> None:
        if self._proc is None or self._proc.stderr is None:
            return

        def drain() -> None:
            stderr = self._proc.stderr if self._proc else None
            if stderr is None:
                return
            for line in stderr:
                self._stderr_lines.append(line.rstrip("\n"))

        self._stderr_thread = threading.Thread(target=drain, daemon=True)
        self._stderr_thread.start()


def _is_context_compaction(item: Any) -> bool:
    return isinstance(item, dict) and item.get("type") == "contextCompaction"


def _item_id(item: Any) -> str | None:
    if isinstance(item, dict):
        return _string(item.get("id"))
    return None


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) else None
