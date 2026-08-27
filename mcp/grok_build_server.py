#!/usr/bin/env python3
"""Minimal MCP stdio server exposing bounded Grok Build delegation tools."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

from grok_build_bridge import (  # noqa: E402
    BridgeError,
    DEFAULT_MAX_TURNS,
    DEFAULT_OUTPUT_CHARS,
    DEFAULT_SETUP_TIMEOUT_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    JobManager,
    MAX_AGENT_TURNS,
    MAX_SETUP_TIMEOUT_SECONDS,
    READ_ONLY_MODES,
    _redact_public_value,
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
        "description": "Absolute project or linked-worktree path. Broad account, temporary, and system roots are refused.",
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
        "Refresh the local Grok Build model catalog for the target directory, then use ACP initialize metadata to attest the provider runtime default model and its highest advertised reasoning effort. Fails closed on stale catalogs, ambiguity, or mismatch.",
        {
            "type": "object",
            "properties": {
                "cwd": {
                    "type": "string",
                    "description": "Absolute target project/worktree path whose effective Grok configuration should be resolved. Broad account, temporary, and system roots are refused.",
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
    ),
    _tool(
        "spawn_readonly",
        "Start an asynchronous Grok Build research, plan, or review job. It requests the Grok CLI OS-level read-only sandbox and disables Grok subagents and external MCP servers. Returns a job ID immediately.",
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
        "Start an asynchronous Grok Build implementation job. Initial work requires a clean linked Git worktree. A Luna-requested correction must reference the immediately preceding successful worker job; the bridge proves no intervening worktree change and enforces at most two correction rounds with no retries or branches.",
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
        "Return lifecycle metadata for one Grok job without returning its potentially large answer.",
        {
            "type": "object",
            "properties": {"job_id": {"type": "string", "format": "uuid"}},
            "required": ["job_id"],
            "additionalProperties": False,
        },
        read_only=True,
    ),
    _tool(
        "result",
        "Return a completed Grok job receipt and a bounded page of its public answer. The receipt is unverified until Codex performs the required checks.",
        {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "format": "uuid"},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
                "limit": {"type": "integer", "minimum": 1000, "maximum": 80000, "default": 40000},
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
        "Cancel one job by terminating only the exact Grok process group created for that job.",
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
        if name == "result":
            return _content(
                MANAGER.result(
                    arguments["job_id"],
                    arguments.get("offset", 0),
                    arguments.get("limit", 40_000),
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


def main() -> int:
    try:
        while True:
            line = sys.stdin.buffer.readline(MAX_MCP_LINE_BYTES + 1)
            if not line:
                break
            if not line.strip():
                continue
            if len(line) > MAX_MCP_LINE_BYTES:
                while line and not line.endswith(b"\n"):
                    line = sys.stdin.buffer.readline(MAX_MCP_LINE_BYTES + 1)
                response = {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": "Request line is too large"},
                }
                encoded = json.dumps(response, separators=(",", ":"))
                sys.stdout.write(encoded + "\n")
                sys.stdout.flush()
                continue
            try:
                message = json.loads(line.decode("utf-8"))
                if not isinstance(message, dict):
                    raise ValueError("request must be an object")
                response = handle_request(message)
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
                response = {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": f"Parse error: {exc}"},
                }
            except Exception:
                response = {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32603, "message": "Internal server error"},
                }
            if response is not None:
                encoded = json.dumps(
                    _redact_public_value(response),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                sys.stdout.write(encoded + "\n")
                sys.stdout.flush()
        return 0
    finally:
        MANAGER.close()


if __name__ == "__main__":
    raise SystemExit(main())
