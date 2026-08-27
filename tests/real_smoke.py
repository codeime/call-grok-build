#!/usr/bin/env python3
"""Opt-in real Grok ACP smoke test; this consumes a Grok Build request."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

from grok_build_bridge import JobManager, setup_grok  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cwd", default=str(PLUGIN_ROOT))
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()
    target_cwd = Path(args.cwd).resolve()

    setup = setup_grok(str(target_cwd))
    if not setup["ready"] or not setup["runtime_attested"]:
        print(json.dumps({"setup": setup}, ensure_ascii=False, indent=2))
        return 2

    manager = JobManager(max_workers=1)
    try:
        spawned = manager.spawn(
            mode="plan",
            cwd=str(target_cwd),
            timeout_seconds=args.timeout,
            max_output_chars=10_000,
            web_access=False,
            max_turns=5,
            task=(
                "This is a smoke test. Return a concise three-step plan for independently "
                "validating a local MCP bridge. Do not inspect or modify files."
            ),
        )
        job_id = spawned["job_id"]
        deadline = time.monotonic() + args.timeout + 15
        while time.monotonic() < deadline:
            status = manager.status(job_id)
            if status["status"] in {"succeeded", "failed", "timed_out", "cancelled"}:
                break
            time.sleep(0.5)
        else:
            manager.cancel(job_id)
            print(json.dumps({"error": "smoke wait exceeded deadline"}))
            return 3

        if status["status"] != "succeeded":
            print(json.dumps(status, ensure_ascii=False, indent=2))
            return 4
        result = manager.result(job_id)
        summary = {
            "status": result["status"],
            "model": result["model"],
            "reasoning_effort": result["reasoning_effort"],
            "sandbox": result["sandbox"],
            "session_id_present": bool(result["session_id"]),
            "answer_nonempty": bool(result["answer"].strip()),
            "answer_preview": result["answer"][:500],
            "verification": result["verification"],
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0 if summary["answer_nonempty"] else 5
    finally:
        manager.close()


if __name__ == "__main__":
    raise SystemExit(main())
