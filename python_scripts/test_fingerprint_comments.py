"""diff_fingerprint ignores documentation files and comment-only lines, and nothing else."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import local_gates


class FingerprintTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        patcher = mock.patch.object(local_gates, "PROJECT_ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def put(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))

    def fp(self, *files):
        return local_gates.diff_fingerprint(list(files))

    def same(self, rel, before, after):
        self.put(rel, before)
        first = self.fp(rel)
        self.put(rel, after)
        self.assertEqual(first, self.fp(rel))

    def differs(self, rel, before, after):
        self.put(rel, before)
        first = self.fp(rel)
        self.put(rel, after)
        self.assertNotEqual(first, self.fp(rel))

    def test_documentation_files_are_ignored(self):
        self.put("a.cs", "int x = 1;\n")
        base = self.fp("a.cs")
        for doc in ("README.md", "notes.txt", "spec.docx", "old.doc", "x/docs/guide.html", "x/Docs/img.png"):
            self.put(doc, "one")
            with self.subTest(doc):
                self.assertEqual(base, self.fp("a.cs", doc))

    def test_a_doc_change_does_not_change_the_fingerprint(self):
        self.same("README.md", "one", "two")

    def test_full_line_comments_are_ignored(self):
        self.same("a.cs", "int x = 1;\nint y = 2;\n", "// a note\nint x = 1;\n/// <summary>s</summary>\nint y = 2;\n")
        self.same("a.ts", "let x = 1;\n", "// note\nlet x = 1;\n")
        self.same("a.sql", "SELECT 1;\n", "-- note\nSELECT 1;\n")
        self.same("a.py", "x = 1\n", "# note\nx = 1\n")
        self.same("a.scss", ".a { b: c; }\n", "// note\n.a { b: c; }\n")

    def test_block_comments_are_ignored(self):
        self.same("a.cs", "int x = 1;\n", "/* one */\nint x = 1;\n/*\n * many\n * lines\n */\n")
        self.same("a.css", ".a { b: c; }\n", "/* note */\n.a { b: c; }\n")
        self.same("a.html", "<p>x</p>\n", "<!-- note -->\n<p>x</p>\n<!--\nmany\nlines\n-->\n")

    def test_editing_a_comment_is_ignored(self):
        self.same("a.cs", "// old text\nint x = 1;\n", "// new text\nint x = 1;\n")

    def test_code_changes_still_count(self):
        self.differs("a.cs", "int x = 1;\n", "int x = 2;\n")
        self.differs("a.ts", "let x = 1;\n", "let x = 1;\nlet y = 2;\n")
        self.differs("a.sql", "SELECT 1;\n", "SELECT 2;\n")
        self.differs("a.html", "<p>x</p>\n", "<p>y</p>\n")

    def test_a_trailing_comment_after_code_still_counts(self):
        self.differs("a.cs", "int x = 1; // old\n", "int x = 1; // new\n")

    def test_code_after_a_block_comment_on_the_same_line_still_counts(self):
        self.differs("a.cs", "/* c */ int x = 1;\n", "/* c */ int x = 2;\n")
        self.differs("a.cs", "/*\n c */ int x = 1;\n", "/*\n c */ int x = 2;\n")

    def test_a_double_slash_inside_a_string_is_code(self):
        self.differs("a.ts", 'const u = "http://a/b";\n', 'const u = "http://a/c";\n')

    def test_commenting_out_code_counts(self):
        self.differs("a.cs", "int x = 1;\nint y = 2;\n", "int x = 1;\n// int y = 2;\n")

    def test_other_file_types_are_hashed_as_they_are(self):
        self.differs("a.json", '{"a": 1}\n', '{"a": 2}\n')
        self.differs("a.csproj", "<a/>\n", "<!-- n -->\n<a/>\n")

    def test_line_ending_style_in_a_code_file_is_ignored(self):
        self.same("a.cs", "int x = 1;\nint y = 2;\n", "int x = 1;\r\nint y = 2;\r\n")

    def test_a_missing_file_differs_from_an_existing_one(self):
        self.put("a.cs", "int x = 1;\n")
        self.assertNotEqual(self.fp("a.cs"), self.fp("b.cs"))


if __name__ == "__main__":
    unittest.main()
