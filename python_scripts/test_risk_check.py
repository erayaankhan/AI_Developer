"""risk_check: documentation files carry no canvas or size risk."""
import unittest
from unittest import mock

import risk_check

README = "c2mwf_admin/src/app/ai-flow-builder3/README.md"
SERVICE = "c2mwf_admin/src/app/ai-flow-builder3/services/afd-graph.service.ts"


class DocPathRiskTests(unittest.TestCase):
    def assess(self, files, lines=10):
        counted = []
        with mock.patch.object(risk_check, "diff_base", return_value="base"), \
                mock.patch.object(risk_check, "git", return_value=""), \
                mock.patch.object(risk_check, "changed_lines", side_effect=lambda f, b: counted.append(list(f)) or lines):
            return risk_check.assess(files), counted[0]

    def test_doc_paths(self):
        self.assertTrue(all(risk_check.is_doc_path(f) for f in (README, "a/notes.txt", "x/docs/img.png")))
        self.assertFalse(any(risk_check.is_doc_path(f) for f in (SERVICE, "a/b.component.html", "docs.ts")))

    def test_a_readme_alone_is_not_a_canvas_change_and_is_not_counted(self):
        outcome, counted = self.assess([README])
        self.assertEqual(outcome["min_tier"], "LOW")
        self.assertEqual(counted, [])

    def test_code_next_to_a_readme_is_still_a_canvas_change_and_only_code_is_counted(self):
        outcome, counted = self.assess([README, SERVICE])
        self.assertEqual(outcome["min_tier"], "MEDIUM")
        self.assertEqual([r for r in outcome["reasons"] if "README" in r], [])
        self.assertEqual(counted, [SERVICE])


if __name__ == "__main__":
    unittest.main()
