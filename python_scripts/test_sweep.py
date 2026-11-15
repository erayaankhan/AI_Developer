"""CLCA sweep record: the BRIEF.md section needed before a defect goes to Code Review."""
import unittest

import sweep


NL = chr(10)


def brief(body: str) -> str:
    return f"# 1 - x\n## Problem\nfoo\n## Same flaw elsewhere\n{body}\n## Assumptions / Decisions\nbar\n"


class SweepTests(unittest.TestCase):
    def test_missing_section(self):
        self.assertTrue(sweep.sweep_problems("# 1\n## Problem\nx\n"))
        self.assertTrue(sweep.sweep_problems(""))

    def test_empty_or_placeholder(self):
        self.assertTrue(sweep.sweep_problems(brief("")))
        self.assertTrue(sweep.sweep_problems(brief("TBD")))

    def test_not_applicable_needs_a_reason(self):
        self.assertEqual(sweep.sweep_problems(brief("not applicable: a typo in one label, not a code pattern")), [])
        self.assertTrue(sweep.sweep_problems(brief("not applicable: n/a")))

    def test_each_hit_needs_a_decision(self):
        ok = "- a/B.cs:12 - same cast - decision: fix here\n- c/D.ts:3 - same pattern - decision: follow-up - ticket: #1"
        self.assertEqual(sweep.sweep_problems(brief(ok)), [])
        bad = ok + "\n- e/F.cs:9 - same cast"
        self.assertEqual(len(sweep.sweep_problems(brief(bad))), 1)

    def test_a_follow_up_hit_needs_a_ticket_or_a_decline(self):
        bare = "- c/D.ts:3 - same pattern - decision: follow-up"
        problems = sweep.sweep_problems(brief(bare))
        self.assertEqual(len(problems), 1)
        self.assertIn("dt followup-create --hit", problems[0])
        self.assertEqual(sweep.sweep_problems(brief(bare + " - ticket: #18578")), [])
        self.assertEqual(sweep.sweep_problems(brief(bare + " - declined: dead code, the developer decided")), [])

    def test_a_decline_needs_a_real_reason(self):
        self.assertEqual(len(sweep.sweep_problems(brief("- c/D.ts:3 - x - decision: follow-up - declined: no"))), 1)
        self.assertEqual(len(sweep.sweep_problems(brief("- c/D.ts:3 - x - decision: follow-up - declined:"))), 1)

    def test_a_fix_here_hit_needs_no_ticket(self):
        self.assertEqual(sweep.sweep_problems(brief("- a/B.cs:12 - same cast - decision: fix here")), [])

    def test_counts_list_the_tickets_and_the_declines(self):
        body = NL.join(["- a:1 - x - decision: fix here", "- b:2 - x - decision: follow-up - ticket: #11",
                        "- c:3 - x - decision: follow-up - declined: not worth a ticket",
                        "- d:4 - x - decision: follow-up - ticket: #12"])
        self.assertEqual(sweep.sweep_counts(brief(body)), {"fixed": 1, "followup": 3, "tickets": [11, 12], "declined": 1})

    def test_find_hit_matches_one_open_follow_up_bullet(self):
        text = brief(NL.join(["- a/B.cs:12 - x - decision: fix here", "- c/D.ts:3 - y - decision: follow-up",
                              "- c/D.ts:30 - z - decision: follow-up - ticket: #5", "- e/F.ts:9 - w - decision: follow-up"]))
        status, line = sweep.find_hit(text, "c/D.ts:3")
        self.assertEqual((status, line.strip()), ("OK", "- c/D.ts:3 - y - decision: follow-up"))  # the ticketed :30 is no candidate
        self.assertEqual(sweep.find_hit(text, "a/B.cs:12")[0], "HIT_NOT_FOUND")  # a fix-here hit is never ticketed
        self.assertEqual(sweep.find_hit(text, "nowhere.cs")[0], "HIT_NOT_FOUND")
        self.assertEqual(sweep.find_hit(text, "")[0], "HIT_NOT_FOUND")
        status, lines = sweep.find_hit(text, ".ts")
        self.assertEqual((status, len(lines)), ("HIT_AMBIGUOUS", 2))

    def test_find_hit_is_case_insensitive_and_needs_the_section(self):
        self.assertEqual(sweep.find_hit(brief("- C/D.ts:3 - y - decision: follow-up"), "c/d.TS:3")[0], "OK")
        self.assertEqual(sweep.find_hit("# no sweep", "c/d.ts")[0], "HIT_NOT_FOUND")

    def test_link_ticket_appends_to_that_bullet_only(self):
        text = brief(NL.join(["- a/B.cs:12 - x - decision: fix here", "- c/D.ts:3 - y - decision: follow-up"]))
        status, new = sweep.link_ticket(text, "c/D.ts:3", 18600)
        self.assertEqual(status, "OK")
        self.assertIn("- c/D.ts:3 - y - decision: follow-up - ticket: #18600" + NL, new)
        self.assertIn("- a/B.cs:12 - x - decision: fix here" + NL, new)
        self.assertEqual(sweep.sweep_problems(new), [])
        self.assertEqual(sweep.link_ticket(new, "c/D.ts:3", 18601), ("HIT_NOT_FOUND", None))  # never tagged twice

    def test_prose_without_bullets_is_refused(self):
        self.assertTrue(sweep.sweep_problems(brief("I looked and found some.")))


if __name__ == "__main__":
    unittest.main()
