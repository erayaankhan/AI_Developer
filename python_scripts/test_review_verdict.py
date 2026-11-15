"""Phase 3 of the reviewer review (REVIEW_TICKET_PHASE3_PLAN.md): the reviewer status moves, the one-PUT assign-and-move API
call, and the REVIEW_VERDICT format (validation, rendering, parsing). No Assembla or git call is made."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import dev_ticket
from develop_ticket import common, review
from assembla_api import AssemblaApi, AssemblaError


class ConfigTests(unittest.TestCase):
    def test_reviewer_status_moves(self):
        moves = dev_ticket.CONFIG["reviewer_status_moves"]
        # phase 3's two verdict moves (phase 4 adds the deploy request and the withdraw: see test_review_deploy.py)
        self.assertIn(["Code Review", "Dev Done"], moves)
        self.assertIn(["Code Review", "Dev Queue"], moves)
        known = set(dev_ticket.STATUS.values())
        self.assertTrue(all(a in known and b in known for a, b in moves))


class AssignAndMoveTests(unittest.TestCase):
    def api(self, after):
        api = AssemblaApi.__new__(AssemblaApi)  # the HTTP layer is mocked
        api.update_ticket = mock.Mock()
        api.get_ticket = mock.Mock(return_value=after)
        return api

    def test_both_fields_go_in_one_put(self):
        api = self.api({"assigned_to_id": "u1", "status": "Dev Queue"})
        api.assign_and_move("100", "u1", "Dev Queue")
        api.update_ticket.assert_called_once_with("100", {"assigned_to_id": "u1", "status": "Dev Queue"})

    def test_an_ignored_status_is_an_error(self):
        with self.assertRaises(AssemblaError) as ctx:
            self.api({"assigned_to_id": "u1", "status": "Code Review"}).assign_and_move("100", "u1", "Dev Queue")
        self.assertIn("status", str(ctx.exception))

    def test_an_ignored_assignee_is_an_error(self):
        with self.assertRaises(AssemblaError) as ctx:
            self.api({"assigned_to_id": "s1", "status": "Dev Queue"}).assign_and_move("100", "u1", "Dev Queue")
        self.assertIn("assignee", str(ctx.exception))


def good_reject(**over):
    v = {"verdict": "REJECT", "summary": "The save handler loses the node id on a rename.",
         "findings": [{"severity": "High", "file_line": "c2mwf_admin/src/app/a.ts:42",
                       "finding": "The rename branch returns before the id is copied.",
                       "fix": "Copy the id first, then return."}]}
    v.update(over)
    return v


def good_approve(**over):
    v = {"verdict": "APPROVE", "summary": "Behaviour, tests and the UI checks match the acceptance criteria.",
         "findings": [{"severity": "Low", "file_line": "general", "finding": "A comment could be clearer.",
                       "fix": "Optional."}]}
    v.update(over)
    return v


class VerdictProblemsTests(unittest.TestCase):
    def errors(self, v, ai_open=False):
        return dev_ticket.verdict_problems(v, ai_open)

    def has(self, v, text, ai_open=False):
        return any(text in e for e in self.errors(v, ai_open))

    def test_valid_verdicts_have_no_errors(self):
        self.assertEqual(self.errors(good_reject()), [])
        self.assertEqual(self.errors(good_approve()), [])

    def test_the_verdict_value_and_summary_are_required(self):
        self.assertTrue(self.has(good_reject(verdict="MAYBE"), "verdict"))
        self.assertTrue(self.has(good_reject(summary=" "), "summary"))
        self.assertTrue(self.errors("not a dict"))

    def test_a_reject_needs_a_complete_finding(self):
        self.assertTrue(self.has(good_reject(findings=[]), "at least one finding"))
        for field in ("finding", "fix"):
            bad = good_reject()
            bad["findings"][0][field] = ""
            self.assertTrue(self.has(bad, field), field)

    def test_severity_and_location_are_checked(self):
        bad = good_reject()
        bad["findings"][0]["severity"] = "Critical"
        self.assertTrue(self.has(bad, "severity"))
        for location in ("a.ts", "a.ts:x", "<a>.ts:3", ""):
            bad = good_reject()
            bad["findings"][0]["file_line"] = location
            self.assertTrue(self.has(bad, "file_line"), location)
        for location in ("general", "a/b.ts:3", "a/b.ts:3-9"):
            ok = good_reject()
            ok["findings"][0]["file_line"] = location
            self.assertEqual(self.errors(ok), [], location)

    def test_an_approval_cannot_carry_a_high_or_medium_finding(self):
        for severity in ("High", "Medium"):
            bad = good_approve()
            bad["findings"][0]["severity"] = severity
            self.assertTrue(self.has(bad, "cannot carry"), severity)

    def test_an_open_ai_finding_needs_an_override_reason_to_approve(self):
        self.assertTrue(self.has(good_approve(), "override_reason", ai_open=True))
        self.assertEqual(self.errors(good_approve(override_reason="The finding is a false positive: see note."), ai_open=True), [])
        self.assertEqual(self.errors(good_reject(), ai_open=True), [])  # a reject never needs one

    def test_assembla_markup_rules_apply_to_every_text_field(self):
        for field, text in (("summary", "uses <b> tags"), ("summary", "uses `code`"), ("summary", "**bold**"),
                            ("summary", "two\nlines"), ("summary", r"see D:\work\x.ts"), ("summary", "TODO later")):
            self.assertTrue(self.has(good_reject(**{field: text}), field), text)
        for field in ("finding", "fix"):
            bad = good_reject()
            bad["findings"][0][field] = "has a <tag>"
            self.assertTrue(self.has(bad, field), field)
        self.assertTrue(self.has(good_reject(notes=["a `tick`"]), "notes"))
        self.assertTrue(self.has(good_reject(fix_list=["a > b"]), "fix_list"))

    def test_lengths_and_counts_are_capped(self):
        self.assertTrue(self.has(good_reject(summary="x" * 700), "summary"))
        many = [good_reject()["findings"][0]] * 31
        self.assertTrue(self.has(good_reject(findings=many), "findings"))


def payload_for(v, **over):
    base = dict(ticket="100", reviewer="reviewer1", author="developer1", head="a" * 40, round=1, tier="MEDIUM",
                checks={"gates": "PASS", "risk": "MEDIUM", "functional": "3 passed, 0 failed", "compare": "COMPARE_OK",
                        "ai_review": {"verdict": "PASS", "high": 0, "medium": 0, "low": 1}})
    base.update(over)
    return dev_ticket.verdict_payload(v, **base)


class VerdictFormatTests(unittest.TestCase):
    def test_payload_numbers_the_findings_and_derives_the_fix_list(self):
        v = good_reject()
        v["findings"].append({"severity": "Low", "file_line": "general", "finding": "Naming.", "fix": "Rename it."})
        p = payload_for(v)
        self.assertEqual([f["n"] for f in p["findings"]], [1, 2])
        self.assertEqual(p["fix_list"], ["Copy the id first, then return.", "Rename it."])
        self.assertEqual((p["verdict"], p["round"], p["tier"], p["author"], p["reviewer"]),
                         ("REJECT", 1, "MEDIUM", "developer1", "reviewer1"))

    def test_an_explicit_fix_list_is_kept(self):
        self.assertEqual(payload_for(good_reject(fix_list=["Do this first."]))["fix_list"], ["Do this first."])

    def test_the_approve_payload_has_no_fix_list(self):
        self.assertEqual(payload_for(good_approve())["fix_list"], [])

    def test_render_then_parse_round_trips(self):
        p = payload_for(good_reject(), tier="HIGH")
        p["summary"] = "A > B holds"  # the JSON block must survive angle brackets
        text = dev_ticket.render_verdict(p)
        self.assertNotIn(">", text.split("VERDICT-JSON-BEGIN")[1])
        self.assertEqual(dev_ticket.parse_verdict(text), p)

    def test_titles_and_sections(self):
        reject = dev_ticket.render_verdict(payload_for(good_reject(), round=2))
        self.assertTrue(reject.startswith("*Review of #100 (round 2): CHANGES REQUIRED*"))
        self.assertIn("*Findings*", reject)
        self.assertIn("*Fix before resubmitting*", reject)
        self.assertIn("* 1 (High) c2mwf_admin/src/app/a.ts:42 - The rename branch", reject)
        approve = dev_ticket.render_verdict(payload_for(good_approve(notes=["Merge after QA."])))
        self.assertTrue(approve.startswith("*Review of #100 (round 1): APPROVED*"))
        self.assertNotIn("Fix before resubmitting", approve)
        self.assertIn("*Notes*", approve)

    def test_the_override_reason_is_shown_in_the_comment(self):
        p = payload_for(good_approve(override_reason="False positive."), checks={
            "gates": "PASS", "risk": "LOW", "functional": "1 passed, 0 failed", "compare": "COMPARE_OK",
            "ai_review": {"verdict": "CHANGES REQUIRED", "high": 1, "medium": 0, "low": 0}})
        self.assertIn("Override: False positive.", dev_ticket.render_verdict(p))

    def test_the_readable_part_is_assembla_wiki_markup(self):
        for v in (good_reject(), good_approve()):
            human = dev_ticket.render_verdict(payload_for(v)).split("VERDICT-JSON-BEGIN")[0]
            self.assertNotIn("`", human)
            self.assertNotIn("**", human)
            for line in (ln for ln in human.splitlines() if ln.strip()):
                self.assertTrue(line.startswith("*"), line)  # a bold heading or a "* " bullet, nothing loose
            self.assertFalse(any(ln.lstrip().startswith("#") or ln.lstrip().startswith("- ") for ln in human.splitlines()))

    def test_parse_ignores_other_comments_and_bad_json(self):
        self.assertIsNone(dev_ticket.parse_verdict("just a note"))
        self.assertIsNone(dev_ticket.parse_verdict("VERDICT-JSON-BEGIN\n{nope\nVERDICT-JSON-END"))
        handoff = dev_ticket.render_handoff({"ticket": "1", "author": "a", "branch": "FB_1", "head": "a" * 40, "round": 1,
                                             "risk": "LOW", "path": "STANDARD", "risk_check": {"min_tier": "LOW", "reasons": []},
                                             "gates": {"result": "PASS", "at": "t", "files_count": 0, "files": []},
                                             "functional": {"mode": "run", "profile": "x", "passed": 1, "failed": 0,
                                                            "skip_reason": None},
                                             "db": None, "baseline": "PASS", "readme_impact": "none"})
        self.assertIsNone(dev_ticket.parse_verdict(handoff))
        self.assertIsNone(dev_ticket.parse_handoff(dev_ticket.render_verdict(payload_for(good_reject()))))

    def test_a_json_block_is_valid_json(self):
        text = dev_ticket.render_verdict(payload_for(good_reject()))
        block = text.split("VERDICT-JSON-BEGIN")[1].split("VERDICT-JSON-END")[0]
        self.assertEqual(json.loads(block)["verdict"], "REJECT")


A, B = "a" * 40, "b" * 40
AI_REVIEW = """# Code review - #100 x
Reviewer: independent sub-agent

## Verdict: CHANGES REQUIRED

## Findings
| # | Severity | Category | File:line | Evidence | Finding | Suggested fix |
|---|---|---|---|---|---|---|
| 1 | High | Correctness | a.ts:4 | `x` | bad | fix |
| 2 | Low | Style | a.ts:9 | | meh | rename |

## Dismissed
| # | Severity | Category | File:line | Evidence | Finding | Suggested fix |
|---|---|---|---|---|---|---|
| 3 | Medium | Style | a.ts:12 | `y` | not a bug | n/a |
"""


class AiReviewSummaryTests(unittest.TestCase):
    def test_counts_only_the_open_findings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "code-review.md"
            path.write_text(AI_REVIEW, encoding="utf-8")
            s = dev_ticket.ai_review_summary(path)
        self.assertEqual((s["verdict"], s["high"], s["medium"], s["low"], s["open_high_medium"]),
                         ("CHANGES REQUIRED", 1, 0, 1, 1))

    def test_a_missing_review_is_not_run(self):
        s = dev_ticket.ai_review_summary(Path("does-not-exist.md"))
        self.assertEqual((s["verdict"], s["open_high_medium"]), ("not run", 0))


def reviewer_state(**over):
    state = {"ticket": "100", "role": "reviewer", "kind": "review", "stage": "done", "branch": "FB_100",
             "review": {"stage": "R4-review", "author": "developer1", "head": A, "tier": "MEDIUM",
                        "handoff": {"comment_id": 1, "round": 1, "risk": "MEDIUM"},
                        "compare": {"status": "COMPARE_OK"}},
             "gates": {"status": "PASS"}, "risk_check": {"min_tier": "MEDIUM"},
             "functional_mode": "run", "functional": {"status": "PASS", "passed": 3, "failed": 0}}
    state.update(over)
    return state


class VerdictFixture(unittest.TestCase):
    """The shared setup of the reject and approve tests: a fetched review of ticket 100 and a mocked Assembla."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.folder = self.root / "DEV_TICKETS" / "100"
        (self.folder / "reviews").mkdir(parents=True)
        (self.folder / "reviews" / "code-review.md").write_text(AI_REVIEW, encoding="utf-8")
        self.write_state(reviewer_state())
        self.verdict_path = self.folder / "verdict.json"
        self.write_verdict(good_reject())
        self.api = mock.Mock()
        self.api.get_current_user.return_value = {"id": "s1", "login": "Reviewer1"}
        self.api.get_ticket.return_value = {"status": "Code Review", "assigned_to_id": "s1"}
        self.api.get_space_users.return_value = [{"id": "s1", "login": "Reviewer1"}, {"id": "j1", "login": "developer1"}]
        self.api.get_comments.return_value = []
        self.api.add_comment.return_value = 777
        self.tips = {"FB_100": ("OK", A), "FB_200": ("OK", B)}
        for patcher in (mock.patch.object(common, "PROJECT_ROOT", self.root),
                        mock.patch.dict(dev_ticket.CONFIG, {"reviewers": ["reviewer1"]}),
                        mock.patch.object(common, "_remembered_role", return_value="reviewer"),
                        mock.patch.object(review, "fetch_origin_branch", side_effect=lambda b, repo=None: self.tips[b]),
                        mock.patch.object(common, "move_db_companion", return_value=None)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def write_state(self, state):
        (self.folder / "state.json").write_text(json.dumps(state), encoding="utf-8")

    def state(self):
        return json.loads((self.folder / "state.json").read_text(encoding="utf-8"))

    def write_verdict(self, v):
        (self.folder / "verdict.json").write_text(json.dumps(v), encoding="utf-8")

    def go(self, yes=False, reject=True, approve=False, file=None, ticket="100"):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = dev_ticket.cmd_review_verdict(self.api, mock.Mock(
                ticket=ticket, reject=reject, approve=approve, yes=yes, file=file or str(self.verdict_path)))
        line = [ln for ln in out.getvalue().splitlines() if ln.startswith("RESULT: ")][-1]
        return code, json.loads(line[len("RESULT: "):])

    def nothing_written(self):
        self.api.add_comment.assert_not_called()
        self.api.edit_comment.assert_not_called()
        self.api.assign_and_move.assert_not_called()


class RejectTests(VerdictFixture):
    def test_preview_writes_only_the_local_file(self):
        _code, res = self.go()
        self.assertEqual(res["status"], "PREVIEW")
        self.assertEqual(res["author"], "developer1")
        self.assertTrue(any("Dev Queue" in a for a in res["actions"]))
        self.nothing_written()
        self.assertIn("CHANGES REQUIRED", (self.folder / "reviews" / "verdict-round1.md").read_text(encoding="utf-8"))
        self.assertNotIn("verdict", self.state()["review"])

    def test_yes_posts_the_comment_then_reassigns_and_moves_in_one_update(self):
        _code, res = self.go(yes=True)
        self.assertEqual((res["status"], res["comment_id"], res["author"]), ("REJECTED", 777, "developer1"))
        posted = dev_ticket.parse_verdict(self.api.add_comment.call_args.args[1])
        self.assertEqual((posted["verdict"], posted["author"], posted["reviewer"], posted["head"], posted["round"]),
                         ("REJECT", "developer1", "Reviewer1", A, 1))
        self.api.assign_and_move.assert_called_once_with("100", "j1", "Dev Queue")
        review = self.state()["review"]
        self.assertEqual((review["stage"], review["verdict_comment_id"], review["verdict"]["verdict"]),
                         ("done", 777, "REJECT"))
        self.assertEqual((review["verdict"]["round"], review["verdict"]["head"]), (1, A))

    def test_the_comment_carries_the_reproduced_checks_and_the_open_ai_findings(self):
        self.go(yes=True)
        checks = dev_ticket.parse_verdict(self.api.add_comment.call_args.args[1])["checks"]
        self.assertEqual((checks["gates"], checks["risk"], checks["functional"], checks["compare"]),
                         ("PASS", "MEDIUM", "3 passed, 0 failed", "COMPARE_OK"))
        self.assertEqual(checks["ai_review"], {"verdict": "CHANGES REQUIRED", "high": 1, "medium": 0, "low": 1})

    def test_a_later_round_edits_the_same_comment(self):
        state = reviewer_state()
        state["review"]["verdict_comment_id"] = 777
        state["review"]["handoff"]["round"] = 2
        self.write_state(state)
        self.api.get_comments.return_value = [{"id": 777, "user_id": "s1"}]
        _code, res = self.go(yes=True)
        self.assertEqual((res["status"], res["comment_id"]), ("REJECTED", 777))
        self.api.edit_comment.assert_called_once()
        self.api.add_comment.assert_not_called()
        self.assertEqual(dev_ticket.parse_verdict(self.api.edit_comment.call_args.args[2])["round"], 2)

    def test_someone_elses_comment_is_never_edited_and_the_ticket_is_not_moved(self):
        state = reviewer_state()
        state["review"]["verdict_comment_id"] = 777
        self.write_state(state)
        self.api.get_comments.return_value = [{"id": 777, "user_id": "other"}]
        _code, res = self.go(yes=True)
        self.assertEqual(res["status"], "NOT_YOUR_COMMENT")
        self.api.assign_and_move.assert_not_called()

    def test_a_failed_move_keeps_the_comment_id_and_does_not_retry(self):
        from assembla_api import AssemblaError
        self.api.assign_and_move.side_effect = AssemblaError("Update not applied")
        code, res = self.go(yes=True)
        self.assertEqual((code, res["status"], res["comment_id"]), (1, "MOVE_FAILED", 777))
        self.assertEqual(self.api.assign_and_move.call_count, 1)
        review = self.state()["review"]
        self.assertEqual(review["verdict_comment_id"], 777)
        self.assertEqual(review["stage"], "R4-review")  # the review is not closed

    def test_the_db_companion_follows_the_main_ticket(self):
        state = reviewer_state(db={"ticket": 200, "files": ["x.sql"]})
        state["review"]["db_head"] = B
        self.write_state(state)
        with mock.patch.object(common, "move_db_companion",
                               return_value={"db_ticket": 200, "moved": True, "to": "Dev Queue"}) as mover:
            _code, res = self.go(yes=True)
        self.assertEqual(mover.call_args.args[2:], ("Code Review", "Dev Queue"))
        self.assertEqual(res["db_companion"]["moved"], True)

    def test_guards_on_the_caller_and_the_ticket(self):
        with mock.patch.dict(dev_ticket.CONFIG, {"reviewers": ["other"]}):
            self.assertEqual(self.go(yes=True)[1]["status"], "NOT_A_REVIEWER")
        self.assertEqual(self.go(yes=True, ticket="999")[1]["status"], "NO_STATE")
        self.write_state({"ticket": "100", "role": "developer", "stage": "6-build"})
        self.assertEqual(self.go(yes=True)[1]["status"], "NOT_A_REVIEW_FOLDER")
        self.write_state(reviewer_state())
        self.api.get_ticket.return_value = {"status": "Code Review", "assigned_to_id": "s2"}
        self.assertEqual(self.go(yes=True)[1]["status"], "NOT_ASSIGNED")
        self.api.get_ticket.return_value = {"status": "Dev Done", "assigned_to_id": "s1"}
        self.assertEqual(self.go(yes=True)[1]["status"], "WRONG_STATUS")
        state = reviewer_state()
        state["review"]["author"] = "reviewer1"
        self.write_state(state)
        self.api.get_ticket.return_value = {"status": "Code Review", "assigned_to_id": "s1"}
        self.assertEqual(self.go(yes=True)[1]["status"], "SELF_REVIEW")
        self.nothing_written()

    def test_the_verdict_file_must_be_inside_the_ticket_folder_and_valid(self):
        outside = self.root / "elsewhere.json"
        outside.write_text("{}", encoding="utf-8")
        self.assertEqual(self.go(yes=True, file=str(outside))[1]["status"], "FILE_OUTSIDE_TICKET_FOLDER")
        self.assertEqual(self.go(yes=True, file=str(self.folder / "missing.json"))[1]["status"], "NOT_FOUND")
        self.verdict_path.write_text("{not json", encoding="utf-8")
        self.assertEqual(self.go(yes=True)[1]["status"], "VERDICT_INVALID")
        self.write_verdict(good_reject(findings=[]))
        _code, res = self.go(yes=True)
        self.assertEqual(res["status"], "VERDICT_INVALID")
        self.assertTrue(any("at least one finding" in e for e in res["errors"]))
        self.write_verdict(good_approve())
        self.assertEqual(self.go(yes=True)[1]["status"], "VERDICT_MISMATCH")
        self.nothing_written()

    def test_a_branch_that_moved_after_the_fetch_is_stale(self):
        self.tips["FB_100"] = ("OK", B)
        _code, res = self.go(yes=True)
        self.assertEqual((res["status"], res["repo"]), ("STALE_REVIEW", "wf"))
        self.nothing_written()

    def test_a_moved_db_branch_is_stale_too(self):
        state = reviewer_state(db={"ticket": 200, "files": ["x.sql"]})
        state["review"]["db_head"] = B
        self.write_state(state)
        self.tips["FB_200"] = ("OK", A)
        _code, res = self.go(yes=True)
        self.assertEqual((res["status"], res["repo"]), ("STALE_REVIEW", "db"))
        self.nothing_written()

    def test_a_fetch_problem_is_reported(self):
        self.tips["FB_100"] = ("FETCH_FAILED", "offline")
        self.assertEqual(self.go(yes=True)[1]["status"], "FETCH_FAILED")
        self.nothing_written()

    def test_an_author_missing_from_the_space_stops_the_reject(self):
        self.api.get_space_users.return_value = [{"id": "s1", "login": "Reviewer1"}]
        self.assertEqual(self.go(yes=True)[1]["status"], "AUTHOR_NOT_FOUND")
        self.nothing_written()

    def test_the_status_move_must_be_in_the_reviewer_table(self):
        with mock.patch.dict(dev_ticket.CONFIG, {"reviewer_status_moves": []}):
            self.assertEqual(self.go(yes=True)[1]["status"], "NOT_ALLOWED")
        self.nothing_written()

    def test_a_missing_mode_flag_is_refused(self):
        self.assertEqual(self.go(reject=False, approve=False)[1]["status"], "BAD_ARG")


CLEAN_AI_REVIEW = """# Code review - #100 x

## Verdict: PASS

## Findings
| # | Severity | Category | File:line | Evidence | Finding | Suggested fix |
|---|---|---|---|---|---|---|
"""


class ApproveTests(VerdictFixture):
    def setUp(self):
        super().setUp()
        self.write_verdict(good_approve(override_reason="The High finding is a false positive: see the note."))

    def approve(self, yes=False, **kw):
        return self.go(yes=yes, reject=False, approve=True, **kw)

    def clean_ai_review(self):
        (self.folder / "reviews" / "code-review.md").write_text(CLEAN_AI_REVIEW, encoding="utf-8")
        self.write_verdict(good_approve())

    def test_preview_changes_nothing_and_says_the_ticket_stays_with_the_reviewer(self):
        _code, res = self.approve()
        self.assertEqual(res["status"], "PREVIEW")
        self.assertTrue(any("Dev Done" in a and "stays assigned to you" in a for a in res["actions"]))
        self.nothing_written()
        self.api.update_status.assert_not_called()

    def test_yes_posts_the_comment_moves_to_dev_done_and_binds_the_approval_to_the_head(self):
        _code, res = self.approve(yes=True)
        self.assertEqual((res["status"], res["status_now"], res["approved_head"]), ("APPROVED", "Dev Done", A[:12]))
        posted = dev_ticket.parse_verdict(self.api.add_comment.call_args.args[1])
        self.assertEqual((posted["verdict"], posted["head"], posted["tier"]), ("APPROVE", A, "MEDIUM"))
        self.assertIn("false positive", posted["override_reason"])
        self.api.update_status.assert_called_once_with("100", "Dev Done")
        self.api.assign_and_move.assert_not_called()  # the ticket stays with the reviewer, who deploys
        review = self.state()["review"]
        self.assertEqual((review["stage"], review["approved_head"], review["verdict"]["verdict"]), ("R6-deploy", A, "APPROVE"))

    def test_the_db_head_is_recorded_with_the_approval(self):
        state = reviewer_state(db={"ticket": 200, "files": ["x.sql"]})
        state["review"]["db_head"] = B
        self.write_state(state)
        self.approve(yes=True)
        self.assertEqual(self.state()["review"]["approved_db_head"], B)

    def test_an_open_ai_finding_cannot_be_approved_without_an_override_reason(self):
        self.write_verdict(good_approve())
        _code, res = self.approve(yes=True)
        self.assertEqual(res["status"], "VERDICT_INVALID")
        self.assertTrue(any("override_reason" in e for e in res["errors"]))
        self.nothing_written()

    def test_a_clean_ai_review_needs_no_override(self):
        self.clean_ai_review()
        self.assertEqual(self.approve(yes=True)[1]["status"], "APPROVED")

    def test_an_approval_cannot_carry_a_high_or_medium_finding(self):
        self.clean_ai_review()
        bad = good_approve()
        bad["findings"][0]["severity"] = "Medium"
        self.write_verdict(bad)
        self.assertEqual(self.approve(yes=True)[1]["status"], "VERDICT_INVALID")
        self.nothing_written()

    def test_the_compare_must_be_ok(self):
        for compare in ({"status": "COMPARE_MISMATCH"}, {"status": "COMPARE_PARTIAL"}, None):
            state = reviewer_state()
            state["review"]["compare"] = compare
            if compare is None:
                del state["review"]["compare"]
            self.write_state(state)
            self.assertEqual(self.approve(yes=True)[1]["status"], "COMPARE_REQUIRED", compare)
        self.nothing_written()

    def test_the_ai_review_stage_must_be_reached(self):
        state = reviewer_state()
        state["review"]["stage"] = "R3-check"
        self.write_state(state)
        self.assertEqual(self.approve(yes=True)[1]["status"], "AI_REVIEW_REQUIRED")
        self.nothing_written()

    def test_a_reject_needs_neither_the_compare_nor_the_ai_review(self):
        state = reviewer_state()
        state["review"].update(stage="R2-fetch", compare={"status": "COMPARE_MISMATCH"})
        self.write_state(state)
        self.write_verdict(good_reject())
        self.assertEqual(self.go(yes=True)[1]["status"], "REJECTED")

    def test_a_branch_that_moved_after_the_fetch_voids_the_approval(self):
        self.tips["FB_100"] = ("OK", B)
        self.assertEqual(self.approve(yes=True)[1]["status"], "STALE_REVIEW")
        self.nothing_written()
        self.api.update_status.assert_not_called()

    def test_a_failed_move_keeps_the_comment_and_records_no_approval(self):
        from assembla_api import AssemblaError
        self.api.update_status.side_effect = AssemblaError("Status update not applied")
        code, res = self.approve(yes=True)
        self.assertEqual((code, res["status"], res["comment_id"]), (1, "MOVE_FAILED", 777))
        self.assertEqual(self.api.update_status.call_count, 1)
        review = self.state()["review"]
        self.assertEqual((review["stage"], review.get("approved_head"), review["verdict_comment_id"]), ("R4-review", None, 777))

    def test_the_db_companion_follows_to_dev_done(self):
        state = reviewer_state(db={"ticket": 200, "files": ["x.sql"]})
        state["review"]["db_head"] = B
        self.write_state(state)
        with mock.patch.object(common, "move_db_companion",
                               return_value={"db_ticket": 200, "moved": True, "to": "Dev Done"}) as mover:
            _code, res = self.approve(yes=True)
        self.assertEqual(mover.call_args.args[2:], ("Code Review", "Dev Done"))
        self.assertTrue(res["db_companion"]["moved"])

    def test_the_move_must_be_in_the_reviewer_table(self):
        with mock.patch.dict(dev_ticket.CONFIG, {"reviewer_status_moves": [["Code Review", "Dev Queue"]]}):
            self.assertEqual(self.approve(yes=True)[1]["status"], "NOT_ALLOWED")
        self.nothing_written()

    def test_a_later_round_edits_the_same_comment(self):
        state = reviewer_state()
        state["review"]["verdict_comment_id"] = 777
        self.write_state(state)
        self.api.get_comments.return_value = [{"id": 777, "user_id": "s1"}]
        _code, res = self.approve(yes=True)
        self.assertEqual((res["status"], res["comment_id"]), ("APPROVED", 777))
        self.api.add_comment.assert_not_called()

    def test_the_guards_are_the_same_as_for_a_reject(self):
        state = reviewer_state()
        state["review"]["author"] = "reviewer1"
        self.write_state(state)
        self.assertEqual(self.approve(yes=True)[1]["status"], "SELF_REVIEW")
        self.write_state(reviewer_state())
        self.api.get_ticket.return_value = {"status": "Dev Done", "assigned_to_id": "s1"}
        self.assertEqual(self.approve(yes=True)[1]["status"], "WRONG_STATUS")
        self.write_verdict(good_reject())
        self.api.get_ticket.return_value = {"status": "Code Review", "assigned_to_id": "s1"}
        self.assertEqual(self.approve(yes=True)[1]["status"], "VERDICT_MISMATCH")
        self.nothing_written()


if __name__ == "__main__":
    unittest.main()
