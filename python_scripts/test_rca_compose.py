"""`dt rca`: the CLCA of a defect gets a closing 'Same flaw elsewhere: ...' line built from BRIEF.md (each follow-up bullet
carries its ticket or its decline).
The Assembla API is mocked; nothing is written anywhere."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import dev_ticket
from develop_ticket import common
from develop_ticket import common

NL = chr(10)
BRIEF_HITS = """# 1
## Same flaw elsewhere
- a/b.ts:1 - same cast - decision: fix here
- c/d.ts:2 - same pattern - decision: follow-up - ticket: #18578
- e/f.ts:3 - same pattern - decision: follow-up - ticket: #18579
## Assumptions / Decisions
"""


def call(api, **kwargs):
    out = io.StringIO()
    args = dict(ticket="100", rca_file=None, clca_file=None, plain=False, yes=False)
    args.update(kwargs)
    with contextlib.redirect_stdout(out):
        code = dev_ticket.cmd_rca(api, mock.Mock(**args))
    line = [ln for ln in out.getvalue().splitlines() if ln.startswith("RESULT: ")][-1]
    return code, json.loads(line[len("RESULT: "):])


class RcaComposeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.folder = self.root / dev_ticket.CONFIG["tickets_dir"] / "100"
        self.folder.mkdir(parents=True)
        self.state = {"followups": [{"title": "t1", "number": 18578}, {"title": "t2", "number": 18579}]}
        (self.folder / "state.json").write_text(json.dumps(self.state), encoding="utf-8")
        (self.folder / "BRIEF.md").write_text(BRIEF_HITS, encoding="utf-8")
        self.rca = self.folder / "rca.md"
        self.rca.write_text("Root cause: x.", encoding="utf-8")
        self.clca = self.folder / "clca.md"
        self.clca.write_text("Added a guard and a test.", encoding="utf-8")
        patcher = mock.patch.object(common, "PROJECT_ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.api = mock.Mock()
        self.ticket = {"id": 1, "custom_fields": {"Category": "Bug", "RCA": "-NA-", "CLCA": "Pending"}}
        self.api.get_ticket.side_effect = lambda n: self.ticket

    def preview(self, **kw):
        return call(self.api, rca_file=str(self.rca), clca_file=str(self.clca), **kw)

    def final(self, name="clca"):
        return (self.folder / f"{name}-final.md").read_text(encoding="utf-8").strip()

    def test_closing_line_counts_hits_and_names_the_raised_tickets(self):
        code, res = self.preview()
        self.assertEqual((code, res["status"]), (0, "PREVIEW"))
        self.assertEqual(self.final(), "Added a guard and a test.\n"
                                       "Same flaw elsewhere: 3 hits (1 fixed here, 2 follow-ups (#18578, #18579)).")
        self.assertEqual(self.final("rca"), "Root cause: x.")
        self.assertEqual(self.clca.read_text(encoding="utf-8"), "Added a guard and a test.")  # the source is untouched

    def test_the_tickets_come_from_the_bullets_not_from_state(self):
        (self.folder / "state.json").write_text("{}", encoding="utf-8")
        self.preview()
        self.assertIn("2 follow-ups (#18578, #18579)", self.final())

    def test_a_declined_follow_up_is_counted(self):
        (self.folder / "BRIEF.md").write_text(
            "## Same flaw elsewhere" + NL + "- a/b.ts:1 - x - decision: fix here" + NL
            + "- c/d.ts:2 - x - decision: follow-up - ticket: #18578" + NL
            + "- e/f.ts:3 - x - decision: follow-up - declined: dead code, the developer says" + NL, encoding="utf-8")
        self.preview()
        self.assertTrue(self.final().endswith("Same flaw elsewhere: 3 hits (1 fixed here, 1 follow-up (#18578), 1 declined)."))

    def test_a_follow_up_with_no_ticket_and_no_decline_leaves_no_closing_line(self):
        (self.folder / "BRIEF.md").write_text(
            "## Same flaw elsewhere" + NL + "- c/d.ts:2 - x - decision: follow-up" + NL, encoding="utf-8")
        _code, res = self.preview()
        self.assertNotIn("Same flaw elsewhere", self.final())

    def test_not_applicable_sweep(self):
        (self.folder / "BRIEF.md").write_text("## Same flaw elsewhere\nnot applicable: a one-off typo, no pattern\n", encoding="utf-8")
        self.preview()
        self.assertTrue(self.final().endswith("Same flaw elsewhere: not applicable."))

    def test_own_line_plain_flag_non_defect_and_missing_sweep_leave_the_text_alone(self):
        self.clca.write_text("Guard added.\nSame flaw elsewhere: none found by me.", encoding="utf-8")
        self.preview()
        self.assertEqual(self.final(), "Guard added.\nSame flaw elsewhere: none found by me.")
        self.clca.write_text("Guard added.", encoding="utf-8")
        self.preview(plain=True)
        self.assertEqual(self.final(), "Guard added.")
        (self.folder / "BRIEF.md").write_text("# no sweep here", encoding="utf-8")
        self.preview()
        self.assertEqual(self.final(), "Guard added.")
        self.ticket["custom_fields"]["Category"] = "CR"
        (self.folder / "BRIEF.md").write_text(BRIEF_HITS, encoding="utf-8")
        self.preview()
        self.assertEqual(self.final(), "Guard added.")

    def test_over_the_cap_refuses_and_says_the_closing_line_is_fixed(self):
        self.clca.write_text("x" * 1000, encoding="utf-8")
        code, res = self.preview()
        self.assertEqual((code, res["status"], res["field"]), (1, "TOO_LONG", "CLCA"))
        self.assertIn("closing line", res["message"])
        self.api.update_ticket.assert_not_called()

    def test_yes_writes_the_composed_text(self):
        stored = {}
        self.api.update_ticket.side_effect = lambda n, fields: stored.update(fields["custom_fields"])
        self.api.get_ticket.side_effect = lambda n: {**self.ticket, "custom_fields": {**self.ticket["custom_fields"], **stored}}
        code, res = self.preview(yes=True)
        self.assertEqual((code, res["status"]), (0, "UPDATED"))
        self.assertIn("Same flaw elsewhere: 3 hits", stored["CLCA"])


if __name__ == "__main__":
    unittest.main()
