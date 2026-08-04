from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "monitor.yml"
CHECKPOINT = ROOT / "scripts" / "checkpoint_state.sh"
GIT = shutil.which("git")
BASH = shutil.which("bash")
if BASH is None:
    candidate = Path.home() / "AppData" / "Local" / "Programs" / "Git" / "bin" / "bash.exe"
    if candidate.exists():
        BASH = str(candidate)


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=check,
        text=True,
        capture_output=True,
    )


class WorkflowStructureTests(unittest.TestCase):
    def test_workflow_serializes_current_main_and_checkpoints_before_delivery(self) -> None:
        data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        self.assertFalse(data["concurrency"]["cancel-in-progress"])
        job = data["jobs"]["check"]
        self.assertEqual(job["timeout-minutes"], 4)
        self.assertIn("MONITOR_JOB_STARTED", job["steps"][0]["run"])
        checkout = next(step for step in job["steps"] if "uses" in step and "actions/checkout" in step["uses"])
        self.assertEqual(checkout["with"]["ref"], "main")
        self.assertEqual(checkout["with"]["fetch-depth"], 1)
        workflow_text = WORKFLOW.read_text(encoding="utf-8")
        budget = workflow_text.index("MONITOR_JOB_STARTED=$(date +%s)")
        checkout_position = workflow_text.index("actions/checkout")
        producer = workflow_text.index("MONITOR_PRODUCER_REVISION=$(git rev-parse HEAD)")
        setup = workflow_text.index("actions/setup-python")
        probe = workflow_text.index("python -m src.main --probe")
        checkpoint = workflow_text.index("checkpoint_state.sh probe")
        delivery = workflow_text.index("python -m src.main --deliver-next")
        self.assertLess(budget, checkout_position)
        self.assertLess(checkout_position, producer)
        self.assertLess(producer, setup)
        self.assertLess(probe, checkpoint)
        self.assertLess(checkpoint, delivery)
        self.assertIn("$(date +%s) - MONITOR_JOB_STARTED", workflow_text)
        self.assertIn("MONITOR_PRODUCER_REVISION=$(git rev-parse HEAD)", workflow_text)
        self.assertIn('remaining=$(( delivery_deadline - elapsed ))', workflow_text)
        self.assertIn('max_wait=$(( remaining - finalization_reserve ))', workflow_text)
        self.assertIn('--max-wait-seconds "$max_wait"', workflow_text)
        self.assertIn(
            'probe_budget=$(( delivery_deadline - elapsed - finalization_reserve ))',
            workflow_text,
        )
        self.assertIn(
            'timeout --signal=TERM --kill-after=5 "${probe_budget}s" python -m src.main --probe',
            workflow_text,
        )
        self.assertNotIn("--max-wait-seconds 35", workflow_text)
        self.assertIn('CHECKPOINT_MAX_ATTEMPTS: "2"', workflow_text)
        self.assertIn('CHECKPOINT_GIT_TIMEOUT_SECONDS: "8"', workflow_text)
        self.assertIn("delivery_deadline=210", workflow_text)
        self.assertIn("finalization_reserve=50", workflow_text)
        self.assertIn("MONITOR_WRITER_BASE=$(git rev-parse HEAD)", workflow_text)
        checkpoint_text = CHECKPOINT.read_text(encoding="utf-8")
        self.assertIn('remote_head" != "$writer_base', checkpoint_text)
        self.assertNotIn("git rebase", checkpoint_text)
        self.assertIn("timeout --signal=TERM", checkpoint_text)


@unittest.skipUnless(GIT and BASH, "git/bash required")
class GitCheckpointIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        # Keep nested Git repositories out of the checkout. Git-for-Windows
        # helper processes can otherwise leave sandbox-owned ACL remnants that
        # pollute `git status` even after a successful test run.
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.remote = self.base / "remote.git"
        subprocess.run(
            ["git", "init", "--bare", "--initial-branch=main", str(self.remote)],
            check=True,
            text=True,
            capture_output=True,
        )
        seed = self.clone("seed")
        (seed / "state").mkdir()
        (seed / "state" / "state.json").write_text('{"value":"seed"}\n', encoding="utf-8")
        (seed / "code.txt").write_text("seed\n", encoding="utf-8")
        git(seed, "add", ".")
        git(seed, "commit", "-m", "seed")
        git(seed, "push", "origin", "HEAD:main")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def clone(self, name: str, *, shallow: bool = False) -> Path:
        path = self.base / name
        command = ["git", "clone"]
        if shallow:
            command.extend(["--depth", "1"])
        remote = self.remote.as_uri() if shallow else str(self.remote)
        command.extend([remote, str(path)])
        subprocess.run(
            command,
            check=True,
            text=True,
            capture_output=True,
        )
        git(path, "config", "user.name", "test")
        git(path, "config", "user.email", "test@example.com")
        return path

    def checkpoint(
        self, repo: Path, *, git_wrapper: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env.update(
            CHECKPOINT_BACKOFF_SECONDS="0",
            CHECKPOINT_GIT_TIMEOUT_SECONDS="5",
        )
        if git_wrapper is not None:
            env["CHECKPOINT_GIT_COMMAND"] = (git_wrapper / "git").as_posix()
        return subprocess.run(
            [BASH or "bash", CHECKPOINT.as_posix(), "test"],
            cwd=repo,
            check=False,
            text=True,
            capture_output=True,
            env=env,
        )

    def test_happy_path_and_second_checkpoint_advance_writer_basis(self) -> None:
        writer = self.clone("writer-happy")
        (writer / "state" / "state.json").write_text('{"value":"one"}\n', encoding="utf-8")
        first = self.checkpoint(writer)
        self.assertEqual(first.returncode, 0, first.stderr)

        (writer / "state" / "state.json").write_text('{"value":"two"}\n', encoding="utf-8")
        second = self.checkpoint(writer)
        self.assertEqual(second.returncode, 0, second.stderr)
        verify = self.clone("verify-happy")
        self.assertEqual(json.loads((verify / "state" / "state.json").read_text())["value"], "two")

    def test_shallow_writer_can_checkpoint_and_advance_basis(self) -> None:
        writer = self.clone("writer-shallow", shallow=True)
        self.assertEqual(git(writer, "rev-list", "--count", "HEAD").stdout.strip(), "1")
        (writer / "state" / "state.json").write_text('{"value":"one"}\n', encoding="utf-8")
        first = self.checkpoint(writer)
        self.assertEqual(first.returncode, 0, first.stderr)

        (writer / "state" / "state.json").write_text('{"value":"two"}\n', encoding="utf-8")
        second = self.checkpoint(writer)
        self.assertEqual(second.returncode, 0, second.stderr)
        verify = self.clone("verify-shallow")
        self.assertEqual(json.loads((verify / "state" / "state.json").read_text())["value"], "two")

    def test_server_accepted_push_despite_local_failure_is_recognized(self) -> None:
        writer = self.clone("writer-uncertain")
        (writer / "state" / "state.json").write_text(
            '{"value":"accepted"}\n', encoding="utf-8"
        )
        wrapper_dir = self.base / "git-wrapper"
        wrapper_dir.mkdir()
        marker = (self.base / "push-was-forwarded").as_posix()
        real_git = Path(GIT or "git").as_posix()
        wrapper = wrapper_dir / "git"
        wrapper.write_text(
            "#!/usr/bin/env bash\n"
            "if [ \"${1:-}\" = push ] && [ ! -f \"$WIESN_PUSH_MARKER\" ]; then\n"
            "  touch \"$WIESN_PUSH_MARKER\"\n"
            "  \"$WIESN_REAL_GIT\" \"$@\" || exit $?\n"
            "  exit 1\n"
            "fi\n"
            "exec \"$WIESN_REAL_GIT\" \"$@\"\n",
            encoding="utf-8",
            newline="\n",
        )
        wrapper.chmod(0o755)

        with patch.dict(
            os.environ,
            {"WIESN_PUSH_MARKER": marker, "WIESN_REAL_GIT": real_git},
        ):
            result = self.checkpoint(writer, git_wrapper=wrapper_dir)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("already present after uncertain push", result.stdout)
        verify = self.clone("verify-uncertain")
        self.assertEqual(
            json.loads((verify / "state" / "state.json").read_text())["value"],
            "accepted",
        )

    def test_code_only_push_race_fails_closed(self) -> None:
        writer = self.clone("writer")
        code_writer = self.clone("code-writer")
        (writer / "state" / "state.json").write_text('{"value":"writer"}\n', encoding="utf-8")
        (code_writer / "code.txt").write_text("new code\n", encoding="utf-8")
        git(code_writer, "add", "code.txt")
        git(code_writer, "commit", "-m", "code")
        git(code_writer, "push", "origin", "HEAD:main")

        result = self.checkpoint(writer)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("remote main changed since writer basis", result.stderr)
        verify = self.clone("verify-code-race")
        self.assertEqual(
            json.loads((verify / "state" / "state.json").read_text())["value"],
            "seed",
        )
        self.assertEqual((verify / "code.txt").read_text(encoding="utf-8"), "new code\n")

    def test_state_push_race_fails_closed(self) -> None:
        writer = self.clone("state-writer")
        competitor = self.clone("state-competitor")
        (writer / "state" / "state.json").write_text('{"value":"writer"}\n', encoding="utf-8")
        (competitor / "state" / "state.json").write_text('{"value":"competitor"}\n', encoding="utf-8")
        git(competitor, "add", "state/state.json")
        git(competitor, "commit", "-m", "competing state")
        git(competitor, "push", "origin", "HEAD:main")

        result = self.checkpoint(writer)
        self.assertNotEqual(result.returncode, 0)
        verify = self.clone("verify-state-race")
        self.assertEqual(
            json.loads((verify / "state" / "state.json").read_text())["value"],
            "competitor",
        )

    def test_disjoint_state_push_race_also_fails_closed(self) -> None:
        writer = self.clone("disjoint-writer")
        competitor = self.clone("disjoint-competitor")
        (writer / "state" / "state.json").write_text('{"value":"writer"}\n', encoding="utf-8")
        (competitor / "state" / "audit.json").write_text('{"event":"other"}\n', encoding="utf-8")
        git(competitor, "add", "state/audit.json")
        git(competitor, "commit", "-m", "disjoint state")
        git(competitor, "push", "origin", "HEAD:main")

        result = self.checkpoint(writer)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("remote main changed since writer basis", result.stderr)
        verify = self.clone("verify-disjoint-race")
        self.assertEqual(json.loads((verify / "state" / "state.json").read_text())["value"], "seed")
        self.assertTrue((verify / "state" / "audit.json").exists())

    def test_final_push_failure_is_visible(self) -> None:
        writer = self.clone("broken-remote")
        (writer / "state" / "state.json").write_text('{"value":"writer"}\n', encoding="utf-8")
        git(writer, "remote", "set-url", "origin", str(self.base / "missing.git"))
        result = self.checkpoint(writer)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stderr.count("state checkpoint fetch failed on attempt"), 3)
        self.assertIn("failed after 3 attempts", result.stderr)


if __name__ == "__main__":
    unittest.main()
