#!/usr/bin/env python3
"""Deterministic fake Grok ACP process for bridge tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path


HELP = """
--cwd --sandbox --no-subagents --no-memory --deny --disallowed-tools
--model --reasoning-effort --always-approve --no-leader --max-turns
--disable-web-search
"""


def emit(message: dict) -> None:
    sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def acp(args: list[str]) -> int:
    cwd = Path.cwd()
    binary_name = Path(sys.argv[0]).name
    if "discovery-fallback-after-limit" in binary_name:
        sys.stderr.write("x" * 13_000)
        sys.stderr.write(" preferred model not in available models, falling back\n")
        sys.stderr.flush()
    elif "discovery-stderr-overflow" in binary_name:
        sys.stderr.write("x" * 13_000)
        sys.stderr.flush()
    requested_model = args[args.index("--model") + 1] if "--model" in args else None
    requested_effort = (
        args[args.index("--reasoning-effort") + 1]
        if "--reasoning-effort" in args
        else None
    )
    selected_model = requested_model or "grok-9.2"
    if selected_model not in {"grok-9.2", "grok-9.1"}:
        print("preferred model not in available models, falling back", file=sys.stderr)
        selected_model = "grok-9.2"
    selected_effort = requested_effort or ("xhigh" if selected_model == "grok-9.2" else "high")
    for line in sys.stdin:
        if not line.strip():
            continue
        message = json.loads(line)
        message_id = message.get("id")
        method = message.get("method")
        params = message.get("params") or {}
        if method == "initialize":
            if "slow-runtime-attestation" in binary_name:
                time.sleep(30)
            emit(
                {
                    "jsonrpc": "2.0",
                    "id": message_id,
                    "result": {
                        "protocolVersion": 1,
                        "authMethods": [{"id": "cached_token"}, {"id": "xai.api_key"}],
                        "_meta": {
                            "modelState": {
                                "currentModelId": selected_model,
                                "availableModels": [
                                    {
                                        "modelId": "grok-9.2",
                                        "_meta": {
                                            "supportsReasoningEffort": True,
                                            "reasoningEffort": selected_effort
                                            if selected_model == "grok-9.2"
                                            else "xhigh",
                                            "reasoningEfforts": [
                                                {"id": "xhigh", "default": False},
                                                {"id": "high", "default": True},
                                                {"id": "medium", "default": False},
                                                {"id": "low", "default": False},
                                            ],
                                        },
                                    },
                                    {
                                        "modelId": "grok-9.1",
                                        "_meta": {
                                            "supportsReasoningEffort": True,
                                            "reasoningEffort": selected_effort
                                            if selected_model == "grok-9.1"
                                            else "high",
                                            "reasoningEfforts": [
                                                {"id": "high", "default": True},
                                                {"id": "medium", "default": False},
                                                {"id": "low", "default": False},
                                            ],
                                        },
                                    },
                                ],
                            }
                        },
                    },
                }
            )
        elif method == "authenticate":
            emit({"jsonrpc": "2.0", "id": message_id, "result": {}})
        elif method == "session/new":
            requested_cwd = params.get("cwd")
            if (
                not isinstance(requested_cwd, str)
                or not Path(requested_cwd).is_absolute()
                or not Path(requested_cwd).is_dir()
            ):
                emit(
                    {
                        "jsonrpc": "2.0",
                        "id": message_id,
                        "error": {"code": -32602, "message": "Invalid params"},
                    }
                )
                continue
            cwd = Path(requested_cwd)
            emit(
                {
                    "jsonrpc": "2.0",
                    "id": message_id,
                    "result": {"sessionId": str(uuid.uuid4())},
                }
            )
        elif method == "session/prompt":
            prompt = params.get("prompt") or []
            text = "".join(
                item.get("text", "") for item in prompt if isinstance(item, dict)
            )
            if "REMOTE_SECRET_ERROR" in text:
                emit(
                    {
                        "jsonrpc": "2.0",
                        "id": message_id,
                        "error": {
                            "code": -32000,
                            "message": (
                                "synthetic failure at "
                                "http://127.0.0.1:9999/mcp?sc_token=SYNTHETIC_REMOTE_SECRET"
                            ),
                        },
                    }
                )
                continue
            if "MODEL_FALLBACK_WARNING" in text:
                print(
                    "preferred model not in available models, falling back",
                    file=sys.stderr,
                    flush=True,
                )
            if "FALLBACK_AFTER_STDERR_LIMIT" in text:
                sys.stderr.write("x" * 13_000)
                sys.stderr.write(" preferred model not in available models, falling back\n")
                sys.stderr.flush()
            elif "STDERR_OVERFLOW" in text:
                sys.stderr.write("x" * 13_000)
                sys.stderr.flush()
            if "FAKE_SLEEP_SHORT" in text:
                time.sleep(2)
            elif "FAKE_SLEEP" in text:
                time.sleep(30)
            if "CREATE_FILE" in text:
                (cwd / "created_by_grok.txt").write_text("created\n", encoding="utf-8")
            if "MODIFY_IGNORED" in text:
                (cwd / "cache.tmp").write_text("changed ignored content\n", encoding="utf-8")
            if "TOUCH_FILE_METADATA" in text:
                target = cwd / "metadata.txt"
                current = target.stat()
                os.utime(
                    target,
                    ns=(current.st_atime_ns, current.st_mtime_ns + 1_000_000_000),
                )
            if "MODIFY_GITDIR" in text:
                git_dir = Path(
                    subprocess.run(
                        ["git", "rev-parse", "--absolute-git-dir"],
                        cwd=cwd,
                        check=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    ).stdout.strip()
                )
                # Keep this synthetic mutation inside the temporary test
                # repository.  A linked worktree's admin directory is not
                # covered by the ordinary status/diff snapshot.
                (git_dir / "locked").write_text("synthetic lock\n", encoding="utf-8")
                hooks = git_dir / "hooks"
                hooks.mkdir(parents=True, exist_ok=True)
                (hooks / "grok-synthetic-hook").write_text(
                    "#!/bin/sh\n# synthetic test hook\n", encoding="utf-8"
                )
                (git_dir / "config.worktree").write_text(
                    "[synthetic]\n\tmarker = grok\n", encoding="utf-8"
                )
            if "COMMIT_FILE" in text:
                (cwd / "committed_by_grok.txt").write_text("committed\n", encoding="utf-8")
                subprocess.run(
                    ["git", "-C", str(cwd), "add", "committed_by_grok.txt"], check=True
                )
                subprocess.run(
                    ["git", "-C", str(cwd), "commit", "-m", "forbidden fake commit"],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            if "CHANGE_PRIMARY" in text:
                worktrees = subprocess.run(
                    ["git", "-C", str(cwd), "worktree", "list", "--porcelain"],
                    check=True,
                    stdout=subprocess.PIPE,
                    text=True,
                ).stdout
                primary = next(
                    Path(line.removeprefix("worktree "))
                    for line in worktrees.splitlines()
                    if line.startswith("worktree ")
                )
                (primary / "changed_from_worker.txt").write_text("changed\n", encoding="utf-8")
            if "MALFORMED_STDOUT" in text:
                sys.stdout.write("not-json\n")
                sys.stdout.flush()
                continue
            answer = "fake Grok public answer"
            if "ECHO_SECRET" in text:
                answer = f"secret={os.environ.get('XAI_API_KEY', '')}"
            if "SENSITIVE_OUTPUT" in text:
                answer = (
                    "callback=https://synthetic-user:SYNTHETIC_USERINFO_SECRET@"
                    "example.invalid/path\n"
                    "[info] Authorization: Bearer SYNTHETIC_BEARER_SECRET\n"
                    "passwordless=https://SYNTHETIC_PASSWORDLESS_SECRET@localhost/mcp\n"
                    "ftp=ftp://synthetic-user:SYNTHETIC_FTP_SECRET@127.0.0.1/x\n"
                    '{\"api_key\":\"SYNTHETIC_JSON_SECRET\"}\n'
                    "contact=" + "reviewer" + "@" + "example.invalid"
                )
                print(
                    "connection failed: "
                    "http://127.0.0.1:9999/mcp?sc_token=SYNTHETIC_QUERY_SECRET",
                    file=sys.stderr,
                    flush=True,
                )
                print(
                    "Cookie: session=SYNTHETIC_COOKIE_SECRET",
                    file=sys.stderr,
                    flush=True,
                )
                print(
                    "12:00:01 Proxy-Authorization: Basic SYNTHETIC_PROXY_SECRET",
                    file=sys.stderr,
                    flush=True,
                )
            if "LONG_OUTPUT" in text:
                answer = "x" * 5_000
            if "EMPTY_RESPONSE" not in text:
                emit(
                    {
                        "jsonrpc": "2.0",
                        "method": "session/update",
                        "params": {
                            "update": {
                                "sessionUpdate": "agent_message_chunk",
                                "content": {"type": "text", "text": answer},
                            }
                        },
                    }
                )
            if "MODEL_SWITCH" in text and "LATE_MODEL_SWITCH" not in text:
                emit(
                    {
                        "jsonrpc": "2.0",
                        "method": "session/update",
                        "params": {
                            "update": {
                                "sessionUpdate": "model_auto_switched",
                                "fromModelId": selected_model,
                                "toModelId": "grok-9.1",
                            }
                        },
                    }
                )
            emit(
                {
                    "jsonrpc": "2.0",
                    "id": message_id,
                    "result": {
                        "stopReason": (
                            "max_turns"
                            if "TURN_LIMIT" in text
                            else (
                                "end_turn?access_token=SYNTHETIC_STOP_SECRET"
                                if "SENSITIVE_OUTPUT" in text
                                else "end_turn"
                            )
                        ),
                        "usage": {
                            "input_tokens": 10,
                            "output_tokens": 5,
                            **({"model": "grok-9.1"} if "MODEL_MISMATCH" in text else {}),
                            **(
                                {
                                    "diagnostic": (
                                        "https://localhost/report?api_key="
                                        "SYNTHETIC_USAGE_SECRET"
                                    )
                                }
                                if "SENSITIVE_OUTPUT" in text
                                else {}
                            ),
                        },
                    },
                }
            )
            if "LATE_MODEL_SWITCH" in text:
                time.sleep(0.6)
                emit(
                    {
                        "jsonrpc": "2.0",
                        "method": "session/update",
                        "params": {
                            "update": {
                                "sessionUpdate": "model_changed",
                                "fromModelId": selected_model,
                                "toModelId": "grok-9.1",
                            }
                        },
                    }
                )
    return 0


def main() -> int:
    args = sys.argv[1:]
    if "slow-probe" in Path(sys.argv[0]).name and (
        "--version" in args or "--help" in args or (args and args[-1] == "models")
    ):
        time.sleep(30)
    if "--version" in args or args == ["version"]:
        print("grok 1.0.5 (fake)")
        return 0
    if args and args[-1] == "models":
        print(
            "You are logged in with grok.com.\n"
            "Default model: grok-9.2\n\n"
            "Available models:\n"
            "  * grok-9.2 (default)\n"
            "  - grok-9.1"
        )
        if (Path.cwd() / ".fake-grok-cwd-marker").exists():
            print(f"Probe cwd marker observed: {Path.cwd()}")
        if "stale-models" in Path(sys.argv[0]).name:
            print(
                "model catalog: bundled defaults in use (remote_fetch disabled)",
                file=sys.stderr,
            )
        return 0
    if "--help" in args or "-h" in args:
        print(HELP)
        return 0
    if "agent" in args and "stdio" in args:
        return acp(args)
    print("unsupported fake invocation", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
