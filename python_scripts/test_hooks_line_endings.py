"""The line-ending pre-commit hooks (scripts/hooks): they follow what git will store, not what is on disk, so a CRLF
working file (core.autocrlf=true) no longer fails every commit, and a legacy CRLF file is not rewritten whole.
Runs the real PowerShell scripts against throw-away git repos; skipped where powershell or git is missing."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EOF_HOOK = ROOT / "scripts" / "hooks" / "fix-end-of-file.ps1"
EOL_HOOK = ROOT / "scripts" / "hooks" / "fix-mixed-line-ending.ps1"
POWERSHELL = shutil.which("powershell")


@unittest.skipUnless(POWERSHELL and shutil.which("git") and EOF_HOOK.is_file(), "needs powershell, git and the hooks")
class HookCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)
        self.git("init", "-q")
        self.git("config", "user.name", "t")
        self.git("config", "user.email", "t@t")

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, capture_output=True, text=True, check=True).stdout

    def put(self, name, data: bytes):
        (self.repo / name).write_bytes(data)

    def read(self, name) -> bytes:
        return (self.repo / name).read_bytes()

    def hook(self, script, *files) -> int:
        return subprocess.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), *files],
                              cwd=self.repo, capture_output=True, text=True).returncode

    def stage(self, name, data: bytes, autocrlf: str):
        self.git("config", "core.autocrlf", autocrlf)
        self.put(name, data)
        self.git("add", name)


class EndOfFileTests(HookCase):
    def run_hook(self, data: bytes):
        self.put("a.txt", data)
        code = self.hook(EOF_HOOK, "a.txt")
        return code, self.read("a.txt")

    def test_a_crlf_file_with_its_final_newline_is_untouched(self):
        self.assertEqual(self.run_hook(b"a\r\nb\r\n"), (0, b"a\r\nb\r\n"))

    def test_a_crlf_file_missing_its_final_newline_gets_a_crlf_not_an_lf(self):
        self.assertEqual(self.run_hook(b"a\r\nb"), (1, b"a\r\nb\r\n"))

    def test_extra_blank_lines_are_trimmed_keeping_crlf(self):
        self.assertEqual(self.run_hook(b"a\r\n\r\n\r\n"), (1, b"a\r\n"))

    def test_an_lf_file_stays_lf(self):
        self.assertEqual(self.run_hook(b"a\nb"), (1, b"a\nb\n"))
        self.assertEqual(self.run_hook(b"a\nb\n"), (0, b"a\nb\n"))

    def test_empty_and_binary_files_are_skipped(self):
        self.assertEqual(self.run_hook(b""), (0, b""))
        self.assertEqual(self.run_hook(b"\x00\x01\r\n"), (0, b"\x00\x01\r\n"))


class MixedLineEndingTests(HookCase):
    def test_a_crlf_working_file_passes_when_git_stores_lf(self):
        self.stage("a.ts", b"a\r\nb\r\n", "true")
        self.assertIn("i/lf", self.git("ls-files", "--eol", "a.ts"))
        self.assertEqual(self.hook(EOL_HOOK, "a.ts"), 0)
        self.assertEqual(self.read("a.ts"), b"a\r\nb\r\n")

    def test_a_mixed_working_file_passes_when_git_stores_lf(self):
        self.stage("a.ts", b"a\r\nb\nc\r\n", "true")
        self.assertEqual(self.hook(EOL_HOOK, "a.ts"), 0)
        self.assertEqual(self.read("a.ts"), b"a\r\nb\nc\r\n")

    def test_an_lf_file_passes(self):
        self.stage("a.ts", b"a\nb\n", "false")
        self.assertEqual(self.hook(EOL_HOOK, "a.ts"), 0)

    def test_new_content_that_git_would_store_with_crlf_is_converted_to_lf(self):
        self.stage("a.ts", b"a\r\nb\r\n", "false")
        self.assertIn("i/crlf", self.git("ls-files", "--eol", "a.ts"))
        self.assertEqual(self.hook(EOL_HOOK, "a.ts"), 1)
        self.assertEqual(self.read("a.ts"), b"a\nb\n")

    def test_new_content_stored_mixed_is_converted_to_lf(self):
        self.stage("a.ts", b"a\r\nb\nc\r\n", "false")
        self.assertEqual(self.hook(EOL_HOOK, "a.ts"), 1)
        self.assertEqual(self.read("a.ts"), b"a\nb\nc\n")

    def test_a_legacy_file_already_crlf_in_head_is_left_alone(self):
        self.stage("a.ts", b"a\r\nb\r\n", "false")
        self.git("commit", "-q", "-m", "legacy crlf")
        self.stage("a.ts", b"a\r\nb\r\nc\r\n", "false")  # an edit; still stored as CRLF
        self.assertIn("i/crlf", self.git("ls-files", "--eol", "a.ts"))
        self.assertEqual(self.hook(EOL_HOOK, "a.ts"), 0)
        self.assertEqual(self.read("a.ts"), b"a\r\nb\r\nc\r\n")

    def test_a_file_that_was_lf_in_head_and_turns_crlf_is_converted(self):
        self.stage("a.ts", b"a\nb\n", "false")
        self.git("commit", "-q", "-m", "lf")
        self.stage("a.ts", b"a\r\nb\r\n", "false")
        self.assertEqual(self.hook(EOL_HOOK, "a.ts"), 1)
        self.assertEqual(self.read("a.ts"), b"a\nb\n")

    def test_no_files_and_binary_files_pass(self):
        self.assertEqual(self.hook(EOL_HOOK), 0)
        self.stage("a.bin", b"\x00\r\n\x00", "false")
        self.assertEqual(self.hook(EOL_HOOK, "a.bin"), 0)
        self.assertEqual(self.read("a.bin"), b"\x00\r\n\x00")


if __name__ == "__main__":
    unittest.main()
