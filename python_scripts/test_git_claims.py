"""Claims the skills and docs make about files and git, checked against git itself: what they call git-ignored really is
ignored, what they call tracked really is, and the tracked env files hold no password. Skipped outside a git checkout."""
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
IGNORED = ["AI_DEV_ENV/credentials.env", "DEV_TICKETS/0/state.json",
           "wfapi_wf_service/wfapi_wf_microservice/appsettings.local.json"]
TRACKED = ["AI_DEV_ENV/local_env.env", "AI_DEV_ENV/python_scripts/local_env.env.example"]
SECRET = re.compile(r"\b(password|pwd)\s*=\s*[^\s;]", re.I)


def git(*args):
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True)


class GitClaimsTests(unittest.TestCase):
    def setUp(self):
        try:
            inside = git("rev-parse", "--is-inside-work-tree").returncode == 0
        except OSError:
            inside = False
        if not inside:
            self.skipTest("not a git checkout")

    def test_files_called_git_ignored_are_ignored(self):
        for rel in IGNORED:
            with self.subTest(rel):
                self.assertEqual(git("check-ignore", "-q", rel).returncode, 0, f"{rel} is not git-ignored")

    def test_files_called_tracked_are_tracked(self):
        for rel in TRACKED:
            with self.subTest(rel):
                self.assertEqual(git("ls-files", "--error-unmatch", rel).returncode, 0, f"{rel} is not tracked")

    def test_tracked_env_files_hold_no_password(self):
        for rel in TRACKED:
            path = ROOT / rel
            if not path.is_file():
                continue
            lines = path.read_text(encoding="utf-8").splitlines()
            hits = [n for n, line in enumerate(lines, 1) if not line.lstrip().startswith("#") and SECRET.search(line)]
            with self.subTest(rel):
                self.assertEqual(hits, [], f"{rel} line(s) {hits} hold a password: move it to a git-ignored overlay")


if __name__ == "__main__":
    unittest.main()
