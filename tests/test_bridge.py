from __future__ import annotations

import json
import errno
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

from grok_build_bridge import (  # noqa: E402
    BridgeError,
    ACPClient,
    JobCancelled,
    JobManager,
    JobTimedOut,
    MAX_GIT_OBJECT_CONTENT_BYTES,
    MAX_GIT_OBJECT_ENTRIES,
    MAX_GIT_TRACKED_ENTRIES,
    MAX_IGNORED_CONTENT_BYTES,
    MAX_IGNORED_FILES,
    _build_task_prompt,
    _catalog_refresh_is_clean,
    _extract_runtime_model_policy,
    _minimal_environment,
    _mark_owned_process_group,
    _open_stable_directory,
    _path_is_within,
    _parse_model_catalog,
    _public_git_snapshot,
    _redact_known_secrets,
    _redact_public_value,
    _same_snapshot,
    _snapshot_tree_fd,
    _validate_cwd,
    build_acp_command,
    git_snapshot,
    probe_grok,
    setup_grok,
    terminate_owned_process,
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


def wait_terminal(manager: JobManager, job_id: str, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = manager.status(job_id)
        if status["status"] in {"succeeded", "failed", "timed_out", "cancelled"}:
            return status
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish")


class BridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_binary = os.environ.get("GROK_BUILD_BIN")
        os.environ["GROK_BUILD_BIN"] = str(FAKE_GROK)

    def tearDown(self) -> None:
        if self.old_binary is None:
            os.environ.pop("GROK_BUILD_BIN", None)
        else:
            os.environ["GROK_BUILD_BIN"] = self.old_binary

    def test_probe_discovers_provider_default_without_a_version_constant(self) -> None:
        probe = probe_grok(force=True)
        self.assertTrue(probe["ready"])
        self.assertEqual(probe["selected_model"], "grok-9.2")
        self.assertEqual(probe["default_model"], "grok-9.2")
        self.assertEqual(probe["available_models"], ["grok-9.2", "grok-9.1"])
        self.assertEqual(
            probe["reasoning_effort_policy"], "highest_advertised_by_runtime_model"
        )
        self.assertTrue(all(probe["required_flags"].values()))

    def test_setup_attests_runtime_model_and_highest_supported_effort(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            setup = setup_grok(directory)
        self.assertTrue(setup["ready"])
        self.assertTrue(setup["runtime_attested"])
        self.assertEqual(setup["selected_model"], "grok-9.2")
        self.assertEqual(setup["selected_reasoning_effort"], "xhigh")
        self.assertEqual(setup["cwd"], ".")
        self.assertEqual(setup["catalog_cwd"], ".")
        self.assertEqual(setup["binary"], Path(setup["binary"]).name)
        self.assertEqual(
            setup["model_selection"]["selection_source"],
            "acp_initialize_model_state",
        )

    def test_runtime_policy_uses_provider_default_and_highest_advertised_effort(self) -> None:
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

    def test_runtime_policy_refuses_unknown_effort_ranking(self) -> None:
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

    def test_model_catalog_parser_rejects_conflicting_default_signals(self) -> None:
        catalog = _parse_model_catalog(
            "Default model: grok-a\nAvailable models:\n  * grok-b (default)\n  - grok-a\n"
        )
        self.assertTrue(catalog["default_model_ambiguous"])
        self.assertIsNone(catalog["default_model"])

    def test_probe_rejects_stale_cached_catalog_even_when_command_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "stale-models-grok"
            binary.write_bytes(FAKE_GROK.read_bytes())
            binary.chmod(0o755)
            os.environ["GROK_BUILD_BIN"] = str(binary)
            probe = probe_grok(force=True)
        self.assertFalse(probe["ready"])
        self.assertFalse(probe["online_model_check_confirmed"])
        self.assertEqual(probe["selected_model"], "grok-9.2")

    def test_catalog_refresh_rejects_known_cache_and_retry_failures(self) -> None:
        catalog = (
            "Default model: grok-9.2\n"
            "Available models:\n"
            "  * grok-9.2 (default)\n"
            "  - grok-9.1\n"
        )
        markers = (
            "model catalog refresh failed",
            "model catalog: all retries exhausted",
            "models cache is stale",
            "models cache origin mismatch",
        )
        for marker in markers:
            with self.subTest(marker=marker):
                self.assertFalse(_catalog_refresh_is_clean(catalog + marker, 0))

    def test_probe_binds_catalog_discovery_to_target_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            (target / ".fake-grok-cwd-marker").touch()
            probe = probe_grok(force=True, cwd=target)
        self.assertEqual(probe["catalog_cwd"], str(target.resolve()))
        self.assertIn("Probe cwd marker observed", probe["model_diagnostics"])

    def test_probe_output_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "large-probe"
            binary.write_text(
                "#!/usr/bin/env python3\nimport sys\nsys.stdout.write('x' * 2000001)\n",
                encoding="utf-8",
            )
            binary.chmod(0o755)
            os.environ["GROK_BUILD_BIN"] = str(binary)
            with self.assertRaises(BridgeError) as caught:
                probe_grok(force=True)
            self.assertEqual(caught.exception.code, "E_PROBE_OUTPUT_LIMIT")

    def test_readonly_job_returns_receipt_without_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = JobManager()
            try:
                spawned = manager.spawn(
                    mode="research", task="Find the architecture", cwd=directory
                )
                self.assertEqual(spawned["cwd"], ".")
                status = wait_terminal(manager, spawned["job_id"])
                self.assertEqual(status["status"], "succeeded", status)
                result = manager.result(spawned["job_id"])
                self.assertEqual(result["cwd"], ".")
                self.assertEqual(result["answer"], "fake Grok public answer")
                self.assertEqual(result["model"], "grok-9.2")
                self.assertEqual(result["reasoning_effort"], "xhigh")
                self.assertTrue(result["model_evidence"]["runtime_attested"])
                self.assertEqual(result["loop_guard"]["automatic_retries"], 0)
                self.assertFalse(result["verification"]["verified"])
                self.assertFalse(result["verification"]["review_required"])
            finally:
                manager.close()

    def test_readonly_job_fails_if_git_state_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_git(root, "init")
            run_git(root, "config", "user.email", "test.invalid")
            run_git(root, "config", "user.name", "Test")
            (root / "seed.txt").write_text("seed\n", encoding="utf-8")
            run_git(root, "add", "seed.txt")
            run_git(root, "commit", "-m", "seed")
            manager = JobManager()
            try:
                spawned = manager.spawn(
                    mode="review", task="CREATE_FILE", cwd=str(root)
                )
                status = wait_terminal(manager, spawned["job_id"])
                self.assertEqual(status["status"], "failed")
                self.assertEqual(status["errors"][0]["code"], "E_READONLY_CHANGED")
            finally:
                manager.close()

    def test_every_readonly_mode_fails_if_an_ignored_file_changes(self) -> None:
        for mode in ("research", "plan", "review"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                run_git(root, "init")
                run_git(root, "config", "user.email", "test.invalid")
                run_git(root, "config", "user.name", "Test")
                (root / ".gitignore").write_text("cache.tmp\n", encoding="utf-8")
                (root / "seed.txt").write_text("seed\n", encoding="utf-8")
                (root / "cache.tmp").write_text("before\n", encoding="utf-8")
                run_git(root, "add", ".gitignore", "seed.txt")
                run_git(root, "commit", "-m", "seed")
                manager = JobManager()
                try:
                    spawned = manager.spawn(
                        mode=mode, task="MODIFY_IGNORED", cwd=str(root)
                    )
                    status = wait_terminal(manager, spawned["job_id"])
                    self.assertEqual(status["status"], "failed")
                    self.assertEqual(status["errors"][0]["code"], "E_READONLY_CHANGED")
                finally:
                    manager.close()

    def test_non_git_readonly_job_fails_if_it_creates_a_file(self) -> None:
        for mode in ("research", "plan", "review"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                manager = JobManager()
                try:
                    spawned = manager.spawn(
                        mode=mode, task="CREATE_FILE", cwd=directory
                    )
                    status = wait_terminal(manager, spawned["job_id"])
                    self.assertEqual(status["status"], "failed")
                    self.assertEqual(status["errors"][0]["code"], "E_READONLY_CHANGED")
                    self.assertTrue(
                        (Path(directory) / "created_by_grok.txt").is_file()
                    )
                finally:
                    manager.close()

    def test_worker_git_admin_mutation_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            primary = base / "primary"
            linked = base / "linked"
            primary.mkdir()
            run_git(primary, "init")
            run_git(primary, "config", "user.email", "test.invalid")
            run_git(primary, "config", "user.name", "Test")
            (primary / "seed.txt").write_text("seed\n", encoding="utf-8")
            run_git(primary, "add", "seed.txt")
            run_git(primary, "commit", "-m", "seed")
            run_git(primary, "worktree", "add", "-b", "grok-admin", str(linked))
            manager = JobManager()
            try:
                spawned = manager.spawn(
                    mode="implement", task="MODIFY_GITDIR", cwd=str(linked)
                )
                status = wait_terminal(manager, spawned["job_id"])
                self.assertEqual(status["status"], "failed")
                self.assertEqual(status["errors"][0]["code"], "E_GIT_ADMIN_CHANGED")
                git_dir = Path(run_git(linked, "rev-parse", "--absolute-git-dir").strip())
                self.assertTrue((git_dir / "locked").is_file())
                self.assertTrue((git_dir / "hooks" / "grok-synthetic-hook").is_file())
                self.assertTrue((git_dir / "config.worktree").is_file())
            finally:
                manager.close()

    def test_readonly_git_admin_mutation_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_git(root, "init")
            run_git(root, "config", "user.email", "test.invalid")
            run_git(root, "config", "user.name", "Test")
            (root / "seed.txt").write_text("seed\n", encoding="utf-8")
            run_git(root, "add", "seed.txt")
            run_git(root, "commit", "-m", "seed")
            manager = JobManager()
            try:
                spawned = manager.spawn(
                    mode="review", task="MODIFY_GITDIR", cwd=str(root)
                )
                status = wait_terminal(manager, spawned["job_id"])
                self.assertEqual(status["status"], "failed")
                self.assertEqual(
                    status["errors"][0]["code"], "E_GIT_ADMIN_CHANGED"
                )
            finally:
                manager.close()

    def test_git_diff_snapshot_never_executes_repository_textconv(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "textconv-executed"
            run_git(root, "init")
            run_git(root, "config", "user.email", "test.invalid")
            run_git(root, "config", "user.name", "Test")
            run_git(
                root,
                "config",
                "diff.spy.textconv",
                f"sh -c 'touch {marker}'",
            )
            (root / ".gitattributes").write_text("*.spy diff=spy\n", encoding="utf-8")
            (root / "data.spy").write_text("before\n", encoding="utf-8")
            run_git(root, "add", ".gitattributes", "data.spy")
            run_git(root, "commit", "-m", "seed")
            (root / "data.spy").write_text("after\n", encoding="utf-8")
            snapshot = git_snapshot(root, include_ignored=True)
            self.assertIsNotNone(snapshot)
            self.assertFalse(marker.exists(), "snapshot executed a repository textconv")

    def test_git_snapshot_refuses_configured_external_filters_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "filter-executed"
            run_git(root, "init")
            run_git(root, "config", "user.email", "test.invalid")
            run_git(root, "config", "user.name", "Test")
            (root / ".gitattributes").write_text("*.evil filter=evil\n", encoding="utf-8")
            (root / "data.evil").write_text("before\n", encoding="utf-8")
            run_git(root, "add", ".gitattributes", "data.evil")
            run_git(root, "commit", "-m", "seed")
            run_git(root, "config", "filter.evil.clean", f"touch {marker}")
            (root / "data.evil").write_text("after\n", encoding="utf-8")
            with self.assertRaises(BridgeError) as caught:
                git_snapshot(root, include_ignored=True)
            self.assertEqual(caught.exception.code, "E_GIT_CONFIG_EXTERNAL")
            self.assertFalse(marker.exists(), "snapshot executed a repository filter")

    def test_git_snapshot_refuses_external_config_include_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "repository"
            root.mkdir()
            marker = base / "included-filter-executed"
            included = base / "outside.gitconfig"
            run_git(root, "init")
            run_git(root, "config", "user.email", "test.invalid")
            run_git(root, "config", "user.name", "Test")
            (root / ".gitattributes").write_text("*.evil filter=evil\n", encoding="utf-8")
            (root / "data.evil").write_text("before\n", encoding="utf-8")
            run_git(root, "add", ".gitattributes", "data.evil")
            run_git(root, "commit", "-m", "seed")
            included.write_text(
                f"[filter \"evil\"]\n\tclean = touch {marker}\n",
                encoding="utf-8",
            )
            run_git(root, "config", "include.path", str(included))
            (root / "data.evil").write_text("after\n", encoding="utf-8")
            with self.assertRaises(BridgeError) as caught:
                git_snapshot(root, include_ignored=True)
            self.assertEqual(caught.exception.code, "E_GIT_CONFIG_EXTERNAL")
            self.assertFalse(marker.exists(), "snapshot executed an included filter")

    def test_git_snapshot_rejects_core_worktree_outside_delegated_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "repository"
            outside = base / "outside-worktree"
            root.mkdir()
            outside.mkdir()
            run_git(root, "init")
            run_git(root, "config", "core.worktree", str(outside))
            with self.assertRaises(BridgeError) as caught:
                git_snapshot(root, include_ignored=True)
            self.assertEqual(caught.exception.code, "E_GIT_SCOPE")

    def test_git_admin_snapshot_includes_index_lock_and_object_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_git(root, "init")
            run_git(root, "config", "user.email", "test.invalid")
            run_git(root, "config", "user.name", "Test")
            (root / "seed.txt").write_text("seed\n", encoding="utf-8")
            run_git(root, "add", "seed.txt")
            run_git(root, "commit", "-m", "seed")
            before = git_snapshot(root, include_ignored=True)
            self.assertIsNotNone(before)
            git_dir = (root / run_git(root, "rev-parse", "--git-dir").strip()).resolve()
            (git_dir / "index.lock").write_text("synthetic lock\n", encoding="utf-8")
            after_lock = git_snapshot(root, include_ignored=True)
            self.assertIsNotNone(after_lock)
            self.assertNotEqual(
                before["git_admin_sha256"], after_lock["git_admin_sha256"]
            )
            (git_dir / "index.lock").unlink()
            object_file = git_dir / "objects" / "synthetic-object"
            object_file.write_text("synthetic object content\n", encoding="utf-8")
            after_object = git_snapshot(root, include_ignored=True)
            self.assertIsNotNone(after_object)
            self.assertNotEqual(
                before["git_admin_sha256"], after_object["git_admin_sha256"]
            )

    def test_git_admin_snapshot_rejects_alternate_object_databases(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "repository"
            alternate = base / "alternate-objects"
            root.mkdir()
            alternate.mkdir()
            run_git(root, "init")
            run_git(root, "config", "user.email", "test.invalid")
            run_git(root, "config", "user.name", "Test")
            (root / "seed.txt").write_text("seed\n", encoding="utf-8")
            run_git(root, "add", "seed.txt")
            run_git(root, "commit", "-m", "seed")
            git_dir = (root / run_git(root, "rev-parse", "--git-dir").strip()).resolve()
            (git_dir / "objects" / "info" / "alternates").write_text(
                f"{alternate}\n", encoding="utf-8"
            )
            with self.assertRaises(BridgeError) as caught:
                git_snapshot(root, include_ignored=True)
            self.assertEqual(caught.exception.code, "E_GIT_OBJECT_ALTERNATES")

    def test_git_scope_rejects_tracked_symlink_outside_repository(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "repository"
            root.mkdir()
            outside = base / "outside.txt"
            outside.write_text("outside\n", encoding="utf-8")
            run_git(root, "init")
            run_git(root, "config", "user.email", "test.invalid")
            run_git(root, "config", "user.name", "Test")
            (root / "escape").symlink_to(outside)
            run_git(root, "add", "escape")
            run_git(root, "commit", "-m", "tracked symlink")
            with self.assertRaises(BridgeError) as caught:
                git_snapshot(root, include_ignored=True)
            self.assertEqual(caught.exception.code, "E_CWD_SCOPE_SYMLINK_SCOPE")

    def test_queued_job_cannot_follow_replaced_cwd_into_victim(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            blocker = base / "blocker"
            target = base / "target"
            detached = base / "detached-target"
            victim = base / "victim"
            blocker.mkdir()
            target.mkdir()
            victim.mkdir()
            manager = JobManager(max_workers=1)
            try:
                first = manager.spawn(
                    mode="research",
                    task="FAKE_SLEEP_SHORT",
                    cwd=str(blocker),
                    timeout_seconds=60,
                )
                deadline = time.monotonic() + 5
                while manager.status(first["job_id"])["status"] == "queued":
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.02)
                second = manager.spawn(
                    mode="research", task="CREATE_FILE", cwd=str(target)
                )
                self.assertEqual(manager.status(second["job_id"])["status"], "queued")
                target.rename(detached)
                target.symlink_to(victim, target_is_directory=True)
                status = wait_terminal(manager, second["job_id"], timeout=15)
                self.assertNotEqual(
                    status["status"],
                    "succeeded",
                    "a replaced cwd must not allow a read-only job to succeed against the victim",
                )
                self.assertFalse((victim / "created_by_grok.txt").exists())
                self.assertIn(
                    status["errors"][0]["code"],
                    {"E_CWD_CHANGED", "E_READONLY_CHANGED"},
                )
            finally:
                manager.cancel(first["job_id"])
                wait_terminal(manager, first["job_id"], timeout=10)
                manager.close()

    def test_worker_deny_rules_cover_git_admin_and_remote_mutators(self) -> None:
        command = build_acp_command(
            binary=str(FAKE_GROK),
            cwd=Path("/tmp/grok-test-worktree"),
            mode="implement",
            max_turns=10,
            web_access=False,
            model="grok-9.2",
            reasoning_effort="xhigh",
        )
        rules = [
            command[index + 1]
            for index, value in enumerate(command[:-1])
            if value == "--deny"
        ]
        disallowed = [
            command[index + 1]
            for index, value in enumerate(command[:-1])
            if value == "--disallowed-tools"
        ]
        self.assertIn(
            "run_terminal_cmd,Agent",
            disallowed,
            "implementation must disable the real shell tool ID and recursive Agent tool",
        )
        self.assertNotIn("Bash", disallowed, "Bash is not the Grok shell tool ID")
        for tool in ("Write", "Edit"):
            self.assertTrue(
                any(rule.startswith(f"{tool}(") and ".git" in rule for rule in rules),
                f"missing {tool} deny for .git admin paths: {rules}",
            )
        for operation in (
            "fetch",
            "pull",
            "remote",
            "config",
            "submodule",
            "clone",
            "init",
            "add",
            "update-ref",
            "symbolic-ref",
            "hash-object",
            "gc",
            "maintenance",
            "pack-refs",
        ):
            self.assertTrue(
                any(
                    rule.startswith("Bash(") and f" {operation}" in rule
                    for rule in rules
                ),
                f"missing git {operation} deny rule: {rules}",
            )

    def test_malformed_acp_stdout_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = JobManager()
            try:
                spawned = manager.spawn(
                    mode="research", task="MALFORMED_STDOUT", cwd=directory
                )
                status = wait_terminal(manager, spawned["job_id"])
                self.assertEqual(status["status"], "failed")
                self.assertEqual(status["errors"][0]["code"], "E_ACP_PROTOCOL")
            finally:
                manager.close()

    def test_task_text_is_not_put_in_process_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            marker = "private marker ; $(touch should-not-run)"
            target = str(Path(directory).resolve())
            task = f"{marker}\nScope: {target}/src"
            command = build_acp_command(
                binary=str(FAKE_GROK),
                cwd=Path(directory),
                mode="research",
                max_turns=10,
                web_access=False,
                model="grok-9.2",
                reasoning_effort="xhigh",
            )
            prompt = _build_task_prompt(
                "research", task, Path(directory), "grok-9.2", "xhigh"
            )
            self.assertNotIn(task, command)
            self.assertIn(marker, prompt)
            self.assertIn("grok-9.2", command)
            self.assertNotIn(target, prompt)
            self.assertIn("Scope: ./src", prompt)
            self.assertIn("WORKING DIRECTORY: .", prompt)

    def test_optional_memory_flag_is_never_unconditional_or_duplicated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            common = {
                "binary": str(FAKE_GROK),
                "cwd": Path(directory),
                "mode": "plan",
                "max_turns": 10,
                "web_access": False,
            }
            unsupported = build_acp_command(**common, supports_no_memory=False)
            supported = build_acp_command(**common, supports_no_memory=True)
            self.assertEqual(unsupported.count("--no-memory"), 0)
            self.assertEqual(supported.count("--no-memory"), 1)

    def test_output_limit_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = JobManager()
            try:
                spawned = manager.spawn(
                    mode="research",
                    task="LONG_OUTPUT",
                    cwd=directory,
                    max_output_chars=1_000,
                )
                status = wait_terminal(manager, spawned["job_id"])
                self.assertEqual(status["status"], "failed")
                self.assertEqual(status["errors"][0]["code"], "E_OUTPUT_LIMIT")
                result = manager.result(spawned["job_id"])
                self.assertFalse(result["result_available"])
            finally:
                manager.close()

    def test_empty_public_answer_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = JobManager()
            try:
                spawned = manager.spawn(
                    mode="plan", task="EMPTY_RESPONSE", cwd=directory
                )
                status = wait_terminal(manager, spawned["job_id"])
                self.assertEqual(status["status"], "failed")
                self.assertEqual(status["errors"][0]["code"], "E_EMPTY_RESULT")
            finally:
                manager.close()

    def test_acp_reported_model_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = JobManager()
            try:
                spawned = manager.spawn(
                    mode="plan", task="MODEL_MISMATCH", cwd=directory
                )
                status = wait_terminal(manager, spawned["job_id"])
                self.assertEqual(status["status"], "failed")
                self.assertEqual(status["errors"][0]["code"], "E_MODEL_MISMATCH")
            finally:
                manager.close()

    def test_runtime_model_switch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = JobManager()
            try:
                spawned = manager.spawn(
                    mode="plan", task="MODEL_SWITCH", cwd=directory
                )
                status = wait_terminal(manager, spawned["job_id"])
                self.assertEqual(status["status"], "failed")
                self.assertEqual(status["errors"][0]["code"], "E_MODEL_SWITCHED")
            finally:
                manager.close()

    def test_late_runtime_model_switch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = JobManager()
            try:
                spawned = manager.spawn(
                    mode="plan", task="LATE_MODEL_SWITCH", cwd=directory
                )
                status = wait_terminal(manager, spawned["job_id"])
                self.assertEqual(status["status"], "failed")
                self.assertEqual(status["errors"][0]["code"], "E_MODEL_SWITCHED")
            finally:
                manager.close()

    def test_runtime_model_fallback_warning_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = JobManager()
            try:
                spawned = manager.spawn(
                    mode="plan", task="MODEL_FALLBACK_WARNING", cwd=directory
                )
                status = wait_terminal(manager, spawned["job_id"])
                self.assertEqual(status["status"], "failed")
                self.assertEqual(status["errors"][0]["code"], "E_MODEL_FALLBACK")
            finally:
                manager.close()

    def test_fallback_after_stderr_receipt_limit_is_still_detected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = JobManager()
            try:
                spawned = manager.spawn(
                    mode="plan", task="FALLBACK_AFTER_STDERR_LIMIT", cwd=directory
                )
                status = wait_terminal(manager, spawned["job_id"])
                self.assertEqual(status["status"], "failed")
                self.assertEqual(status["errors"][0]["code"], "E_MODEL_FALLBACK")
            finally:
                manager.close()

    def test_stderr_truncation_without_known_warning_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = JobManager()
            try:
                spawned = manager.spawn(
                    mode="plan", task="STDERR_OVERFLOW", cwd=directory
                )
                status = wait_terminal(manager, spawned["job_id"])
                self.assertEqual(status["status"], "failed")
                self.assertEqual(status["errors"][0]["code"], "E_STDERR_LIMIT")
            finally:
                manager.close()

    def test_turn_limit_stops_without_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = JobManager()
            try:
                spawned = manager.spawn(
                    mode="plan", task="TURN_LIMIT", cwd=directory, max_turns=2
                )
                status = wait_terminal(manager, spawned["job_id"])
                self.assertEqual(status["status"], "failed")
                self.assertEqual(status["errors"][0]["code"], "E_TURN_LIMIT")
                self.assertEqual(status["loop_guard"]["automatic_retries"], 0)
                self.assertFalse(status["loop_guard"]["automatic_redelegation"])
            finally:
                manager.close()

    def test_web_access_requires_boolean(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = JobManager()
            try:
                with self.assertRaises(BridgeError) as caught:
                    manager.spawn(
                        mode="research",
                        task="test",
                        cwd=directory,
                        web_access="false",  # type: ignore[arg-type]
                    )
                self.assertEqual(caught.exception.code, "E_WEB_ACCESS")
            finally:
                manager.close()

    def test_cwd_scope_rejects_broad_account_temporary_and_system_roots(self) -> None:
        candidates = {
            Path.home().resolve().parent,
            Path("/tmp").resolve(),
            Path("/etc").resolve(),
        }
        for candidate in candidates:
            if not candidate.is_dir():
                continue
            with self.subTest(candidate=candidate):
                with self.assertRaises(BridgeError) as caught:
                    _validate_cwd(str(candidate))
                self.assertEqual(caught.exception.code, "E_CWD_SCOPE")
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(_validate_cwd(directory), Path(directory).resolve())

    def test_cwd_scope_uses_filesystem_identity_on_case_folding_filesystems(self) -> None:
        home = Path.home().resolve()
        variants = []
        if str(home).startswith("/Users/"):
            variants.extend(
                (Path("/USERS"), Path("/USERS") / home.relative_to("/Users"))
            )
        variants.extend((Path("/LIBRARY"), Path("/SYSTEM")))
        exercised = False
        for candidate in variants:
            try:
                same_target = candidate.is_dir() and any(
                    os.path.samefile(candidate, expected)
                    for expected in (
                        home.parent,
                        home,
                        Path("/Library"),
                        Path("/System"),
                    )
                    if expected.exists()
                )
            except OSError:
                same_target = False
            if not same_target:
                continue
            exercised = True
            with self.subTest(candidate=candidate):
                with self.assertRaises(BridgeError) as caught:
                    _validate_cwd(str(candidate))
                self.assertEqual(caught.exception.code, "E_CWD_SCOPE")
        if sys.platform == "darwin":
            self.assertTrue(exercised, "expected a case-folded path alias on macOS")

    def test_cwd_scope_fails_closed_when_identity_cannot_be_proved(self) -> None:
        failures = (
            PermissionError("synthetic permission denial"),
            FileNotFoundError("synthetic identity race"),
            OSError(errno.ELOOP, "synthetic symlink loop"),
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                with tempfile.TemporaryDirectory() as directory, mock.patch(
                    "grok_build_bridge.os.path.samefile", side_effect=failure
                ):
                    with self.assertRaises(BridgeError) as caught:
                        _validate_cwd(directory)
                self.assertEqual(caught.exception.code, "E_CWD_SCOPE")

    def test_path_containment_rejects_identity_race_before_ancestor_match(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            child = parent / "child"
            child.mkdir()
            with mock.patch(
                "grok_build_bridge.os.path.samefile",
                side_effect=FileNotFoundError("synthetic target disappeared"),
            ):
                with self.assertRaises(BridgeError) as caught:
                    _path_is_within(child, parent)
        self.assertEqual(caught.exception.code, "E_SCOPE_IDENTITY")

    def test_cwd_symlink_loop_returns_stable_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            loop = Path(directory) / "loop"
            loop.symlink_to(loop)
            with self.assertRaises(BridgeError) as caught:
                _validate_cwd(str(loop))
        self.assertEqual(caught.exception.code, "E_CWD")

    def test_non_git_readonly_detects_metadata_only_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            (target / "metadata.txt").write_text("stable content\n", encoding="utf-8")
            manager = JobManager()
            try:
                spawned = manager.spawn(
                    mode="review", task="TOUCH_FILE_METADATA", cwd=directory
                )
                status = wait_terminal(manager, spawned["job_id"])
                self.assertEqual(status["status"], "failed")
                self.assertEqual(status["errors"][0]["code"], "E_READONLY_CHANGED")
            finally:
                manager.close()

    def test_bounded_tree_accepts_exact_limits_and_rejects_one_over(self) -> None:
        self.assertEqual(MAX_IGNORED_FILES, 20_000)
        self.assertEqual(MAX_IGNORED_CONTENT_BYTES, 128_000_000)
        self.assertEqual(MAX_GIT_TRACKED_ENTRIES, 200_000)
        self.assertEqual(MAX_GIT_OBJECT_ENTRIES, 200_000)
        self.assertEqual(MAX_GIT_OBJECT_CONTENT_BYTES, 512_000_000)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a").write_bytes(b"a")
            (root / "b").write_bytes(b"b")
            descriptor = _open_stable_directory(root)
            try:
                exact = _snapshot_tree_fd(
                    descriptor,
                    code_prefix="TEST",
                    label="test tree",
                    root_path=root,
                    max_entries=2,
                    max_content_bytes=2,
                )
                self.assertEqual(exact["entry_count"], 2)
                self.assertEqual(exact["content_bytes"], 2)
                with self.assertRaises(BridgeError) as entries:
                    _snapshot_tree_fd(
                        descriptor,
                        code_prefix="TEST",
                        label="test tree",
                        root_path=root,
                        max_entries=1,
                        max_content_bytes=2,
                    )
                self.assertEqual(entries.exception.code, "E_TEST_SNAPSHOT_LIMIT")
                with self.assertRaises(BridgeError) as content:
                    _snapshot_tree_fd(
                        descriptor,
                        code_prefix="TEST",
                        label="test tree",
                        root_path=root,
                        max_entries=2,
                        max_content_bytes=1,
                    )
                self.assertEqual(content.exception.code, "E_TEST_SNAPSHOT_LIMIT")
            finally:
                os.close(descriptor)

    def test_scope_rejects_symlinked_git_marker_before_git_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            target = base / "target"
            nested_target = base / "nested-target"
            external_admin = base / "external-admin"
            target.mkdir()
            (nested_target / "child").mkdir(parents=True)
            external_admin.mkdir()
            (target / ".git").symlink_to(external_admin, target_is_directory=True)
            with self.assertRaises(BridgeError) as caught:
                git_snapshot(target, include_ignored=True)
            self.assertEqual(caught.exception.code, "E_CWD_SCOPE_SYMLINK_SCOPE")
            (nested_target / "child" / ".git").symlink_to(
                external_admin, target_is_directory=True
            )
            with self.assertRaises(BridgeError) as nested:
                git_snapshot(nested_target, include_ignored=True)
            self.assertEqual(nested.exception.code, "E_CWD_SCOPE_SYMLINK_SCOPE")

    def test_non_git_scope_rejects_symlink_target_outside_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            target = base / "target"
            target.mkdir()
            outside = base / "outside.txt"
            outside.write_text("outside\n", encoding="utf-8")
            (target / "escape").symlink_to(outside)
            manager = JobManager()
            try:
                spawned = manager.spawn(mode="plan", task="inspect", cwd=str(target))
                status = wait_terminal(manager, spawned["job_id"])
                self.assertEqual(status["status"], "failed")
                self.assertEqual(
                    status["errors"][0]["code"], "E_CWD_SCOPE_SYMLINK_SCOPE"
                )
            finally:
                manager.close()

    def test_scope_allows_internal_symlink_and_rejects_dangling_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            internal = base / "inside.txt"
            internal.write_text("inside\n", encoding="utf-8")
            (base / "inside-link").symlink_to(internal)
            manager = JobManager()
            try:
                spawned = manager.spawn(mode="plan", task="inspect", cwd=directory)
                status = wait_terminal(manager, spawned["job_id"])
                self.assertEqual(status["status"], "succeeded", status)
            finally:
                manager.close()
            (base / "inside-link").unlink()
            (base / "dangling-link").symlink_to(base / "missing.txt")
            manager = JobManager()
            try:
                spawned = manager.spawn(mode="plan", task="inspect", cwd=directory)
                status = wait_terminal(manager, spawned["job_id"])
                self.assertEqual(status["status"], "failed")
                self.assertEqual(
                    status["errors"][0]["code"], "E_CWD_SCOPE_SYMLINK_SCOPE"
                )
            finally:
                manager.close()

    def test_git_subdirectory_scope_rejects_symlink_to_repository_sibling(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            sibling = root / "sibling"
            target.mkdir()
            sibling.mkdir()
            (sibling / "private.txt").write_text("outside delegated cwd\n", encoding="utf-8")
            (target / "escape").symlink_to(sibling)
            run_git(root, "init")
            run_git(root, "config", "user.email", "test.invalid")
            run_git(root, "config", "user.name", "Test")
            run_git(root, "add", "target/escape", "sibling/private.txt")
            run_git(root, "commit", "-m", "seed")
            manager = JobManager()
            try:
                spawned = manager.spawn(mode="review", task="inspect", cwd=str(target))
                status = wait_terminal(manager, spawned["job_id"])
                self.assertEqual(status["status"], "failed")
                self.assertEqual(
                    status["errors"][0]["code"], "E_CWD_SCOPE_SYMLINK_SCOPE"
                )
            finally:
                manager.close()

    def test_sensitive_environment_is_not_forwarded(self) -> None:
        old_key = os.environ.get("XAI_API_KEY")
        os.environ["XAI_API_KEY"] = "xai-test-secret-value"
        try:
            with tempfile.TemporaryDirectory() as directory:
                manager = JobManager()
                try:
                    spawned = manager.spawn(
                        mode="research", task="ECHO_SECRET", cwd=directory
                    )
                    status = wait_terminal(manager, spawned["job_id"])
                    self.assertEqual(status["status"], "succeeded")
                    result = manager.result(spawned["job_id"])
                    self.assertNotIn("xai-test-secret-value", result["answer"])
                    self.assertEqual(result["answer"], "secret=")
                finally:
                    manager.close()
        finally:
            if old_key is None:
                os.environ.pop("XAI_API_KEY", None)
            else:
                os.environ["XAI_API_KEY"] = old_key

    def test_account_identity_environment_is_not_forwarded(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"USER": "local-account", "LOGNAME": "local-account"},
            clear=False,
        ):
            child_environment = _minimal_environment()
        self.assertNotIn("USER", child_environment)
        self.assertNotIn("LOGNAME", child_environment)

    def test_known_secret_is_redacted_from_error_text(self) -> None:
        old_key = os.environ.get("XAI_API_KEY")
        os.environ["XAI_API_KEY"] = "xai-error-secret-value"
        try:
            error = BridgeError("E_TEST", "failed with xai-error-secret-value")
            self.assertNotIn("xai-error-secret-value", error.message)
            self.assertIn("[REDACTED_ENV:XAI_API_KEY]", error.message)
            self.assertEqual(
                _redact_known_secrets("xai-error-secret-value"),
                "[REDACTED_ENV:XAI_API_KEY]",
            )
        finally:
            if old_key is None:
                os.environ.pop("XAI_API_KEY", None)
            else:
                os.environ["XAI_API_KEY"] = old_key

    def test_public_redaction_covers_nested_credential_shapes(self) -> None:
        payload = {
            "url": (
                "http://127.0.0.1:9999/mcp?sc_token="
                "SYNTHETIC_QUERY_SECRET"
            ),
            "usage": {
                "input_tokens": 10,
                "diagnostic": "Authorization: Bearer SYNTHETIC_BEARER_SECRET",
            },
            "userinfo": (
                "https://synthetic-user:SYNTHETIC_USERINFO_SECRET@"
                "example.invalid/path"
            ),
            "contact": SYNTHETIC_EMAIL,
            "api_key": "SYNTHETIC_STRUCTURED_SECRET",
            "items": ["Cookie: session=SYNTHETIC_COOKIE_SECRET"],
        }
        redacted = _redact_public_value(payload)
        encoded = json.dumps(redacted, sort_keys=True)
        for secret in (
            "SYNTHETIC_QUERY_SECRET",
            "SYNTHETIC_BEARER_SECRET",
            "SYNTHETIC_USERINFO_SECRET",
            "SYNTHETIC_STRUCTURED_SECRET",
            "SYNTHETIC_COOKIE_SECRET",
            SYNTHETIC_EMAIL,
        ):
            self.assertNotIn(secret, encoded)
        self.assertEqual(redacted["usage"]["input_tokens"], 10)
        self.assertEqual(redacted["api_key"], "[REDACTED]")

    def test_public_redaction_recurses_through_objects_and_arrays_with_valid_json(self) -> None:
        payload = {
            "response": [
                {
                    "metadata": {
                        "envelope": {
                            "access-token": "SYNTHETIC_DEEP_ACCESS_TOKEN",
                            "details": [
                                {
                                    "authorization": (
                                        "Bearer SYNTHETIC_DEEP_BEARER_SECRET"
                                    )
                                },
                                [
                                    {
                                        "api_key": "SYNTHETIC_DEEP_API_KEY"
                                    }
                                ],
                            ],
                        }
                    }
                },
                [
                    {
                        "safe": "SYNTHETIC_DEEP_SAFE_TEXT",
                        "nested_secret": "SYNTHETIC_DEEP_NESTED_SECRET",
                    }
                ],
            ]
        }

        redacted = _redact_public_value(payload)
        encoded = json.dumps(redacted, ensure_ascii=False, sort_keys=True)
        decoded = json.loads(encoded)

        for secret in (
            "SYNTHETIC_DEEP_ACCESS_TOKEN",
            "SYNTHETIC_DEEP_BEARER_SECRET",
            "SYNTHETIC_DEEP_API_KEY",
            "SYNTHETIC_DEEP_NESTED_SECRET",
        ):
            self.assertNotIn(secret, encoded)
        self.assertEqual(decoded, redacted)
        self.assertEqual(
            decoded["response"][0]["metadata"]["envelope"]["access-token"],
            "[REDACTED]",
        )
        self.assertEqual(
            decoded["response"][0]["metadata"]["envelope"]["details"][0][
                "authorization"
            ],
            "[REDACTED]",
        )
        self.assertEqual(
            decoded["response"][0]["metadata"]["envelope"]["details"][1][0][
                "api_key"
            ],
            "[REDACTED]",
        )
        self.assertEqual(decoded["response"][1][0]["safe"], "SYNTHETIC_DEEP_SAFE_TEXT")

        # Regression: serializing first and applying a scalar regex used to
        # redact only the first array item, corrupt JSON, and leak the second.
        raw = json.dumps(
            {"tokens": ["SYNTHETIC_ARRAY_SECRET_A", "SYNTHETIC_ARRAY_SECRET_B"]}
        )
        safe_raw = _redact_known_secrets(raw)
        self.assertNotIn("SYNTHETIC_ARRAY_SECRET_A", safe_raw)
        self.assertNotIn("SYNTHETIC_ARRAY_SECRET_B", safe_raw)
        self.assertEqual(json.loads(safe_raw), {"tokens": "[REDACTED]"})

        compound_raw = json.dumps(
            {
                "providerTokens": ["SYNTHETIC_RAW_PROVIDER"],
                "tokenList": {"value": "SYNTHETIC_RAW_LIST"},
                "inputTokens": 7,
            }
        )
        safe_compound_raw = _redact_known_secrets(compound_raw)
        self.assertNotIn("SYNTHETIC_RAW_PROVIDER", safe_compound_raw)
        self.assertNotIn("SYNTHETIC_RAW_LIST", safe_compound_raw)
        self.assertEqual(
            json.loads(safe_compound_raw),
            {
                "providerTokens": "[REDACTED]",
                "tokenList": "[REDACTED]",
                "inputTokens": 7,
            },
        )

        from mcp import grok_build_server as server

        content = server._content(
            {"tokens": ["SYNTHETIC_MCP_SECRET_A", "SYNTHETIC_MCP_SECRET_B"]}
        )
        public_payload = json.loads(content["content"][0]["text"])
        self.assertEqual(public_payload, {"tokens": "[REDACTED]"})

        camel_content = server._content(
            {
                "apiKeys": ["SYNTHETIC_CAMEL_API_A", "SYNTHETIC_CAMEL_API_B"],
                "nested": {
                    "accessToken": ["SYNTHETIC_CAMEL_ACCESS"],
                    "clientSecrets": {
                        "primary": "SYNTHETIC_CAMEL_CLIENT_SECRET"
                    },
                    "providerTokens": ["SYNTHETIC_PROVIDER_TOKEN"],
                    "serviceTokens": ["SYNTHETIC_SERVICE_TOKEN"],
                    "OAuthTokens": ["SYNTHETIC_OAUTH_TOKEN"],
                    "tokenList": ["SYNTHETIC_TOKEN_LIST"],
                    "secretValues": ["SYNTHETIC_SECRET_VALUE"],
                    "XAIAPIKey": ["SYNTHETIC_XAI_API_KEY"],
                    "APIKEY": ["SYNTHETIC_UPPER_API_KEY"],
                    "CLIENTSECRET_VALUE": ["SYNTHETIC_UPPER_CLIENT_SECRET"],
                },
                "usage": {
                    "inputTokens": 10,
                    "output_tokens": 5,
                    "providerTokens": 3,
                    "INPUTTOKENS": 8,
                },
            }
        )
        camel_payload = json.loads(camel_content["content"][0]["text"])
        self.assertEqual(camel_payload["apiKeys"], "[REDACTED]")
        self.assertEqual(camel_payload["nested"]["accessToken"], "[REDACTED]")
        self.assertEqual(camel_payload["nested"]["clientSecrets"], "[REDACTED]")
        for field in (
            "providerTokens",
            "serviceTokens",
            "OAuthTokens",
            "tokenList",
            "secretValues",
            "XAIAPIKey",
            "APIKEY",
            "CLIENTSECRET_VALUE",
        ):
            self.assertEqual(camel_payload["nested"][field], "[REDACTED]")
        self.assertEqual(
            camel_payload["usage"],
            {
                "inputTokens": 10,
                "output_tokens": 5,
                "providerTokens": 3,
                "INPUTTOKENS": 8,
            },
        )
        encoded_camel = json.dumps(camel_payload, sort_keys=True)
        for secret in (
            "SYNTHETIC_CAMEL_API_A",
            "SYNTHETIC_CAMEL_API_B",
            "SYNTHETIC_CAMEL_ACCESS",
            "SYNTHETIC_CAMEL_CLIENT_SECRET",
            "SYNTHETIC_PROVIDER_TOKEN",
            "SYNTHETIC_SERVICE_TOKEN",
            "SYNTHETIC_OAUTH_TOKEN",
            "SYNTHETIC_TOKEN_LIST",
            "SYNTHETIC_SECRET_VALUE",
            "SYNTHETIC_XAI_API_KEY",
            "SYNTHETIC_UPPER_API_KEY",
            "SYNTHETIC_UPPER_CLIENT_SECRET",
        ):
            self.assertNotIn(secret, encoded_camel)

    def test_truncated_query_value_is_still_redacted(self) -> None:
        redacted = _redact_known_secrets(
            "request failed at ?sc_token=SYNTHETIC_PARTIAL_SECR"
        )
        self.assertEqual(redacted, "request failed at ?sc_token=[REDACTED]")

    def test_redaction_covers_prefixed_headers_json_and_url_userinfo(self) -> None:
        value = (
            "[info] Authorization: Bearer SYNTHETIC_PREFIXED_SECRET\n"
            "12:00 Proxy-Authorization: Basic SYNTHETIC_PROXY_SECRET\n"
            "https://SYNTHETIC_PASSWORDLESS_SECRET@localhost/mcp\n"
            "ftp://user:SYNTHETIC_FTP_SECRET@127.0.0.1/x\n"
            '{"api_key":"SYNTHETIC_JSON_SECRET"}'
        )
        redacted = _redact_known_secrets(value)
        for secret in (
            "SYNTHETIC_PREFIXED_SECRET",
            "SYNTHETIC_PROXY_SECRET",
            "SYNTHETIC_PASSWORDLESS_SECRET",
            "SYNTHETIC_FTP_SECRET",
            "SYNTHETIC_JSON_SECRET",
        ):
            self.assertNotIn(secret, redacted)

    def test_setup_redacts_version_and_diagnostics_when_preflight_fails(self) -> None:
        fake_probe = {
            "ready": False,
            "binary": "/opt/grok",
            "version": (
                "grok synthetic "
                "https://localhost/version?access_token=SYNTHETIC_VERSION_SECRET"
            ),
            "model_diagnostics": (
                "Authorization: Bearer SYNTHETIC_DIAGNOSTIC_SECRET"
            ),
        }
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch("grok_build_bridge.probe_grok", return_value=fake_probe):
                setup = setup_grok(directory)
        encoded = json.dumps(setup, sort_keys=True)
        self.assertNotIn("SYNTHETIC_VERSION_SECRET", encoded)
        self.assertNotIn("SYNTHETIC_DIAGNOSTIC_SECRET", encoded)

    def test_setup_fails_closed_on_discovery_fallback_or_stderr_truncation(self) -> None:
        cases = (
            ("discovery-fallback-after-limit-grok", "E_MODEL_FALLBACK"),
            ("discovery-stderr-overflow-grok", "E_STDERR_LIMIT"),
        )
        for binary_name, expected_code in cases:
            with self.subTest(binary=binary_name):
                with tempfile.TemporaryDirectory() as directory:
                    base = Path(directory)
                    binary = base / binary_name
                    binary.write_bytes(FAKE_GROK.read_bytes())
                    binary.chmod(0o755)
                    os.environ["GROK_BUILD_BIN"] = str(binary)
                    target = base / "target"
                    target.mkdir()
                    setup = setup_grok(str(target))
                self.assertFalse(setup["ready"])
                self.assertFalse(setup["runtime_attested"])
                self.assertEqual(setup["runtime_error"]["code"], expected_code)

    def test_setup_deadline_terminates_a_slow_probe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            binary = base / "slow-probe-grok"
            binary.write_bytes(FAKE_GROK.read_bytes())
            binary.chmod(0o755)
            os.environ["GROK_BUILD_BIN"] = str(binary)
            target = base / "target"
            target.mkdir()
            started = time.monotonic()
            with self.assertRaises(BridgeError) as caught:
                setup_grok(str(target), timeout_seconds=10)
            self.assertEqual(caught.exception.code, "E_PROBE_TIMEOUT")
            self.assertLess(time.monotonic() - started, 13)

    def test_setup_deadline_covers_runtime_attestation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            binary = base / "slow-runtime-attestation-grok"
            binary.write_bytes(FAKE_GROK.read_bytes())
            binary.chmod(0o755)
            os.environ["GROK_BUILD_BIN"] = str(binary)
            target = base / "target"
            target.mkdir()
            started = time.monotonic()
            setup = setup_grok(str(target), timeout_seconds=10)
            self.assertFalse(setup["ready"])
            self.assertFalse(setup["runtime_attested"])
            self.assertEqual(setup["runtime_error"]["code"], "E_PROBE_TIMEOUT")
            self.assertLess(time.monotonic() - started, 13)

    def test_success_receipt_recursively_redacts_acp_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = JobManager()
            try:
                spawned = manager.spawn(
                    mode="review", task="SENSITIVE_OUTPUT", cwd=directory
                )
                status = wait_terminal(manager, spawned["job_id"])
                self.assertEqual(status["status"], "succeeded", status)
                result = manager.result(spawned["job_id"])
            finally:
                manager.close()
        encoded = json.dumps(result, sort_keys=True)
        for secret in (
            "SYNTHETIC_QUERY_SECRET",
            "SYNTHETIC_BEARER_SECRET",
            "SYNTHETIC_USERINFO_SECRET",
            "SYNTHETIC_COOKIE_SECRET",
            "SYNTHETIC_PROXY_SECRET",
            "SYNTHETIC_PASSWORDLESS_SECRET",
            "SYNTHETIC_FTP_SECRET",
            "SYNTHETIC_JSON_SECRET",
            "SYNTHETIC_STOP_SECRET",
            "SYNTHETIC_USAGE_SECRET",
            SYNTHETIC_EMAIL,
        ):
            self.assertNotIn(secret, encoded)
        self.assertEqual(result["usage"]["input_tokens"], 10)

    def test_failed_status_redacts_remote_error(self) -> None:
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

    def test_known_local_paths_are_redacted_from_public_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory) / "linked"
            primary = Path(directory) / "primary"
            text = f"target={cwd}/src primary={primary}/README.md"
            redacted = _redact_known_secrets(text, cwd, [str(primary)])
        self.assertNotIn(directory, redacted)
        self.assertIn("target=./src", redacted)
        self.assertIn("primary=[LOCAL_PATH]/README.md", redacted)

    def test_worker_refuses_primary_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_git(root, "init")
            manager = JobManager()
            try:
                spawned = manager.spawn(
                    mode="implement", task="change it", cwd=str(root)
                )
                status = wait_terminal(manager, spawned["job_id"])
                self.assertEqual(status["status"], "failed")
                self.assertEqual(status["errors"][0]["code"], "E_PRIMARY_CHECKOUT")
            finally:
                manager.close()

    def test_worktree_list_is_part_of_snapshot_identity(self) -> None:
        before = {
            "status_sha256": "status",
            "diff_sha256": "diff",
            "head_oid": "head",
            "head_ref": "refs/heads/test",
            "worktrees_sha256": "before",
        }
        after = dict(before, worktrees_sha256="after")
        self.assertFalse(_same_snapshot(before, after))

    def test_public_git_snapshot_hides_local_paths_and_branch_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = str((Path(directory) / "repository").resolve())
            snapshot = {
                "root": root,
                "head_ref": "refs/heads/example-feature",
                "worktrees": [root, f"{root}-linked"],
                "primary_worktree": root,
                "worktrees_sha256": "worktree-hash",
                "changed_files": ["src/example.py"],
                "_records": {"example": "internal"},
            }
            public = _public_git_snapshot(snapshot)
            self.assertIsNotNone(public)
            encoded = json.dumps(public, sort_keys=True)
            self.assertNotIn(directory, encoded)
            self.assertNotIn("example-feature", encoded)
            self.assertNotIn("_records", encoded)
            self.assertEqual(public["root"], ".")
            self.assertEqual(public["worktree_count"], 2)
            self.assertTrue(public["primary_checkout_present"])
            self.assertIn("head_ref_sha256", public)

    def test_worker_refuses_dirty_linked_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            primary = base / "primary"
            linked = base / "linked"
            primary.mkdir()
            run_git(primary, "init")
            run_git(primary, "config", "user.email", "test.invalid")
            run_git(primary, "config", "user.name", "Test")
            (primary / "seed.txt").write_text("seed\n", encoding="utf-8")
            run_git(primary, "add", "seed.txt")
            run_git(primary, "commit", "-m", "seed")
            run_git(primary, "worktree", "add", "-b", "grok-dirty", str(linked))
            (linked / "dirty.txt").write_text("dirty\n", encoding="utf-8")
            manager = JobManager()
            try:
                spawned = manager.spawn(
                    mode="implement", task="change it", cwd=str(linked)
                )
                status = wait_terminal(manager, spawned["job_id"])
                self.assertEqual(status["status"], "failed")
                self.assertEqual(status["errors"][0]["code"], "E_DIRTY_WORKTREE")
            finally:
                manager.close()

    def test_worker_changes_only_clean_linked_worktree_and_requires_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            primary = base / "primary"
            linked = base / "linked"
            primary.mkdir()
            run_git(primary, "init")
            run_git(primary, "config", "user.email", "test.invalid")
            run_git(primary, "config", "user.name", "Test")
            (primary / "seed.txt").write_text("seed\n", encoding="utf-8")
            run_git(primary, "add", "seed.txt")
            run_git(primary, "commit", "-m", "seed")
            run_git(primary, "worktree", "add", "-b", "grok-test", str(linked))
            primary_status_before = run_git(primary, "status", "--porcelain")
            manager = JobManager()
            try:
                spawned = manager.spawn(
                    mode="implement", task="CREATE_FILE", cwd=str(linked)
                )
                status = wait_terminal(manager, spawned["job_id"])
                self.assertEqual(status["status"], "succeeded", status)
                result = manager.result(spawned["job_id"])
                self.assertIn("created_by_grok.txt", result["git"]["changed_files"])
                self.assertTrue(result["git"]["primary_checkout_unchanged"])
                self.assertTrue(result["verification"]["review_required"])
                self.assertEqual(result["verification"]["codex_review"], "pending")
                self.assertEqual(run_git(primary, "status", "--porcelain"), primary_status_before)
            finally:
                manager.close()

    def test_worker_correction_chain_is_state_bound_and_capped_at_two(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            primary = base / "primary"
            linked = base / "linked"
            primary.mkdir()
            run_git(primary, "init")
            run_git(primary, "config", "user.email", "test.invalid")
            run_git(primary, "config", "user.name", "Test")
            (primary / "seed.txt").write_text("seed\n", encoding="utf-8")
            run_git(primary, "add", "seed.txt")
            run_git(primary, "commit", "-m", "seed")
            run_git(primary, "worktree", "add", "-b", "grok-corrections", str(linked))
            manager = JobManager()
            try:
                initial = manager.spawn(
                    mode="implement", task="CREATE_FILE", cwd=str(linked)
                )
                self.assertEqual(
                    wait_terminal(manager, initial["job_id"])["status"], "succeeded"
                )
                first = manager.spawn(
                    mode="implement",
                    task="bounded correction one",
                    cwd=str(linked),
                    correction_of_job_id=initial["job_id"],
                )
                first_status = wait_terminal(manager, first["job_id"])
                self.assertEqual(first_status["status"], "succeeded")
                self.assertEqual(first_status["correction_chain"]["round"], 1)
                with self.assertRaises(BridgeError) as reused:
                    manager.spawn(
                        mode="implement",
                        task="forbidden branch",
                        cwd=str(linked),
                        correction_of_job_id=initial["job_id"],
                    )
                self.assertEqual(reused.exception.code, "E_CORRECTION_ALREADY_USED")

                second = manager.spawn(
                    mode="implement",
                    task="bounded correction two",
                    cwd=str(linked),
                    correction_of_job_id=first["job_id"],
                )
                second_status = wait_terminal(manager, second["job_id"])
                self.assertEqual(second_status["status"], "succeeded")
                self.assertEqual(second_status["correction_chain"]["round"], 2)
                with self.assertRaises(BridgeError) as limited:
                    manager.spawn(
                        mode="implement",
                        task="forbidden third correction",
                        cwd=str(linked),
                        correction_of_job_id=second["job_id"],
                    )
                self.assertEqual(limited.exception.code, "E_CORRECTION_LIMIT")
            finally:
                manager.close()

    def test_worker_correction_rejects_intervening_worktree_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            primary = base / "primary"
            linked = base / "linked"
            primary.mkdir()
            run_git(primary, "init")
            run_git(primary, "config", "user.email", "test.invalid")
            run_git(primary, "config", "user.name", "Test")
            (primary / "seed.txt").write_text("seed\n", encoding="utf-8")
            run_git(primary, "add", "seed.txt")
            run_git(primary, "commit", "-m", "seed")
            run_git(primary, "worktree", "add", "-b", "grok-state-bound", str(linked))
            manager = JobManager()
            try:
                initial = manager.spawn(
                    mode="implement", task="CREATE_FILE", cwd=str(linked)
                )
                self.assertEqual(
                    wait_terminal(manager, initial["job_id"])["status"], "succeeded"
                )
                (linked / "created_by_grok.txt").write_text(
                    "intervening edit\n", encoding="utf-8"
                )
                changed = manager.spawn(
                    mode="implement",
                    task="must not mix changes",
                    cwd=str(linked),
                    correction_of_job_id=initial["job_id"],
                )
                changed_status = wait_terminal(manager, changed["job_id"])
                self.assertEqual(changed_status["status"], "failed")
                self.assertEqual(
                    changed_status["errors"][0]["code"], "E_CORRECTION_STATE"
                )
            finally:
                manager.close()

    def test_worker_commit_is_detected_and_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            primary = base / "primary"
            linked = base / "linked"
            primary.mkdir()
            run_git(primary, "init")
            run_git(primary, "config", "user.email", "test.invalid")
            run_git(primary, "config", "user.name", "Test")
            (primary / "seed.txt").write_text("seed\n", encoding="utf-8")
            run_git(primary, "add", "seed.txt")
            run_git(primary, "commit", "-m", "seed")
            run_git(primary, "worktree", "add", "-b", "grok-commit", str(linked))
            manager = JobManager()
            try:
                spawned = manager.spawn(
                    mode="implement", task="COMMIT_FILE", cwd=str(linked)
                )
                status = wait_terminal(manager, spawned["job_id"])
                self.assertEqual(status["status"], "failed")
                self.assertEqual(status["errors"][0]["code"], "E_COMMIT_DETECTED")
            finally:
                manager.close()

    def test_worker_receipt_captures_ignored_content_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            primary = base / "primary"
            linked = base / "linked"
            primary.mkdir()
            run_git(primary, "init")
            run_git(primary, "config", "user.email", "test.invalid")
            run_git(primary, "config", "user.name", "Test")
            (primary / ".gitignore").write_text("*.tmp\n", encoding="utf-8")
            (primary / "seed.txt").write_text("seed\n", encoding="utf-8")
            run_git(primary, "add", ".gitignore", "seed.txt")
            run_git(primary, "commit", "-m", "seed")
            run_git(primary, "worktree", "add", "-b", "grok-ignored", str(linked))
            (linked / "cache.tmp").write_text("before\n", encoding="utf-8")
            manager = JobManager()
            try:
                spawned = manager.spawn(
                    mode="implement", task="MODIFY_IGNORED", cwd=str(linked)
                )
                status = wait_terminal(manager, spawned["job_id"])
                self.assertEqual(status["status"], "succeeded")
                result = manager.result(spawned["job_id"])
                self.assertFalse(result["git"]["ignored_unchanged"])
                self.assertEqual(result["git"]["ignored_changed_files"], ["cache.tmp"])
                self.assertNotIn("_ignored_records", result["git"]["before"])
            finally:
                manager.close()

    def test_worker_primary_checkout_change_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            primary = base / "primary"
            linked = base / "linked"
            primary.mkdir()
            run_git(primary, "init")
            run_git(primary, "config", "user.email", "test.invalid")
            run_git(primary, "config", "user.name", "Test")
            (primary / "seed.txt").write_text("seed\n", encoding="utf-8")
            run_git(primary, "add", "seed.txt")
            run_git(primary, "commit", "-m", "seed")
            run_git(primary, "worktree", "add", "-b", "grok-primary", str(linked))
            manager = JobManager()
            try:
                spawned = manager.spawn(
                    mode="implement", task="CHANGE_PRIMARY", cwd=str(linked)
                )
                status = wait_terminal(manager, spawned["job_id"])
                self.assertEqual(status["status"], "failed")
                self.assertEqual(status["errors"][0]["code"], "E_PRIMARY_CHANGED")
            finally:
                manager.close()

    @unittest.skipUnless(hasattr(os, "killpg"), "process-group cleanup requires POSIX")
    def test_terminate_owned_process_kills_group_after_parent_exits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "child-heartbeat.txt"
            child_code = (
                "import pathlib,time\n"
                f"marker=pathlib.Path({str(marker)!r})\n"
                "while True:\n"
                "    marker.write_text(str(time.monotonic_ns()), encoding='utf-8')\n"
                "    time.sleep(0.02)\n"
            )
            parent_code = (
                "import pathlib,subprocess,sys,time\n"
                f"marker=pathlib.Path({str(marker)!r})\n"
                f"child=subprocess.Popen([sys.executable,'-c',{child_code!r}])\n"
                "deadline=time.monotonic()+5\n"
                "while not marker.exists() and time.monotonic()<deadline:\n"
                "    time.sleep(0.01)\n"
                "print(child.pid, flush=True)\n"
            )
            proc = subprocess.Popen(
                [sys.executable, "-c", parent_code],
                cwd=directory,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            _mark_owned_process_group(proc)
            child_pid: int | None = None
            try:
                assert proc.stdout is not None
                line = proc.stdout.readline().strip()
                self.assertTrue(line, "parent did not report its child PID")
                child_pid = int(line)
                proc.wait(timeout=3)
                self.assertIsNotNone(proc.returncode)
                before_cleanup = marker.read_text(encoding="utf-8")

                terminate_owned_process(proc, grace_seconds=0.1)

                time.sleep(0.3)
                self.assertEqual(
                    marker.read_text(encoding="utf-8"),
                    before_cleanup,
                    "a child left in the exited parent's process group remained active",
                )
                # On POSIX, ps exposes a short-lived zombie as Z; either that
                # state or no row is acceptable after the process-group kill.
                try:
                    process_state = subprocess.run(
                        ["ps", "-o", "stat=", "-p", str(child_pid)],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                        text=True,
                        check=False,
                    ).stdout.strip()
                except OSError:
                    process_state = ""
                if process_state:
                    self.assertTrue(
                        process_state.startswith("Z"),
                        f"child process remained runnable after cleanup: {process_state!r}",
                    )
            finally:
                # A failing assertion must not leave a process group behind;
                # this fallback also makes the regression test itself bounded
                # against the pre-fix early-return behavior.
                if proc.poll() is None:
                    terminate_owned_process(proc, grace_seconds=0.1)
                else:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except (ProcessLookupError, OSError):
                        pass
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
                if proc.stdout is not None:
                    proc.stdout.close()
                if proc.stderr is not None:
                    proc.stderr.close()

    def test_cancel_targets_owned_job(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = JobManager()
            try:
                spawned = manager.spawn(
                    mode="research", task="FAKE_SLEEP", cwd=directory, timeout_seconds=60
                )
                deadline = time.monotonic() + 5
                while manager.status(spawned["job_id"])["status"] == "queued":
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.02)
                manager.cancel(spawned["job_id"])
                status = wait_terminal(manager, spawned["job_id"])
                self.assertEqual(status["status"], "cancelled")
            finally:
                manager.close()

    def test_cancel_interrupts_an_active_probe_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            slow = base / "slow-probe-grok"
            slow.write_bytes(FAKE_GROK.read_bytes())
            slow.chmod(0o755)
            os.environ["GROK_BUILD_BIN"] = str(slow)
            target = base / "target"
            target.mkdir()
            manager = JobManager()
            try:
                spawned = manager.spawn(
                    mode="research", task="inspect", cwd=str(target), timeout_seconds=60
                )
                deadline = time.monotonic() + 5
                while manager.get(spawned["job_id"]).process is None:
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.02)
                manager.cancel(spawned["job_id"])
                status = wait_terminal(manager, spawned["job_id"], timeout=5)
                self.assertEqual(status["status"], "cancelled")
                self.assertIsNone(manager.get(spawned["job_id"]).process)
            finally:
                manager.close()

    def test_cancel_interrupts_preflight_snapshot_loop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            entered = threading.Event()

            def cancellable_snapshot(*args: object, **kwargs: object) -> dict:
                cancel_event = kwargs.get("cancel_event")
                deadline = kwargs.get("deadline")
                self.assertIsInstance(cancel_event, threading.Event)
                self.assertIsInstance(deadline, float)
                entered.set()
                assert isinstance(cancel_event, threading.Event)
                while not cancel_event.is_set():
                    time.sleep(0.01)
                raise BridgeError("E_SYNTHETIC_CANCEL", "synthetic cancelled scan")

            manager = JobManager()
            try:
                with mock.patch(
                    "grok_build_bridge._snapshot_tree_fd",
                    side_effect=cancellable_snapshot,
                ):
                    spawned = manager.spawn(
                        mode="plan", task="inspect", cwd=directory
                    )
                    self.assertTrue(entered.wait(5), "snapshot did not start")
                    manager.cancel(spawned["job_id"])
                    status = wait_terminal(manager, spawned["job_id"], timeout=5)
                self.assertEqual(status["status"], "cancelled")
                self.assertEqual(status["errors"][0]["code"], "E_CANCELLED")
            finally:
                manager.close()

    def test_job_deadline_includes_probe_time(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            slow = base / "slow-probe-grok"
            slow.write_bytes(FAKE_GROK.read_bytes())
            slow.chmod(0o755)
            os.environ["GROK_BUILD_BIN"] = str(slow)
            target = base / "target"
            target.mkdir()
            manager = JobManager()
            try:
                started = time.monotonic()
                spawned = manager.spawn(
                    mode="research", task="inspect", cwd=str(target), timeout_seconds=10
                )
                status = wait_terminal(manager, spawned["job_id"], timeout=15)
                self.assertEqual(status["status"], "timed_out")
                self.assertEqual(status["errors"][0]["code"], "E_TIMEOUT")
                self.assertLess(time.monotonic() - started, 13)
                self.assertIsNone(manager.get(spawned["job_id"]).process)
            finally:
                manager.close()

    def test_timeout_marks_job_and_releases_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = JobManager()
            try:
                spawned = manager.spawn(
                    mode="research",
                    task="FAKE_SLEEP",
                    cwd=directory,
                    timeout_seconds=10,
                )
                status = wait_terminal(manager, spawned["job_id"], timeout=15)
                self.assertEqual(status["status"], "timed_out")
                self.assertEqual(status["errors"][0]["code"], "E_TIMEOUT")
                self.assertIsNone(manager.get(spawned["job_id"]).process)
            finally:
                manager.close()

    def test_acp_blocked_stdin_honors_deadline_and_cancel_without_process_leak(self) -> None:
        acp_script = (
            "import json,pathlib,sys,time\n"
            "marker=pathlib.Path(sys.argv[1])\n"
            "def emit(message):\n"
            "    sys.stdout.write(json.dumps(message,separators=(',',':'))+'\\n')\n"
            "    sys.stdout.flush()\n"
            "for line in sys.stdin:\n"
            "    if not line.strip():\n"
            "        continue\n"
            "    request=json.loads(line)\n"
            "    method=request.get('method')\n"
            "    if method == 'initialize':\n"
            "        emit({'jsonrpc':'2.0','id':request.get('id'),'result':{"
            "'authMethods':[{'id':'cached_token'}], '_meta':{'modelState':{"
            "'currentModelId':'synthetic-model','availableModels':[{'modelId':"
            "'synthetic-model','_meta':{'reasoningEffort':'xhigh',"
            "'reasoningEfforts':[{'id':'xhigh'}]}}]}}}})\n"
            "    elif method == 'authenticate':\n"
            "        emit({'jsonrpc':'2.0','id':request.get('id'),'result':{}})\n"
            "    elif method == 'session/new':\n"
            "        emit({'jsonrpc':'2.0','id':request.get('id'),'result':"
            "{'sessionId':'synthetic-session'}})\n"
            "        marker.write_text('ready',encoding='utf-8')\n"
            "        time.sleep(60)\n"
            "        break\n"
        )
        # This payload is larger than a normal POSIX pipe buffer. The child
        # deliberately stops reading after session/new, so request() must not
        # block forever in BufferedWriter.write().
        large_prompt = "x" * 2_000_000
        scenarios = (
            ("deadline", JobTimedOut, False),
            ("cancel", JobCancelled, True),
        )
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            for name, expected_error, cancel in scenarios:
                with self.subTest(scenario=name):
                    marker = base / f"{name}-ready"
                    script = base / f"{name}-blocked-acp.py"
                    script.write_text(acp_script, encoding="utf-8")
                    cancel_event = threading.Event()
                    holder: dict[str, subprocess.Popen[bytes]] = {}
                    client = ACPClient(
                        [sys.executable, str(script), str(marker)],
                        Path(directory),
                        None,
                        cancel_event,
                        1_000,
                        lambda proc: holder.__setitem__("proc", proc),
                    )
                    outcome: dict[str, BaseException | dict] = {}

                    def invoke() -> None:
                        try:
                            outcome["result"] = client.run(
                                large_prompt,
                                time.monotonic() + (2.0 if not cancel else 10.0),
                                10,
                                expected_model="synthetic-model",
                                expected_reasoning_effort="xhigh",
                            )
                        except BaseException as exc:  # captured for the assertion below
                            outcome["error"] = exc

                    worker = threading.Thread(target=invoke, daemon=True)
                    worker.start()
                    try:
                        ready_deadline = time.monotonic() + 5
                        while not marker.exists():
                            self.assertLess(
                                time.monotonic(),
                                ready_deadline,
                                "ACP handshake did not reach the blocked prompt",
                            )
                            time.sleep(0.01)
                        self.assertTrue(
                            worker.is_alive(),
                            "the blocked-stdin request completed before cancellation/deadline",
                        )
                        if cancel:
                            cancel_event.set()
                        worker.join(timeout=4)
                        self.assertFalse(
                            worker.is_alive(),
                            "ACP request remained blocked after cancellation/deadline",
                        )
                        self.assertIsInstance(outcome.get("error"), expected_error)
                        self.assertEqual(
                            client._pending,
                            {},
                            "failed ACP writes must remove their pending waiter",
                        )
                        owned = holder.get("proc")
                        self.assertIsNotNone(owned)
                        assert owned is not None
                        self.assertIsNotNone(
                            owned.poll(), "ACP process leaked after a blocked stdin request"
                        )
                    finally:
                        if worker.is_alive():
                            terminate_owned_process(holder.get("proc"), grace_seconds=0.1)
                            worker.join(timeout=2)
                        client._close_streams()


class MCPProtocolTests(unittest.TestCase):
    def test_spawn_readonly_rejects_implement_mode_before_manager(self) -> None:
        from mcp import grok_build_server as server

        with mock.patch.object(server.MANAGER, "spawn") as spawn:
            response = server.call_tool(
                "spawn_readonly",
                {
                    "mode": "implement",
                    "task": "must remain read-only",
                    "cwd": str(PLUGIN_ROOT),
                },
            )
        self.assertTrue(response["isError"])
        payload = json.loads(response["content"][0]["text"])
        self.assertEqual(payload["error"]["code"], "E_MODE")
        spawn.assert_not_called()

    def test_spawn_worker_passes_correction_parent_to_manager(self) -> None:
        from mcp import grok_build_server as server

        parent_id = "11111111-1111-4111-8111-111111111111"
        with mock.patch.object(
            server.MANAGER,
            "spawn",
            return_value={"job_id": "22222222-2222-4222-8222-222222222222"},
        ) as spawn:
            response = server.call_tool(
                "spawn_worker",
                {
                    "task": "bounded correction",
                    "cwd": str(PLUGIN_ROOT),
                    "correction_of_job_id": parent_id,
                },
            )
        self.assertNotIn("isError", response)
        self.assertEqual(spawn.call_args.kwargs["correction_of_job_id"], parent_id)

    def test_initialize_and_tool_list(self) -> None:
        env = dict(os.environ)
        env["GROK_BUILD_BIN"] = str(FAKE_GROK)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        proc = subprocess.Popen(
            [sys.executable, str(PLUGIN_ROOT / "mcp" / "grok_build_server.py")],
            cwd=str(PLUGIN_ROOT),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert proc.stdin is not None and proc.stdout is not None
        try:
            requests = [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18"},
                },
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "setup",
                        "arguments": {"cwd": str(PLUGIN_ROOT)},
                    },
                },
            ]
            for request in requests:
                proc.stdin.write(json.dumps(request) + "\n")
                proc.stdin.flush()
            initialize = json.loads(proc.stdout.readline())
            tools = json.loads(proc.stdout.readline())
            setup = json.loads(proc.stdout.readline())
            self.assertEqual(initialize["result"]["serverInfo"]["name"], "call-grok-build")
            manifest = json.loads(
                (PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                initialize["result"]["serverInfo"]["version"],
                manifest["version"],
            )
            self.assertEqual(initialize["result"]["protocolVersion"], "2025-06-18")
            names = {tool["name"] for tool in tools["result"]["tools"]}
            self.assertEqual(
                names,
                {"setup", "spawn_readonly", "spawn_worker", "status", "result", "list", "cancel"},
            )
            setup_payload = json.loads(setup["result"]["content"][0]["text"])
            self.assertTrue(setup_payload["ready"])
            self.assertTrue(setup_payload["runtime_attested"])
            self.assertEqual(setup_payload["cwd"], ".")
            self.assertEqual(setup_payload["catalog_cwd"], ".")
            self.assertEqual(
                setup_payload["binary"], Path(setup_payload["binary"]).name
            )
            self.assertEqual(setup_payload["selected_model"], "grok-9.2")
            self.assertEqual(setup_payload["selected_reasoning_effort"], "xhigh")
        finally:
            proc.stdin.close()
            proc.wait(timeout=5)
            if proc.returncode != 0:
                self.fail(proc.stderr.read())
            proc.stdout.close()
            proc.stderr.close()

    def test_invalid_params_returns_error_and_server_stays_alive(self) -> None:
        env = dict(os.environ)
        env["GROK_BUILD_BIN"] = str(FAKE_GROK)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        proc = subprocess.Popen(
            [sys.executable, str(PLUGIN_ROOT / "mcp" / "grok_build_server.py")],
            cwd=str(PLUGIN_ROOT),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert proc.stdin is not None and proc.stdout is not None
        try:
            proc.stdin.write(
                json.dumps(
                    {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": [1]}
                )
                + "\n"
            )
            proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}) + "\n")
            proc.stdin.flush()
            invalid = json.loads(proc.stdout.readline())
            ping = json.loads(proc.stdout.readline())
            self.assertEqual(invalid["error"]["code"], -32602)
            self.assertEqual(ping["result"], {})
        finally:
            proc.stdin.close()
            proc.wait(timeout=5)
            if proc.returncode != 0:
                self.fail(proc.stderr.read())
            proc.stdout.close()
            proc.stderr.close()

    def test_mcp_boundary_redacts_unknown_method_text(self) -> None:
        env = dict(os.environ)
        env["GROK_BUILD_BIN"] = str(FAKE_GROK)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        proc = subprocess.Popen(
            [sys.executable, str(PLUGIN_ROOT / "mcp" / "grok_build_server.py")],
            cwd=str(PLUGIN_ROOT),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert proc.stdin is not None and proc.stdout is not None
        try:
            proc.stdin.write(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": (
                            "unknown?sc_token=SYNTHETIC_MCP_BOUNDARY_SECRET"
                        ),
                    }
                )
                + "\n"
            )
            proc.stdin.flush()
            response = json.loads(proc.stdout.readline())
            encoded = json.dumps(response, sort_keys=True)
            self.assertNotIn("SYNTHETIC_MCP_BOUNDARY_SECRET", encoded)
            self.assertIn("[REDACTED]", encoded)
        finally:
            proc.stdin.close()
            proc.wait(timeout=5)
            if proc.returncode != 0:
                self.fail(proc.stderr.read())
            proc.stdout.close()
            proc.stderr.close()


if __name__ == "__main__":
    unittest.main()
