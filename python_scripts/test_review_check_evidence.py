"""review_check.check_finding: the Evidence excerpt is matched against the cited line, including code that holds backticks."""
import tempfile
import unittest
from pathlib import Path

import review_check

SOURCE = (
    "export class A {\n"
    "  label = `a${this.b}`;\n"
    "  path = 'C:\\\\temp\\\\x';\n"
    "  re = /a\\`b/;\n"
    "  total = x || y;\n"
    "}\n"
)


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "a.ts").write_text(SOURCE, encoding="utf-8")
        self.resolver = review_check.Resolver(self.root, self.root / "db")
        self.resolver.resolve = lambda name: (self.root / name, "")  # no git repo in the temp dir

    def status(self, evidence, line=2):
        finding = {"num": "1", "severity": "High", "category": "bug", "cite": f"a.ts:{line}", "evidence": evidence}
        return review_check.check_finding(self.resolver, finding)["status"]

    def test_plain_code_is_found_wrapped_or_not(self):
        self.assertEqual(self.status("total = x || y;", 5), "OK")
        self.assertEqual(self.status("`total = x || y;`", 5), "OK")

    def test_a_template_literal_with_bare_backticks_is_found(self):
        self.assertEqual(self.status("label = `a${this.b}`;"), "OK")
        self.assertEqual(self.status("``label = `a${this.b}`;``"), "OK")

    def test_backticks_escaped_in_the_markdown_cell_are_found(self):
        self.assertEqual(self.status("label = \\`a${this.b}\\`;"), "OK")
        self.assertEqual(self.status("`label = \\`a${this.b}\\`;`"), "OK")

    def test_a_backslash_that_is_really_in_the_code_still_matches(self):
        self.assertEqual(self.status("path = 'C:\\\\temp\\\\x';", 3), "OK")
        self.assertEqual(self.status("re = /a\\`b/;", 4), "OK")  # a literal backslash before a backtick in the file

    def test_text_that_is_not_in_the_file_is_still_not_found(self):
        self.assertEqual(self.status("label = \\`other${this.b}\\`;"), "NOT_FOUND")
        self.assertEqual(self.status("label = a${this.b};"), "NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
