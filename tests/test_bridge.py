from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

import grok_build_bridge as bridge  # noqa: E402
from grok_build_bridge import (  # noqa: E402
    BridgeError,
    DEFAULT_AWAIT_RESULT_CHARS,
    DEFAULT_OUTPUT_CHARS,
    JobManager,
    MAX_SCOPE_PATH_HINTS,
    MAX_SCOPE_PATH_HINT_BYTES,
    ROUTE_DIRECT,
    _build_task_prompt,
    _catalog_refresh_is_clean,
    _extract_runtime_model_policy,
    _minimal_environment,
    _parse_model_catalog,
    _path_is_within,
    _redact_public_value,
    _run_probe_command,
    _validate_cwd,
    _validate_scope_path_hints,
    build_acp_command,
    probe_grok,
    setup_grok,
)


FAKE_GROK = PLUGIN_ROOT / "tests" / "fake_grok.py"
SYNTHETIC_EMAIL = "reviewer" + "@" + "example.invalid"


def run_git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(cwd), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr)
    return completed.stdout


def wait_terminal(manager: JobManager, job_id: str, timeout: float = 60.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = manager.status(job_id)
        if status["status"] in {"succeeded", "failed", "timed_out", "cancelled"}:
            return status
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish")


class DirectCwdBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_binary = os.environ.get("GROK_BUILD_BIN")
        os.environ["GROK_BUILD_BIN"] = str(FAKE_GROK)

    def tearDown(self) -> None:
        if self.old_binary is None:
            os.environ.pop("GROK_BUILD_BIN", None)
        else:
            os.environ["GROK_BUILD_BIN"] = self.old_binary

    def test_probe_uses_live_provider_default_without_version_constant(self) -> None:
        probe = probe_grok(force=True)
        self.assertTrue(probe["ready"])
        self.assertEqual(probe["selected_model"], "grok-9.2")
        self.assertEqual(probe["default_model"], "grok-9.2")
        self.assertEqual(probe["available_models"], ["grok-9.2", "grok-9.1"])
        self.assertTrue(probe["online_model_check_confirmed"])

    def test_setup_attests_highest_effort_for_runtime_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = setup_grok(directory)
        self.assertTrue(result["ready"])
        self.assertTrue(result["runtime_attested"])
        self.assertEqual(result["selected_model"], "grok-9.2")
        self.assertEqual(result["selected_reasoning_effort"], "xhigh")
        self.assertEqual(result["cwd"], ".")
        self.assertEqual(result["catalog_cwd"], ".")

    def test_runtime_policy_prefers_highest_advertised_effort(self) -> None:
        policy = _extract_runtime_model_policy(
            {
                "_meta": {
                    "modelState": {
                        "currentModelId": "grok-future",
                        "availableModels": [
                            {
                                "modelId": "grok-future",
                                "_meta": {
                                    "reasoningEffort": "high",
                                    "reasoningEfforts": [
                                        {"id": "low"},
                                        {"id": "xhigh"},
                                        {"id": "high"},
                                    ],
                                },
                            }
                        ],
                    }
                }
            },
            expected_catalog_default="grok-future",
        )
        self.assertEqual(policy["selected_model"], "grok-future")
        self.assertEqual(policy["selected_reasoning_effort"], "xhigh")

    def test_runtime_policy_refuses_unknown_effort(self) -> None:
        with self.assertRaises(BridgeError) as caught:
            _extract_runtime_model_policy(
                {
                    "_meta": {
                        "modelState": {
                            "currentModelId": "grok-future",
                            "availableModels": [
                                {
                                    "modelId": "grok-future",
                                    "_meta": {
                                        "reasoningEfforts": [
                                            {"id": "ultra"},
                                            {"id": "xhigh"},
                                        ]
                                    },
                                }
                            ],
                        }
                    }
                },
                expected_catalog_default="grok-future",
            )
        self.assertEqual(caught.exception.code, "E_EFFORT_ATTESTATION")

    def test_catalog_parser_rejects_conflicting_defaults(self) -> None:
        catalog = _parse_model_catalog(
            "Default model: grok-a\nAvailable models:\n  * grok-b (default)\n  - grok-a\n"
        )
        self.assertTrue(catalog["default_model_ambiguous"])
        self.assertIsNone(catalog["default_model"])

    def test_catalog_refresh_rejects_cache_and_retry_failures(self) -> None:
        catalog = (
            "Default model: grok-9.2\nAvailable models:\n"
            "  * grok-9.2 (default)\n  - grok-9.1\n"
        )
        for marker in (
            "model catalog refresh failed",
            "model catalog: all retries exhausted",
            "models cache is stale",
            "models cache origin mismatch",
        ):
            with self.subTest(marker=marker):
                self.assertFalse(_catalog_refresh_is_clean(catalog + marker, 0))

    def test_probe_rejects_stale_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "stale-models-grok"
            binary.write_bytes(FAKE_GROK.read_bytes())
            binary.chmod(0o755)
            os.environ["GROK_BUILD_BIN"] = str(binary)
            result = probe_grok(force=True)
        self.assertFalse(result["ready"])
        self.assertFalse(result["online_model_check_confirmed"])

    def test_probe_is_resolved_in_target_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            (target / ".fake-grok-cwd-marker").touch()
            result = probe_grok(force=True, cwd=target)
        self.assertEqual(result["catalog_cwd"], str(target.resolve()))
        self.assertIn("Probe cwd marker observed", result["model_diagnostics"])

    def test_readonly_job_returns_v2_direct_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = JobManager()
            try:
                spawned = manager.spawn(
                    mode="research", task="Find the architecture", cwd=directory
                )
                self.assertEqual(spawned["route"], ROUTE_DIRECT)
                status = wait_terminal(manager, spawned["job_id"])
                self.assertEqual(status["status"], "succeeded", status)
                result = manager.result(spawned["job_id"])
            finally:
                manager.close()
        self.assertEqual(result["schema_version"], "grok.codex.result.v2")
        self.assertEqual(result["cwd"], ".")
        self.assertEqual(result["answer"], "fake Grok public answer")
        self.assertEqual(result["model"], "grok-9.2")
        self.assertEqual(result["reasoning_effort"], "xhigh")
        self.assertEqual(result["workspace"]["execution"], "native_direct")
        self.assertTrue(result["workspace"]["cwd_bound_by_stable_fd"])
        self.assertEqual(result["workspace"]["integrity_snapshot"], "not_collected")
        self.assertFalse(result["verification"]["verified"])
        self.assertFalse(result["verification"]["review_required"])

    def test_non_git_implement_writes_to_exact_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = JobManager()
            try:
                spawned = manager.spawn(
                    mode="implement", task="CREATE_FILE", cwd=str(root)
                )
                status = wait_terminal(manager, spawned["job_id"])
                self.assertEqual(status["status"], "succeeded", status)
                result = manager.result(spawned["job_id"])
            finally:
                manager.close()
            self.assertEqual(
                (root / "created_by_grok.txt").read_text(encoding="utf-8"),
                "created\n",
            )
        self.assertEqual(result["sandbox"], "workspace")
        self.assertTrue(result["verification"]["review_required"])
        self.assertEqual(result["workspace"]["execution"], "native_direct")

    def test_primary_dirty_git_checkout_allows_direct_implement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_git(root, "init")
            run_git(root, "config", "user.email", "test@example.invalid")
            run_git(root, "config", "user.name", "Test")
            (root / "tracked.txt").write_text("base\n", encoding="utf-8")
            run_git(root, "add", "tracked.txt")
            run_git(root, "commit", "-m", "base")
            (root / "tracked.txt").write_text("dirty\n", encoding="utf-8")
            (root / "untracked.txt").write_text("existing\n", encoding="utf-8")
            manager = JobManager()
            try:
                spawned = manager.spawn(
                    mode="implement", task="CREATE_FILE", cwd=str(root)
                )
                status = wait_terminal(manager, spawned["job_id"])
                self.assertEqual(status["status"], "succeeded", status)
            finally:
                manager.close()
            self.assertTrue((root / "created_by_grok.txt").is_file())
            self.assertEqual((root / "tracked.txt").read_text(), "dirty\n")
            self.assertTrue((root / "untracked.txt").is_file())

    def test_paths_are_prompt_only_and_keep_exact_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = JobManager()
            try:
                with mock.patch(
                    "grok_build_bridge.os.scandir",
                    side_effect=AssertionError("tree scan is forbidden"),
                ):
                    spawned = manager.spawn(
                        mode="review",
                        task="Review focused code",
                        cwd=str(root),
                        paths=["src", "tests/test_api.py"],
                        delegate_readonly=True,
                    )
                    status = wait_terminal(manager, spawned["job_id"])
                self.assertEqual(status["status"], "succeeded", status)
                result = manager.result(spawned["job_id"])
            finally:
                manager.close()
        self.assertEqual(result["route"], ROUTE_DIRECT)
        self.assertTrue(result["workspace"]["scope_paths_advisory"])
        self.assertEqual(result["workspace"]["scope_path_count"], 2)

    def test_no_legacy_entry_limit_or_snapshot_api_remains(self) -> None:
        for name in (
            "MAX_IGNORED_FILES",
            "MAX_IGNORED_CONTENT_BYTES",
            "MAX_PROJECTION_ENTRIES",
            "git_snapshot",
            "filesystem_snapshot",
            "materialize_scoped_projection",
        ):
            self.assertFalse(hasattr(bridge, name), name)

    def test_directory_with_20001_entries_runs_without_tree_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index in range(20_001):
                (root / f"entry-{index}").touch()
            manager = JobManager(max_workers=1)
            try:
                spawned = manager.spawn(
                    mode="review",
                    task="Review without scanning the directory tree",
                    cwd=str(root),
                )
                status = wait_terminal(manager, spawned["job_id"])
                self.assertEqual(status["status"], "succeeded", status)
                result = manager.result(spawned["job_id"])
            finally:
                manager.close()
        self.assertEqual(result["route"], ROUTE_DIRECT)
        self.assertEqual(result["workspace"]["execution"], "native_direct")
        self.assertEqual(result["workspace"]["integrity_snapshot"], "not_collected")

    def test_focus_hint_validation_does_not_read_filesystem(self) -> None:
        with mock.patch(
            "grok_build_bridge.os.scandir",
            side_effect=AssertionError("focus validation must not scan"),
        ):
            self.assertEqual(
                _validate_scope_path_hints(["src", "tests/test_api.py"]),
                ["src", "tests/test_api.py"],
            )
        invalid = ([], ["/tmp/x"], ["../x"], [".git/config"], [".env"], ["a", "a"])
        for paths in invalid:
            with self.subTest(paths=paths), self.assertRaises(BridgeError) as caught:
                _validate_scope_path_hints(paths)
            self.assertEqual(caught.exception.code, "E_PATHS")
        with self.assertRaises(BridgeError):
            _validate_scope_path_hints(
                [f"p{index}" for index in range(MAX_SCOPE_PATH_HINTS + 1)]
            )
        exact = [f"{index:02d}-" + "a" * 3997 for index in range(8)]
        self.assertEqual(sum(len(value.encode("utf-8")) for value in exact), 32_000)
        self.assertEqual(MAX_SCOPE_PATH_HINT_BYTES, 32_000)
        self.assertEqual(_validate_scope_path_hints(exact), exact)
        with self.assertRaises(BridgeError) as total_limit:
            _validate_scope_path_hints(exact + ["b"])
        self.assertEqual(total_limit.exception.code, "E_PATHS")

    def test_task_prompt_reports_direct_workspace_and_advisory_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory).resolve()
            prompt = _build_task_prompt(
                "review",
                f"Review {cwd}/src",
                cwd,
                "grok-9.2",
                "xhigh",
                scope_paths=("src",),
            )
        self.assertIn("WORKING DIRECTORY: .", prompt)
        self.assertIn("ADVISORY FOCUS PATHS (relative): src", prompt)
        self.assertIn("do not change or restrict the working directory", prompt)
        self.assertNotIn(str(cwd), prompt)

    def test_acp_command_keeps_sandbox_and_recursion_guards(self) -> None:
        readonly = build_acp_command(
            binary=str(FAKE_GROK),
            cwd=Path("."),
            mode="review",
            max_turns=24,
            web_access=False,
            model="grok-9.2",
            reasoning_effort="xhigh",
            supports_no_memory=True,
            supports_no_auto_update=True,
        )
        worker = build_acp_command(
            binary=str(FAKE_GROK),
            cwd=Path("."),
            mode="implement",
            max_turns=24,
            web_access=False,
            model="grok-9.2",
            reasoning_effort="xhigh",
        )
        self.assertEqual(readonly[readonly.index("--sandbox") + 1], "read-only")
        self.assertEqual(worker[worker.index("--sandbox") + 1], "workspace")
        for command in (readonly, worker):
            self.assertIn("--no-subagents", command)
            self.assertIn("--no-leader", command)
            self.assertIn("run_terminal_cmd,Agent", command)
            self.assertIn("--always-approve", command)
            self.assertEqual(command[command.index("--model") + 1], "grok-9.2")
            self.assertEqual(command[command.index("--reasoning-effort") + 1], "xhigh")
        self.assertIn("--no-memory", readonly)
        self.assertIn("--no-auto-update", readonly)

    def test_same_cwd_allows_only_one_active_implement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = JobManager(max_workers=2)
            try:
                first = manager.spawn(
                    mode="implement",
                    task="FAKE_SLEEP_SHORT",
                    cwd=directory,
                    timeout_seconds=30,
                )
                deadline = time.monotonic() + 5
                while manager.status(first["job_id"])["status"] == "queued":
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.02)
                with self.assertRaises(BridgeError) as caught:
                    manager.spawn(mode="implement", task="second", cwd=directory)
                self.assertEqual(caught.exception.code, "E_CWD_BUSY")
                manager.cancel(first["job_id"])
                self.assertEqual(
                    wait_terminal(manager, first["job_id"], timeout=10)["status"],
                    "cancelled",
                )
            finally:
                manager.close()

    def test_independent_managers_do_not_share_job_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = JobManager(max_workers=1)
            second = JobManager(max_workers=1)
            try:
                spawned = first.spawn(mode="plan", task="plan", cwd=directory)
                self.assertEqual(
                    wait_terminal(first, spawned["job_id"])["status"], "succeeded"
                )
                with self.assertRaises(BridgeError) as caught:
                    second.status(spawned["job_id"])
                self.assertEqual(caught.exception.code, "E_JOB_NOT_FOUND")
            finally:
                first.close()
                second.close()

    def test_queued_job_cannot_follow_replaced_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            blocker = base / "blocker"
            target = base / "target"
            detached = base / "detached"
            victim = base / "victim"
            blocker.mkdir()
            target.mkdir()
            victim.mkdir()
            manager = JobManager(max_workers=1)
            try:
                first = manager.spawn(
                    mode="research", task="FAKE_SLEEP_SHORT", cwd=str(blocker)
                )
                deadline = time.monotonic() + 5
                while manager.status(first["job_id"])["status"] == "queued":
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.02)
                second = manager.spawn(
                    mode="implement", task="CREATE_FILE", cwd=str(target)
                )
                target.rename(detached)
                target.symlink_to(victim, target_is_directory=True)
                status = wait_terminal(manager, second["job_id"], timeout=15)
                self.assertEqual(status["status"], "failed", status)
                self.assertEqual(status["errors"][0]["code"], "E_CWD_CHANGED")
                self.assertFalse((victim / "created_by_grok.txt").exists())
                manager.cancel(first["job_id"])
                wait_terminal(manager, first["job_id"], timeout=10)
            finally:
                manager.close()

    def test_correction_chain_is_bounded_and_non_branching(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = JobManager(max_workers=1)
            try:
                root = manager.spawn(mode="implement", task="root", cwd=directory)
                self.assertEqual(
                    wait_terminal(manager, root["job_id"])["status"], "succeeded"
                )
                first = manager.spawn(
                    mode="implement",
                    task="first correction",
                    cwd=directory,
                    correction_of_job_id=root["job_id"],
                )
                self.assertEqual(
                    wait_terminal(manager, first["job_id"])["status"], "succeeded"
                )
                with self.assertRaises(BridgeError) as branch:
                    manager.spawn(
                        mode="implement",
                        task="branch",
                        cwd=directory,
                        correction_of_job_id=root["job_id"],
                    )
                self.assertEqual(branch.exception.code, "E_CORRECTION_ALREADY_USED")
                with self.assertRaises(BridgeError) as limit:
                    manager.spawn(
                        mode="implement",
                        task="second correction is refused",
                        cwd=directory,
                        correction_of_job_id=first["job_id"],
                    )
                self.assertEqual(limit.exception.code, "E_CORRECTION_LIMIT")
            finally:
                manager.close()

    def test_correction_must_reference_latest_same_cwd_implementation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = JobManager(max_workers=1)
            try:
                root = manager.spawn(mode="implement", task="root", cwd=directory)
                self.assertEqual(
                    wait_terminal(manager, root["job_id"])["status"], "succeeded"
                )
                newer = manager.spawn(
                    mode="implement", task="newer unrelated work", cwd=directory
                )
                self.assertEqual(
                    wait_terminal(manager, newer["job_id"])["status"], "succeeded"
                )
                with self.assertRaises(BridgeError) as caught:
                    manager.spawn(
                        mode="implement",
                        task="stale correction",
                        cwd=directory,
                        correction_of_job_id=root["job_id"],
                    )
                self.assertEqual(caught.exception.code, "E_CORRECTION_PARENT")
            finally:
                manager.close()

    def test_await_result_long_polls_same_job_without_redelegation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = JobManager(max_workers=1)
            try:
                spawned = manager.spawn(
                    mode="research", task="FAKE_SLEEP_SHORT", cwd=directory
                )
                first = manager.await_result(
                    spawned["job_id"], after_revision=0, max_wait_seconds=1
                )
                self.assertTrue(first["wait_timed_out"])
                terminal = wait_terminal(manager, spawned["job_id"], timeout=10)
                self.assertEqual(terminal["status"], "succeeded", terminal)
                final = manager.await_result(
                    spawned["job_id"],
                    after_revision=first["revision"],
                    max_wait_seconds=1,
                )
                self.assertEqual(final["job_id"], spawned["job_id"])
                self.assertEqual(final["status"], "succeeded")
                self.assertEqual(len(manager.list()["jobs"]), 1)
            finally:
                manager.close()

    def test_cancel_stops_exact_active_job(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = JobManager(max_workers=1)
            try:
                spawned = manager.spawn(
                    mode="research",
                    task="FAKE_SLEEP",
                    cwd=directory,
                    timeout_seconds=30,
                )
                deadline = time.monotonic() + 5
                while manager.status(spawned["job_id"])["status"] == "queued":
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.02)
                manager.cancel(spawned["job_id"])
                self.assertEqual(
                    wait_terminal(manager, spawned["job_id"], timeout=10)["status"],
                    "cancelled",
                )
            finally:
                manager.close()

    def test_normal_flow_uses_bounded_low_token_defaults(self) -> None:
        self.assertEqual(DEFAULT_OUTPUT_CHARS, 16_000)
        self.assertEqual(DEFAULT_AWAIT_RESULT_CHARS, 12_000)

    def test_failure_modes_are_fail_closed_without_retry(self) -> None:
        cases = (
            ("LONG_OUTPUT", "E_OUTPUT_LIMIT"),
            ("EMPTY_RESPONSE", "E_EMPTY_RESULT"),
            ("MALFORMED_STDOUT", "E_ACP_PROTOCOL"),
            ("MODEL_MISMATCH", "E_MODEL_MISMATCH"),
            ("MODEL_SWITCH", "E_MODEL_SWITCHED"),
            ("LATE_MODEL_SWITCH", "E_MODEL_SWITCHED"),
            ("MODEL_FALLBACK_WARNING", "E_MODEL_FALLBACK"),
            ("STDERR_OVERFLOW", "E_STDERR_LIMIT"),
            ("TURN_LIMIT", "E_TURN_LIMIT"),
        )
        for task, expected in cases:
            with self.subTest(task=task), tempfile.TemporaryDirectory() as directory:
                manager = JobManager(max_workers=1)
                try:
                    spawned = manager.spawn(
                        mode="review",
                        task=task,
                        cwd=directory,
                        max_output_chars=1_000 if task == "LONG_OUTPUT" else 120_000,
                    )
                    status = wait_terminal(manager, spawned["job_id"])
                    self.assertEqual(status["status"], "failed", status)
                    self.assertEqual(status["errors"][0]["code"], expected)
                    self.assertEqual(len(manager.list()["jobs"]), 1)
                finally:
                    manager.close()

    def test_sensitive_environment_is_not_forwarded(self) -> None:
        with mock.patch.dict(
            os.environ, {"XAI_API_KEY": "xai-test-secret"}, clear=False
        ):
            with tempfile.TemporaryDirectory() as directory:
                manager = JobManager()
                try:
                    spawned = manager.spawn(
                        mode="research", task="ECHO_SECRET", cwd=directory
                    )
                    status = wait_terminal(manager, spawned["job_id"])
                    self.assertEqual(status["status"], "succeeded", status)
                    result = manager.result(spawned["job_id"])
                finally:
                    manager.close()
        self.assertEqual(result["answer"], "secret=")
        environment = _minimal_environment()
        self.assertNotIn("XAI_API_KEY", environment)
        self.assertNotIn("USER", environment)
        self.assertNotIn("LOGNAME", environment)

    def test_public_redaction_covers_nested_secrets_paths_and_email(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory) / "project"
            payload = {
                "url": "https://user:password@example.invalid/a?token=secret",
                "authorization": "Bearer secret",
                "contact": SYNTHETIC_EMAIL,
                "path": f"{cwd}/src/file.py",
                "usage": {"input_tokens": 10},
            }
            redacted = _redact_public_value(payload, cwd)
            encoded = json.dumps(redacted, sort_keys=True)
            self.assertNotIn(directory, encoded)
        for secret in ("password", "Bearer secret", SYNTHETIC_EMAIL):
            self.assertNotIn(secret, encoded)
        self.assertEqual(redacted["usage"]["input_tokens"], 10)
        self.assertIn("./src/file.py", redacted["path"])
        uppercase_alias = _redact_public_value(
            {"path": "/USERS/private-account/project/file.py"}
        )
        self.assertNotIn("private-account", uppercase_alias["path"])

    def test_probe_start_error_does_not_expose_binary_absolute_path(self) -> None:
        binary = "/opt/private-team/tools/custom-grok"
        with mock.patch(
            "grok_build_bridge._run_bounded_process",
            side_effect=PermissionError("synthetic"),
        ):
            with self.assertRaises(BridgeError) as caught:
                _run_probe_command([binary, "--version"])
        self.assertEqual(caught.exception.code, "E_PROBE_START")
        self.assertNotIn("/opt/private-team", caught.exception.message)
        self.assertIn("custom-grok", caught.exception.message)

    def test_remote_error_is_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = JobManager()
            try:
                spawned = manager.spawn(
                    mode="review", task="REMOTE_SECRET_ERROR", cwd=directory
                )
                status = wait_terminal(manager, spawned["job_id"])
            finally:
                manager.close()
        self.assertEqual(status["status"], "failed")
        encoded = json.dumps(status, sort_keys=True)
        self.assertNotIn("SYNTHETIC_REMOTE_SECRET", encoded)
        self.assertIn("[REDACTED]", encoded)

    def test_cwd_scope_refuses_broad_roots_and_symlink_loops(self) -> None:
        with self.assertRaises(BridgeError) as home_error:
            _validate_cwd(str(Path.home()))
        self.assertEqual(home_error.exception.code, "E_CWD_SCOPE")
        with tempfile.TemporaryDirectory() as directory:
            loop = Path(directory) / "loop"
            loop.symlink_to(loop)
            with self.assertRaises(BridgeError) as loop_error:
                _validate_cwd(str(loop))
        self.assertEqual(loop_error.exception.code, "E_CWD")

    def test_path_identity_failure_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            child = parent / "child"
            child.mkdir()
            with mock.patch(
                "grok_build_bridge.os.path.samefile",
                side_effect=PermissionError("synthetic"),
            ):
                with self.assertRaises(BridgeError) as caught:
                    _path_is_within(child, parent)
        self.assertEqual(caught.exception.code, "E_SCOPE_IDENTITY")

    def test_request_validation_rejects_invalid_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = JobManager()
            try:
                cases = (
                    ({"mode": "unknown", "task": "x", "cwd": directory}, "E_MODE"),
                    ({"mode": "plan", "task": "", "cwd": directory}, "E_TASK"),
                    (
                        {
                            "mode": "plan",
                            "task": "x",
                            "cwd": directory,
                            "web_access": "yes",
                        },
                        "E_WEB_ACCESS",
                    ),
                    (
                        {
                            "mode": "implement",
                            "task": "x",
                            "cwd": directory,
                            "paths": ["src"],
                        },
                        "E_ROUTE",
                    ),
                )
                for kwargs, code in cases:
                    with self.subTest(code=code), self.assertRaises(BridgeError) as caught:
                        manager.spawn(**kwargs)
                    self.assertEqual(caught.exception.code, code)
            finally:
                manager.close()


class MCPContractTests(unittest.TestCase):
    def test_initialize_and_tool_list_expose_v2_direct_contract(self) -> None:
        from mcp import grok_build_server as server

        initialized = server.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        )
        self.assertTrue(
            initialized["result"]["serverInfo"]["version"].startswith("0.2.0")
        )
        listed = server.handle_request(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        )
        tools = {item["name"]: item for item in listed["result"]["tools"]}
        self.assertIn("delegate_readonly", tools)
        self.assertIn("spawn_worker", tools)
        self.assertIn("await_result", tools)
        self.assertIn(
            "directly in the exact caller-provided cwd",
            tools["delegate_readonly"]["description"],
        )
        self.assertEqual(
            tools["delegate_readonly"]["inputSchema"]["properties"]["paths"][
                "maxItems"
            ],
            MAX_SCOPE_PATH_HINTS,
        )
        self.assertEqual(
            tools["delegate_readonly"]["inputSchema"]["properties"][
                "max_output_chars"
            ]["default"],
            16_000,
        )
        self.assertEqual(
            tools["await_result"]["inputSchema"]["properties"]["limit"][
                "default"
            ],
            12_000,
        )
        self.assertTrue(tools["setup"]["annotations"]["openWorldHint"])

    def test_delegate_rejects_explicit_null_paths(self) -> None:
        from mcp import grok_build_server as server

        response = server.call_tool(
            "delegate_readonly",
            {"mode": "review", "task": "review", "cwd": "/tmp", "paths": None},
        )
        self.assertTrue(response["isError"])
        payload = json.loads(response["content"][0]["text"])
        self.assertEqual(payload["error"]["code"], "E_PATHS")

    def test_unknown_method_and_invalid_arguments_are_bounded(self) -> None:
        from mcp import grok_build_server as server

        unknown = server.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "unknown", "params": {}}
        )
        self.assertEqual(unknown["error"]["code"], -32601)
        invalid = server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "status", "arguments": []},
            }
        )
        self.assertEqual(invalid["error"]["code"], -32602)


if __name__ == "__main__":
    unittest.main()
