#!/usr/bin/env python3
"""Minimal MCP stdio server exposing bounded Grok Build delegation tools."""

from __future__ import annotations

import json
import os
import queue
import select
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

from grok_build_bridge import (  # noqa: E402
    BridgeError,
    DEFAULT_AWAIT_SECONDS,
    DEFAULT_AWAIT_RESULT_CHARS,
    DEFAULT_MAX_TURNS,
    DEFAULT_OUTPUT_CHARS,
    DEFAULT_RESULT_PAGE_CHARS,
    DEFAULT_SETUP_TIMEOUT_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    JobManager,
    MAX_AGENT_TURNS,
    MAX_AWAIT_SECONDS,
    MAX_SETUP_TIMEOUT_SECONDS,
    MIN_AWAIT_SECONDS,
    READ_ONLY_MODES,
    _redact_public_value,
    begin_bridge_shutdown,
    setup_grok,
)


SERVER_NAME = "call-grok-build"


def _manifest_version() -> str:
    try:
        payload = json.loads(
            (PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text(
                encoding="utf-8"
            )
        )
    except (OSError, json.JSONDecodeError):
        return "unknown"
    version = payload.get("version") if isinstance(payload, dict) else None
    return version if isinstance(version, str) and version else "unknown"


SERVER_VERSION = _manifest_version()
MCP_PROTOCOL_VERSION = "2025-06-18"
MAX_MCP_LINE_BYTES = 2_000_000
MANAGER = JobManager(max_workers=2)
MAX_BLOCKING_MCP_WORKERS = 2
MAX_BLOCKING_MCP_REQUESTS = 4
MAX_MCP_WRITE_QUEUE = 8
MCP_WRITE_PUT_TIMEOUT_SECONDS = 2.0
MCP_WRITE_HARD_TIMEOUT_SECONDS = 2.0
MCP_STDIN_POLL_SECONDS = 0.2
BLOCKING_TOOLS = {"setup", "await_result"}
_BLOCKING_GATE = threading.BoundedSemaphore(MAX_BLOCKING_MCP_REQUESTS)
_BLOCKING_POOL = ThreadPoolExecutor(
    max_workers=MAX_BLOCKING_MCP_WORKERS, thread_name_prefix="mcp-block"
)
_WRITER: Optional["BoundedStdoutWriter"] = None
_FALLBACK_WRITE_LOCK = threading.Lock()


class BoundedStdoutWriter:
    """One bounded writer thread so request workers cannot stall forever on stdout.

    The MCP host normally gives us a pipe.  TextIOWrapper writes to a full pipe
    can block while holding its internal lock, and closing that wrapper from a
    different thread can then deadlock both the writer and the server shutdown
    path.  When a file descriptor is available, write through a non-blocking FD
    and enforce a deadline.  The generic stream fallback remains best effort,
    but its close is isolated in a daemon thread so shutdown is still bounded.
    """

    def __init__(
        self,
        stream: Any,
        *,
        max_queue: int = MAX_MCP_WRITE_QUEUE,
        put_timeout: float = MCP_WRITE_PUT_TIMEOUT_SECONDS,
    ) -> None:
        self._stream = stream
        self._queue: queue.Queue[Optional[str]] = queue.Queue(maxsize=max_queue)
        self._put_timeout = put_timeout
        self.failed = threading.Event()
        self._closed = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._start_lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._close_started = False
        self._fd: Optional[int] = None
        self._fd_was_blocking: Optional[bool] = None
        try:
            candidate_fd = int(stream.fileno())
            was_blocking = os.get_blocking(candidate_fd)
            os.set_blocking(candidate_fd, False)
        except (AttributeError, OSError, TypeError, ValueError):
            candidate_fd = None
            was_blocking = None
        self._fd = candidate_fd
        self._fd_was_blocking = was_blocking

    def start(self) -> None:
        with self._start_lock:
            if self._thread is not None:
                return
            self._thread = threading.Thread(
                target=self._run, name="mcp-stdout", daemon=True
            )
            self._thread.start()

    def emit(self, response: Optional[Dict[str, Any]]) -> None:
        if response is None or self.failed.is_set() or self._closed.is_set():
            return
        encoded = json.dumps(
            _redact_public_value(response),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        try:
            self._queue.put(encoded, timeout=self._put_timeout)
        except queue.Full:
            self.fail_closed()

    def fail_closed(self) -> None:
        if self.failed.is_set():
            return
        self.failed.set()
        self._closed.set()
        # Never close a real TextIOWrapper here: it can wait on the same lock
        # held by a blocked write.  A non-FD test/custom stream gets an isolated
        # best-effort close so a cooperative stream can release its writer.
        if self._fd is None:
            with self._close_lock:
                if self._close_started:
                    return
                self._close_started = True
            threading.Thread(
                target=self._close_fallback_stream,
                name="mcp-stdout-close",
                daemon=True,
            ).start()

    def _close_fallback_stream(self) -> None:
        try:
            self._stream.close()
        except Exception:
            pass

    def _write_fd(self, item: str) -> bool:
        fd = self._fd
        if fd is None:
            return False
        payload = (item + "\n").encode("utf-8")
        pending = memoryview(payload)
        deadline = time.monotonic() + MCP_WRITE_HARD_TIMEOUT_SECONDS
        while pending:
            if self.failed.is_set():
                return False
            try:
                written = os.write(fd, pending)
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.fail_closed()
                    return False
                try:
                    _, writable, _ = select.select([], [fd], [], min(remaining, MCP_STDIN_POLL_SECONDS))
                except (OSError, ValueError):
                    self.fail_closed()
                    return False
                if not writable and time.monotonic() >= deadline:
                    self.fail_closed()
                    return False
                continue
            except (BrokenPipeError, OSError):
                self.fail_closed()
                return False
            if written <= 0:
                self.fail_closed()
                return False
            pending = pending[written:]
        return True

    def _run(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=MCP_STDIN_POLL_SECONDS)
            except queue.Empty:
                if self._closed.is_set() or self.failed.is_set():
                    return
                continue
            except Exception:
                self.fail_closed()
                return
            if item is None:
                return
            if self.failed.is_set():
                continue
            try:
                if self._fd is not None:
                    if not self._write_fd(item):
                        return
                else:
                    self._stream.write(item + "\n")
                    self._stream.flush()
            except Exception:
                self.fail_closed()
                return

    def close(self) -> None:
        self._closed.set()
        try:
            self._queue.put_nowait(None)
        except Exception:
            # A full queue is safe to abandon: the bounded join below is the
            # shutdown deadline, and a failed writer must never be unblocked by
            # closing a potentially locked high-level stream.
            pass
        if self._thread is not None:
            self._thread.join(timeout=MCP_WRITE_HARD_TIMEOUT_SECONDS + 1.0)
            if self._thread.is_alive():
                self.fail_closed()
        if self._fd is not None and self._fd_was_blocking is not None:
            try:
                os.set_blocking(self._fd, self._fd_was_blocking)
            except OSError:
                pass


_LINE_TOO_LARGE = object()


class BoundedStdinReader:
    """Read complete JSON-RPC lines while polling the writer failure event.

    Reading directly from a non-blocking stdin FD prevents the main thread from
    getting stuck in ``BufferedReader.readline`` after stdout has failed.  The
    no-fileno fallback is retained for embedders that provide a custom stream.
    """

    def __init__(self, stream: Any) -> None:
        self._stream = stream
        self._fd: Optional[int] = None
        self._buffer = bytearray()
        self._eof = False
        self._oversized = False
        try:
            candidate_fd = int(stream.fileno())
            os.set_blocking(candidate_fd, False)
            self._fd = candidate_fd
        except (AttributeError, OSError, TypeError, ValueError):
            self._fd = None

    def readline(self, max_bytes: int, stop_event: threading.Event) -> Any:
        if self._fd is None:
            if stop_event.is_set():
                return None
            return self._stream.readline(max_bytes + 1)
        while True:
            if stop_event.is_set():
                return None
            newline = self._buffer.find(b"\n")
            if newline >= 0:
                line = bytes(self._buffer[: newline + 1])
                del self._buffer[: newline + 1]
                too_large = self._oversized or len(line) > max_bytes
                self._oversized = False
                return _LINE_TOO_LARGE if too_large else line
            if len(self._buffer) > max_bytes:
                # Keep at most a bounded chunk while discarding the remainder
                # of this oversized line up to its newline.
                self._buffer.clear()
                self._oversized = True
            if self._eof:
                if not self._buffer:
                    if self._oversized:
                        self._oversized = False
                        return _LINE_TOO_LARGE
                    return b""
                line = bytes(self._buffer)
                self._buffer.clear()
                too_large = self._oversized or len(line) > max_bytes
                self._oversized = False
                return _LINE_TOO_LARGE if too_large else line
            try:
                ready, _, _ = select.select(
                    [self._fd], [], [], MCP_STDIN_POLL_SECONDS
                )
            except (OSError, ValueError):
                self._eof = True
                continue
            if not ready:
                continue
            try:
                chunk = os.read(self._fd, 65_536)
            except BlockingIOError:
                continue
            except OSError:
                self._eof = True
                continue
            if not chunk:
                self._eof = True
                continue
            if self._oversized:
                newline = chunk.find(b"\n")
                if newline < 0:
                    continue
                self._oversized = False
                self._buffer.extend(chunk[newline + 1 :])
                return _LINE_TOO_LARGE
            self._buffer.extend(chunk)


def _tool(
    name: str,
    description: str,
    schema: Dict[str, Any],
    *,
    read_only: bool,
    destructive: bool = False,
    open_world: bool = False,
) -> Dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "inputSchema": schema,
        "annotations": {
            "readOnlyHint": read_only,
            "destructiveHint": destructive,
            "idempotentHint": False,
            "openWorldHint": open_world,
        },
    }


COMMON_TASK_PROPERTIES = {
    "task": {
        "type": "string",
        "minLength": 1,
        "maxLength": 100000,
        "description": "A bounded task packet with scope, constraints, and acceptance criteria.",
    },
    "cwd": {
        "type": "string",
        "description": "Absolute current Codex workspace directory. Grok runs directly in this exact directory; broad account, temporary, and system roots are refused.",
    },
    "timeout_seconds": {
        "type": "integer",
        "minimum": 10,
        "maximum": 3600,
        "default": DEFAULT_TIMEOUT_SECONDS,
    },
    "max_output_chars": {
        "type": "integer",
        "minimum": 1000,
        "maximum": 200000,
        "default": DEFAULT_OUTPUT_CHARS,
    },
    "web_access": {
        "type": "boolean",
        "description": "Enable Grok built-in web search. Defaults on only for research.",
    },
    "max_turns": {
        "type": "integer",
        "minimum": 1,
        "maximum": MAX_AGENT_TURNS,
        "default": DEFAULT_MAX_TURNS,
        "description": "Hard ACP turn ceiling. The bridge never retries automatically.",
    },
}


TOOLS: List[Dict[str, Any]] = [
    _tool(
        "setup",
        "Optional diagnostic: refresh the local Grok Build model catalog for the target directory, then use ACP initialize metadata to attest the provider runtime default model and its highest advertised reasoning effort. Not required before delegate_readonly; every job still live-attests. Fails closed on stale catalogs, ambiguity, or mismatch.",
        {
            "type": "object",
            "properties": {
                "cwd": {
                    "type": "string",
                    "description": "Absolute current Codex workspace directory whose effective Grok configuration should be resolved.",
                },
                "timeout_seconds": {
                    "type": "integer",
                    "minimum": 10,
                    "maximum": MAX_SETUP_TIMEOUT_SECONDS,
                    "default": DEFAULT_SETUP_TIMEOUT_SECONDS,
                    "description": "Total setup deadline covering catalog probes and ACP runtime attestation.",
                },
            },
            "required": ["cwd"],
            "additionalProperties": False,
        },
        read_only=True,
        open_world=True,
    ),
    _tool(
        "delegate_readonly",
        "Start a read-only Grok Build job directly in the exact caller-provided cwd. No repository copy, projection, Git snapshot, file-tree scan, or entry limit is applied. Optional paths are prompt-only focus hints and never change Grok's accessible cwd. The job live-attests the provider default model and highest advertised effort. Pair with await_result; long jobs repeat await_result only after a wait timeout.",
        {
            "type": "object",
            "properties": {
                **COMMON_TASK_PROPERTIES,
                "mode": {"type": "string", "enum": ["research", "plan", "review"]},
                "paths": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1, "maxLength": 4096},
                    "minItems": 1,
                    "maxItems": 200,
                    "description": "Optional relative focus paths included only in the task prompt. They are advisory, do not create an allowlist or projection, and do not change the exact cwd.",
                },
            },
            "required": ["task", "cwd", "mode"],
            "additionalProperties": False,
        },
        read_only=True,
        open_world=True,
    ),
    _tool(
        "spawn_readonly",
        "Start an asynchronous Grok Build research, plan, or review job directly in the exact caller-provided cwd. It requests Grok's read-only sandbox and disables Grok's own subagents and external MCP servers. Returns an exact job ID immediately; prefer delegate_readonly for new callers.",
        {
            "type": "object",
            "properties": {
                **COMMON_TASK_PROPERTIES,
                "mode": {"type": "string", "enum": ["research", "plan", "review"]},
            },
            "required": ["task", "cwd", "mode"],
            "additionalProperties": False,
        },
        read_only=True,
        open_world=True,
    ),
    _tool(
        "spawn_worker",
        "Start an asynchronous Grok Build implementation job directly in the exact caller-provided cwd. Git primary checkouts, linked worktrees, dirty Git directories, and non-Git directories are supported. A Luna-requested correction may reference the immediately preceding successful same-cwd worker; the bridge caps rounds and branches but does not snapshot the workspace.",
        {
            "type": "object",
            "properties": {
                **COMMON_TASK_PROPERTIES,
                "correction_of_job_id": {
                    "type": "string",
                    "format": "uuid",
                    "description": "For a Luna-requested correction only: the immediately preceding successful implementation job ID in the same chain.",
                },
            },
            "required": ["task", "cwd"],
            "additionalProperties": False,
        },
        read_only=False,
        destructive=True,
        open_world=True,
    ),
    _tool(
        "status",
        "Return grok.codex.result.v2 compact lifecycle metadata for one exact Grok job ID, including native-direct workspace evidence, model attestation, sandbox, loop guards, revision, route, and result availability.",
        {
            "type": "object",
            "properties": {"job_id": {"type": "string", "format": "uuid"}},
            "required": ["job_id"],
            "additionalProperties": False,
        },
        read_only=True,
    ),
    _tool(
        "await_result",
        "Bounded long-poll for one exact job ID. Waits at most 1..60 seconds for a terminal state, manager/server close, or the wait timeout. Routine running/model revision changes do not complete the wait. The response reports whether the wait timed out and whether revision is newer than after_revision. Terminal calls are idempotent. Do not start another Grok job because an internal revision changed. Normal short flow: delegate_readonly then one await_result. Long jobs repeat await_result only after a wait timeout.",
        {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "format": "uuid"},
                "after_revision": {
                    "type": "integer",
                    "minimum": 0,
                    "default": 0,
                    "description": "Caller-side revision already observed. The response sets progress_changed when its revision is newer; internal running/model revisions do not complete the wait.",
                },
                "max_wait_seconds": {
                    "type": "integer",
                    "minimum": MIN_AWAIT_SECONDS,
                    "maximum": MAX_AWAIT_SECONDS,
                    "default": DEFAULT_AWAIT_SECONDS,
                },
                "offset": {"type": "integer", "minimum": 0, "default": 0},
                "limit": {
                    "type": "integer",
                    "minimum": 1000,
                    "maximum": 80000,
                    "default": DEFAULT_AWAIT_RESULT_CHARS,
                },
            },
            "required": ["job_id"],
            "additionalProperties": False,
        },
        read_only=True,
    ),
    _tool(
        "result",
        "Return a completed Grok job receipt and a bounded page of its public answer. The v2 receipt records native-direct execution and does not claim a Git or file-tree integrity snapshot. It remains unverified until Codex performs the required checks.",
        {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "format": "uuid"},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
                "limit": {
                    "type": "integer",
                    "minimum": 1000,
                    "maximum": 80000,
                    "default": DEFAULT_RESULT_PAGE_CHARS,
                },
            },
            "required": ["job_id"],
            "additionalProperties": False,
        },
        read_only=True,
    ),
    _tool(
        "list",
        "List recent in-memory Grok jobs. Active jobs are not claimed to survive an MCP server restart.",
        {
            "type": "object",
            "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20}},
            "additionalProperties": False,
        },
        read_only=True,
    ),
    _tool(
        "cancel",
        "Cancel one job by terminating only the exact Grok process group created for that job. A Codex subagent must not cancel a sibling caller's job.",
        {
            "type": "object",
            "properties": {"job_id": {"type": "string", "format": "uuid"}},
            "required": ["job_id"],
            "additionalProperties": False,
        },
        read_only=False,
        destructive=True,
    ),
]


def _content(payload: Dict[str, Any], *, is_error: bool = False) -> Dict[str, Any]:
    safe_payload = _redact_public_value(payload)
    result: Dict[str, Any] = {
        "content": [
            {
                "type": "text",
                "text": json.dumps(
                    safe_payload, ensure_ascii=False, separators=(",", ":")
                ),
            }
        ]
    }
    if is_error:
        result["isError"] = True
    return result


def call_tool(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    try:
        if name == "setup":
            return _content(
                setup_grok(
                    arguments["cwd"],
                    arguments.get("timeout_seconds", DEFAULT_SETUP_TIMEOUT_SECONDS),
                )
            )
        if name == "delegate_readonly":
            mode = arguments["mode"]
            if mode not in READ_ONLY_MODES:
                raise BridgeError(
                    "E_MODE",
                    "delegate_readonly accepts only research, plan, or review mode.",
                )
            if "paths" in arguments and arguments["paths"] is None:
                raise BridgeError(
                    "E_PATHS",
                    "paths must be omitted or provided as a non-empty array of advisory relative focus paths; null is not valid.",
                )
            payload = MANAGER.spawn(
                mode=mode,
                task=arguments["task"],
                cwd=arguments["cwd"],
                timeout_seconds=arguments.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS),
                max_output_chars=arguments.get("max_output_chars", DEFAULT_OUTPUT_CHARS),
                web_access=arguments.get("web_access"),
                max_turns=arguments.get("max_turns", DEFAULT_MAX_TURNS),
                paths=arguments.get("paths"),
                delegate_readonly=True,
            )
            return _content(payload)
        if name == "spawn_readonly":
            mode = arguments["mode"]
            if mode not in READ_ONLY_MODES:
                raise BridgeError(
                    "E_MODE",
                    "spawn_readonly accepts only research, plan, or review mode.",
                )
            payload = MANAGER.spawn(
                mode=mode,
                task=arguments["task"],
                cwd=arguments["cwd"],
                timeout_seconds=arguments.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS),
                max_output_chars=arguments.get("max_output_chars", DEFAULT_OUTPUT_CHARS),
                web_access=arguments.get("web_access"),
                max_turns=arguments.get("max_turns", DEFAULT_MAX_TURNS),
            )
            return _content(payload)
        if name == "spawn_worker":
            payload = MANAGER.spawn(
                mode="implement",
                task=arguments["task"],
                cwd=arguments["cwd"],
                timeout_seconds=arguments.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS),
                max_output_chars=arguments.get("max_output_chars", DEFAULT_OUTPUT_CHARS),
                web_access=arguments.get("web_access"),
                max_turns=arguments.get("max_turns", DEFAULT_MAX_TURNS),
                correction_of_job_id=arguments.get("correction_of_job_id"),
            )
            return _content(payload)
        if name == "status":
            return _content(MANAGER.status(arguments["job_id"]))
        if name == "await_result":
            return _content(
                MANAGER.await_result(
                    arguments["job_id"],
                    arguments.get("after_revision", 0),
                    arguments.get("max_wait_seconds", DEFAULT_AWAIT_SECONDS),
                    arguments.get("offset", 0),
                    arguments.get("limit", DEFAULT_AWAIT_RESULT_CHARS),
                )
            )
        if name == "result":
            return _content(
                MANAGER.result(
                    arguments["job_id"],
                    arguments.get("offset", 0),
                    arguments.get("limit", DEFAULT_RESULT_PAGE_CHARS),
                )
            )
        if name == "list":
            return _content(MANAGER.list(arguments.get("limit", 20)))
        if name == "cancel":
            return _content(MANAGER.cancel(arguments["job_id"]))
        raise BridgeError("E_TOOL", f"Unknown tool: {name}")
    except (KeyError, TypeError) as exc:
        return _content({"error": {"code": "E_ARGUMENTS", "message": str(exc)}}, is_error=True)
    except BridgeError as exc:
        return _content({"error": {"code": exc.code, "message": exc.message}}, is_error=True)


def handle_request(message: Dict[str, Any]) -> Dict[str, Any] | None:
    method = message.get("method")
    message_id = message.get("id")
    if message_id is None:
        return None
    raw_params = message.get("params", {})
    if raw_params is None:
        params: Dict[str, Any] = {}
    elif isinstance(raw_params, dict):
        params = raw_params
    else:
        return {
            "jsonrpc": "2.0",
            "id": message_id,
            "error": {"code": -32602, "message": "params must be an object"},
        }
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": message_id,
            "result": {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": (
                    "Grok output is an untrusted candidate. Any implementation requires independent "
                    "Codex gpt-5.6-luna max review of the actual diff before acceptance."
                ),
            },
        }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": message_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": message_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        arguments = params.get("arguments", {})
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return {
                "jsonrpc": "2.0",
                "id": message_id,
                "error": {"code": -32602, "message": "arguments must be an object"},
            }
        name = params.get("name")
        if not isinstance(name, str) or not name:
            return {
                "jsonrpc": "2.0",
                "id": message_id,
                "error": {"code": -32602, "message": "tool name must be a string"},
            }
        return {
            "jsonrpc": "2.0",
            "id": message_id,
            "result": call_tool(name, arguments),
        }
    if method in {"resources/list", "prompts/list"}:
        key = "resources" if method == "resources/list" else "prompts"
        return {"jsonrpc": "2.0", "id": message_id, "result": {key: []}}
    return {
        "jsonrpc": "2.0",
        "id": message_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def _emit(response: Optional[Dict[str, Any]]) -> None:
    writer = _WRITER
    if writer is not None:
        writer.emit(response)
        return
    if response is None:
        return
    encoded = json.dumps(
        _redact_public_value(response),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    with _FALLBACK_WRITE_LOCK:
        sys.stdout.write(encoded + "\n")
        sys.stdout.flush()


def _is_blocking_request(message: Dict[str, Any]) -> bool:
    if message.get("method") != "tools/call":
        return False
    params = message.get("params")
    if not isinstance(params, dict):
        return False
    name = params.get("name")
    return isinstance(name, str) and name in BLOCKING_TOOLS


def _process_message(message: Dict[str, Any]) -> None:
    try:
        response = handle_request(message)
    except Exception:
        message_id = message.get("id") if isinstance(message, dict) else None
        response = {
            "jsonrpc": "2.0",
            "id": message_id,
            "error": {"code": -32603, "message": "Internal server error"},
        }
    _emit(response)


def _run_blocking(message: Dict[str, Any]) -> None:
    try:
        _process_message(message)
    finally:
        _BLOCKING_GATE.release()


def main() -> int:
    global _WRITER
    _WRITER = BoundedStdoutWriter(sys.stdout)
    _WRITER.start()
    stdin_reader = BoundedStdinReader(sys.stdin.buffer)
    try:
        while not _WRITER.failed.is_set():
            line = stdin_reader.readline(MAX_MCP_LINE_BYTES, _WRITER.failed)
            if line is None:
                break
            if not line:
                break
            if line is _LINE_TOO_LARGE:
                _emit(
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {
                            "code": -32700,
                            "message": "Request line is too large",
                        },
                    }
                )
                continue
            if not line.strip():
                continue
            if len(line) > MAX_MCP_LINE_BYTES:
                _emit(
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {
                            "code": -32700,
                            "message": "Request line is too large",
                        },
                    }
                )
                continue
            try:
                message = json.loads(line.decode("utf-8"))
                if not isinstance(message, dict):
                    raise ValueError("request must be an object")
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
                _emit(
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32700, "message": f"Parse error: {exc}"},
                    }
                )
                continue
            if _is_blocking_request(message):
                acquired = _BLOCKING_GATE.acquire(blocking=False)
                if not acquired:
                    _emit(
                        {
                            "jsonrpc": "2.0",
                            "id": message.get("id"),
                            "error": {
                                "code": -32000,
                                "message": "Too many in-flight blocking MCP requests.",
                            },
                        }
                    )
                    continue
                try:
                    _BLOCKING_POOL.submit(_run_blocking, message)
                except Exception:
                    _BLOCKING_GATE.release()
                    _emit(
                        {
                            "jsonrpc": "2.0",
                            "id": message.get("id"),
                            "error": {
                                "code": -32603,
                                "message": "Internal server error",
                            },
                        }
                    )
                continue
            _process_message(message)
        return 0
    finally:
        begin_bridge_shutdown()
        MANAGER.close()
        if _WRITER is not None:
            _WRITER.close()
        _BLOCKING_POOL.shutdown(wait=True, cancel_futures=True)


if __name__ == "__main__":
    raise SystemExit(main())
