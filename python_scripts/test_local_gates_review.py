"""local_gates in a review: files the pre-commit fixers rewrote are restored, the reviewer's own edits are not."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import local_gates


class RestoreGateChangesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for args in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
            self.run_git(*args)
        for name in ("fixed.ts", "own.ts"):
            (self.root / name).write_text("a\n", encoding="utf-8")
        self.run_git("add", ".")
        self.run_git("commit", "-q", "-m", "init")
        patcher = mock.patch.object(local_gates, "PROJECT_ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_git(self, *args):
        subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True)

    def text(self, name):
        return (self.root / name).read_bytes().decode().replace("\r\n", "\n")  # core.autocrlf may write CRLF

    def test_what_the_gates_changed_is_restored_but_earlier_edits_are_kept(self):
        (self.root / "own.ts").write_bytes(b"mine\n")  # the reviewer's own edit, before the gates
        before = local_gates.tracked_changes()
        (self.root / "fixed.ts").write_bytes(b"a \r\n")  # what a fixer rewrote
        (self.root / "own.ts").write_bytes(b"mine and the fixer\n")
        restored = local_gates.restore_gate_changes(before)
        self.assertEqual(restored, ["fixed.ts"])
        self.assertEqual(self.text("fixed.ts"), "a\n")
        self.assertEqual(self.text("own.ts"), "mine and the fixer\n")

    def test_only_a_review_folder_is_treated_as_a_review(self):
        folder = self.root / local_gates.CONFIG["tickets_dir"] / "7"
        folder.mkdir(parents=True)
        self.assertFalse(local_gates.is_review_ticket("7"))
        (folder / "state.json").write_text(json.dumps({"kind": "review"}), encoding="utf-8")
        self.assertTrue(local_gates.is_review_ticket("7"))
        (folder / "state.json").write_text(json.dumps({"stage": "6-build"}), encoding="utf-8")
        self.assertFalse(local_gates.is_review_ticket("7"))
        self.assertFalse(local_gates.is_review_ticket(None))


class NgTestBatchTests(unittest.TestCase):
    def test_a_long_spec_list_is_split_into_numbered_ng_test_gates(self):
        specs = [f"src/app/feature-{i}/feature-{i}.component.spec.ts" for i in range(300)]
        with mock.patch.object(local_gates, "angular_specs", return_value={"C2MWFAng": specs}):
            gates = local_gates.plan_gates([], {"pre-commit", "dotnet-build", "dotnet-test", "db-checks"})
        ng = [g for g in gates if g["name"].startswith("ng-test")]
        self.assertGreater(len(ng), 1)
        self.assertEqual(ng[0]["name"], "ng-test:C2MWFAng")
        self.assertEqual(ng[1]["name"], "ng-test:C2MWFAng#2")
        included = [a.split("=", 1)[1] for g in ng for a in g["cmd"] if a.startswith("--include=")]
        self.assertEqual(included, specs)
        for gate in ng:
            self.assertLess(len(" ".join(gate["cmd"])), 8191)

    def test_a_short_spec_list_stays_one_gate(self):
        with mock.patch.object(local_gates, "angular_specs", return_value={"C2MWFAng": ["src/a.spec.ts"]}):
            gates = local_gates.plan_gates([], {"pre-commit", "dotnet-build", "dotnet-test", "db-checks"})
        self.assertEqual([g["name"] for g in gates], ["ng-test:C2MWFAng"])


if __name__ == "__main__":
    unittest.main()
