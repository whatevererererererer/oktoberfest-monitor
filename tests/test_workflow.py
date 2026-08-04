from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

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
        self.assertEqual(job["timeout-minutes"], 6)
        checkout = job["steps"][0]
        self.assertEqual(checkout["with"]["ref"], "main")
        self.assertEqual(checkout["with"]["fetch-depth"], 0)
        workflow_text = WORKFLOW.read_text(encoding="utf-8")
        budget = workflow_text.index("MONITOR_JOB_STARTED=$(date +%s)")
        setup = workflow_text.index("actions/setup-python")
        probe = workflow_text.index("python -m src.main --probe")
        checkpoint = workflow_text.index("checkpoint_state.sh probe")
        delivery = workflow_text.index("python -m src.main --deliver-next")
        self.assertLess(budget, setup)
        self.assertLess(probe, checkpoint)
        self.assertLess(checkpoint, delivery)
        self.assertIn("$(date +%s) - MONITOR_JOB_STARTED", workflow_text)
        self.assertIn("git push origin HEAD:main", CHECKPOINT.read_text(encoding="utf-8"))


@unittest.skipUnless(GIT and BASH, "git/bash required")
class GitCheckpointIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / "work")
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

    def clone(self, name: str) -> Path:
        path = self.base / name
        subprocess.run(
            ["git", "clone", str(self.remote), str(path)],
            check=True,
            text=True,
            capture_output=True,
        )
        git(path, "config", "user.name", "test")
        git(path, "config", "user.email", "test@example.com")
        return path

    def checkpoint(self, repo: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [BASH or "bash", CHECKPOINT.as_posix(), "test"],
            cwd=repo,
            check=False,
            text=True,
            capture_output=True,
        )

    def test_code_only_push_race_rebases_without_losing_state(self) -> None:
        writer = self.clone("writer")
        code_writer = self.clone("code-writer")
        (writer / "state" / "state.json").write_text('{"value":"writer"}\n', encoding="utf-8")
        (code_writer / "code.txt").write_text("new code\n", encoding="utf-8")
        git(code_writer, "add", "code.txt")
        git(code_writer, "commit", "-m", "code")
        git(code_writer, "push", "origin", "HEAD:main")

        result = self.checkpoint(writer)
        self.assertEqual(result.returncode, 0, result.stderr)
        verify = self.clone("verify-code-race")
        self.assertEqual(
            json.loads((verify / "state" / "state.json").read_text())["value"],
            "writer",
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

    def test_final_push_failure_is_visible(self) -> None:
        writer = self.clone("broken-remote")
        (writer / "state" / "state.json").write_text('{"value":"writer"}\n', encoding="utf-8")
        git(writer, "remote", "set-url", "origin", str(self.base / "missing.git"))
        result = self.checkpoint(writer)
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
