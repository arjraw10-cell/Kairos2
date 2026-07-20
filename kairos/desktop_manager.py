"""Cua Driver lifecycle and command transport for native desktop automation."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .config import Config
from .tools.base import ToolResult


class DesktopManager:
    """Manage a Cua Driver daemon and invoke its JSON CLI tools.

    Cua Driver owns accessibility caches and platform-specific desktop state,
    so calls are routed through one long-lived daemon rather than spawning a
    new driver for every action. The manager is lazy: constructing an Agent
    does not require Cua to be installed, and the first desktop call reports a
    useful installation/permission error if it is unavailable.
    """

    STARTUP_TIMEOUT = 15.0
    CALL_TIMEOUT = 45.0

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._process_lock = threading.Lock()
        self._daemon: Optional[subprocess.Popen] = None
        self._active_process: Optional[subprocess.Popen] = None
        self._closed = False
        self.session_id = f"kairos-{uuid.uuid4().hex}"

    @property
    def is_open(self) -> bool:
        with self._lock:
            return self._daemon is not None and self._daemon.poll() is None

    def _executable(self) -> Optional[str]:
        configured = Config.KAIROS_CUA_DRIVER_PATH()
        if configured:
            path = Path(configured).expanduser()
            if path.is_dir():
                path = path / ("cua-driver.exe" if os.name == "nt" else "cua-driver")
            if path.is_file():
                return str(path)
            return None

        discovered = shutil.which("cua-driver") or shutil.which("cua-driver.exe")
        if discovered:
            return discovered

        # The official Windows installer uses this per-user location. This
        # fallback helps long-lived gateway processes whose inherited PATH was
        # created before the installer updated the user's environment.
        if os.name == "nt":
            default_path = (
                Path(os.environ.get("LOCALAPPDATA", ""))
                / "Programs" / "Cua" / "cua-driver" / "bin" / "cua-driver.exe"
            )
            if default_path.is_file():
                return str(default_path)
        return None

    def _run_client(self, args: list[str], timeout: float = CALL_TIMEOUT) -> Tuple[int, str, str]:
        executable = self._executable()
        if not executable:
            raise FileNotFoundError(
                "Cua Driver was not found. Install it from https://cua.ai/docs/how-to-guides/driver/install "
                "or set KAIROS_CUA_DRIVER_PATH to the cua-driver executable."
            )
        process = subprocess.Popen(
            [executable, *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            **({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {}),
        )
        with self._lock:
            self._active_process = process
        try:
            stdout, stderr = process.communicate(timeout=timeout)
            return process.returncode or 0, stdout.strip(), stderr.strip()
        except subprocess.TimeoutExpired:
            self._terminate_process(process)
            raise TimeoutError(f"Cua Driver operation timed out after {timeout:g} seconds")
        finally:
            with self._lock:
                if self._active_process is process:
                    self._active_process = None

    @staticmethod
    def _terminate_process(process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            else:
                process.terminate()
        except Exception:
            try:
                process.kill()
            except Exception:
                pass

    def _status_ok(self) -> bool:
        try:
            code, stdout, _ = self._run_client(["status"], timeout=5)
            return code == 0 and "not running" not in stdout.lower()
        except Exception:
            return False

    def _ensure_daemon(self) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("Desktop manager is closed")
            if self.is_open:
                return

        if self.is_open or self._status_ok():
            return
        if self.is_open:
            return
        executable = self._executable()
        if not executable:
            # Let _run_client produce the canonical installation message.
            self._run_client(["status"], timeout=5)
            return
        daemon = subprocess.Popen(
            [executable, "serve"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            **({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {}),
        )
        with self._lock:
            self._daemon = daemon
        deadline = time.monotonic() + self.STARTUP_TIMEOUT
        while time.monotonic() < deadline:
            if daemon.poll() is not None:
                stderr = (daemon.stderr.read() if daemon.stderr else "").strip()
                raise RuntimeError(
                    "Cua Driver daemon exited during startup"
                    + (f": {stderr[-2000:]}" if stderr else "")
                )
            if self._status_ok():
                return
            time.sleep(0.25)
        self._terminate_process(daemon)
        with self._lock:
            self._daemon = None
        raise TimeoutError("Cua Driver daemon did not become ready within 15 seconds")

    @staticmethod
    def _decode_response(stdout: str) -> Tuple[str, Optional[str]]:
        """Extract structured Cua output and image content without leaking base64."""
        if not stdout:
            return "", None
        try:
            value: Any = json.loads(stdout)
        except json.JSONDecodeError:
            return stdout, None

        image_url: Optional[str] = None

        def visit(node: Any) -> None:
            nonlocal image_url
            if isinstance(node, dict):
                mime = node.get("mimeType") or node.get("screenshot_mime_type") or "image/png"
                data = node.get("data") or node.get("screenshot_png_b64")
                if isinstance(data, str) and data and (
                    node.get("type") == "image" or "screenshot_png_b64" in node
                ):
                    image_url = f"data:{mime};base64,{data}"
                for child in node.values():
                    visit(child)
            elif isinstance(node, list):
                for child in node:
                    visit(child)

        visit(value)

        def remove_images(node: Any) -> Any:
            if isinstance(node, dict):
                return {
                    key: remove_images(child)
                    for key, child in node.items()
                    if key not in {"screenshot_png_b64", "data"}
                }
            if isinstance(node, list):
                return [remove_images(child) for child in node]
            return node

        cleaned = remove_images(value)
        return json.dumps(cleaned, ensure_ascii=False), image_url

    def doctor(self) -> ToolResult:
        """Run Cua's local prerequisite diagnostic without requiring a daemon."""
        try:
            code, stdout, stderr = self._run_client(["doctor"], timeout=self.CALL_TIMEOUT)
            if code != 0:
                return ToolResult(False, "", stderr or stdout or f"Cua Driver exited with status {code}")
            return ToolResult(True, stdout or "Cua Driver doctor completed.")
        except Exception as exc:
            return ToolResult(False, "", str(exc))

    def call(self, tool_name: str, arguments: Optional[Dict[str, Any]] = None) -> ToolResult:
        """Invoke a Cua tool through the persistent daemon-backed CLI."""
        call_arguments = dict(arguments or {})
        # Cua's session is stable for this Agent/runtime. Do not overwrite an
        # explicit caller session, which is useful when coordinating several
        # desktop tasks deliberately.
        call_arguments.setdefault("session", self.session_id)
        args = json.dumps(call_arguments, ensure_ascii=False, separators=(",", ":"))
        try:
            with self._process_lock:
                self._ensure_daemon()
                code, stdout, stderr = self._run_client(
                    ["call", tool_name, args], timeout=self.CALL_TIMEOUT
                )
            output, image_url = self._decode_response(stdout)
            if code != 0:
                error = stderr or output or f"Cua Driver exited with status {code}"
                return ToolResult(False, "", error)
            return ToolResult(True, output or "Cua Driver completed the operation.", image_url=image_url)
        except Exception as exc:
            return ToolResult(False, "", str(exc))

    def cancel_active_operation(self) -> None:
        with self._lock:
            process = self._active_process
        if process is not None:
            self._terminate_process(process)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            daemon = self._daemon
            self._daemon = None
        self.cancel_active_operation()
        if daemon is not None and daemon.poll() is None:
            try:
                executable = self._executable()
                if executable:
                    subprocess.run([executable, "stop"], timeout=5, check=False,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass
            self._terminate_process(daemon)
            try:
                daemon.wait(timeout=3)
            except Exception:
                pass

    def reset(self) -> None:
        self.close()
        with self._lock:
            self._closed = False
            self.session_id = f"kairos-{uuid.uuid4().hex}"

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass
