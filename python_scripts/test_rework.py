"""Phase 3 of the reviewer review (REVIEW_TICKET_PHASE3_PLAN.md), task 5: the developer's `dt rework`. Assembla is mocked and
only read from; the local state is a temp folder."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import dev_ticket
from develop_ticket import common, review

A, B = "a" * 40, "b" * 40
REJECT = {"verdict": "REJECT", "summary": "The save handler loses the node id on a rename.",
          "findings": [{"severity": "High", "file_line": "c2mwf_admin/src/app/a.ts:42",
                        "finding": "The rename branch returns before the id is copied.",
                        "fix": "Copy the id first, then return."}]}
CHECKS = {"gates": "PASS", "risk": "MEDIUM", "functional": "3 passed, 0 failed", "compare": "COMPARE_OK",
          "ai_review": {"verdict": "PASS", "high": 0, "medium": 0, "low": 0}}


def verdict_comment(cid=9, user="s1", created="2026-10-05", verdict=None, **over):
    kwargs = dict(ticket="100", reviewer="reviewer1", author="developer1", head=A, round=1, tier="MEDIUM", checks=CHECKS)
    kwargs.update(over)
    payload = dev_ticket.verdict_payload(verdict or REJECT, **kwargs)
    return {"id": cid, "user_id": user, "created_on": created, "comment": dev_ticket.render_verdict(payload)}


def author_state(**over):
    state = {"ticket": "100", "role": "developer", "stage": "done", "branch": "FB_100",
             "handoff": {"comment_id": 555, "head": A, "round": 1, "author": "developer1", "reviewer": "reviewer1",
                         "assigned_at": "2026-10-04", "at": "2026-10-04"}}
    state.update(over)
    return state


class ReworkTests(unittest.TestCase):
    READ_ONLY = {"get_current_user", "get_ticket", "get_comments", "get_user", "user_label"}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.folder = self.root / "DEV_TICKETS" / "100"
        self.folder.mkdir(parents=True)
        self.write_state(author_state())
        self.users = {"j1": "developer1", "s1": "reviewer1", "s2": "reviewer2"}
        self.api = mock.Mock()
        self.api.get_current_user.return_value = {"id": "j1", "login": "developer1"}
        self.api.get_ticket.return_value = {"status": "Dev Queue", "assigned_to_id": "j1"}
        self.api.get_user.side_effect = lambda uid: {"id": uid, "login": self.users.get(uid)}
        self.api.user_label.side_effect = lambda uid: self.users.get(uid, str(uid))
        self.comments = [verdict_comment()]
        self.api.get_comments.side_effect = lambda n: list(self.comments)
        for patcher in (mock.patch.object(common, "PROJECT_ROOT", self.root),
                        mock.patch.dict(dev_ticket.CONFIG, {"reviewers": ["reviewer1", "reviewer2"]})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def write_state(self, state):
        (self.folder / "state.json").write_text(json.dumps(state), encoding="utf-8")

    def state(self):
        return json.loads((self.folder / "state.json").read_text(encoding="utf-8"))

    def go(self, yes=False, ticket="100"):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = dev_ticket.cmd_rework(self.api, mock.Mock(ticket=ticket, yes=yes))
        line = [ln for ln in out.getvalue().splitlines() if ln.startswith("RESULT: ")][-1]
        return code, json.loads(line[len("RESULT: "):])

    def test_preview_shows_the_findings_and_changes_nothing(self):
        _code, res = self.go()
        self.assertEqual((res["status"], res["round"], res["next_round"], res["reviewer"]), ("PREVIEW", 1, 2, "reviewer1"))
        self.assertEqual(res["findings"][0]["fix"], "Copy the id first, then return.")
        self.assertEqual(self.state(), author_state())
        self.assertFalse((self.folder / "reviews").exists())

    def test_yes_saves_the_findings_and_reopens_at_the_build_step(self):
        _code, res = self.go(yes=True)
        self.assertEqual(res["status"], "REWORK_READY")
        self.assertIn("CHANGES REQUIRED", (self.folder / "reviews" / "review-feedback-round1.md").read_text(encoding="utf-8"))
        state = self.state()
        self.assertEqual((state["stage"], state["round"]), ("6-build", 2))
        self.assertEqual((state["rework"]["round"], state["rework"]["reviewer"], state["rework"]["findings"]),
                         (1, "reviewer1", 1))
        self.assertEqual((state["handoff"]["comment_id"], state["handoff"]["head"]), (555, A))  # the next hand-off edits in place
        self.assertNotIn("reviewer", state["handoff"])
        self.assertNotIn("assigned_at", state["handoff"])

    def test_the_next_steps_start_with_the_status_move_only_from_dev_queue(self):
        _code, res = self.go()
        self.assertEqual(res["steps"][0], 'dt status 100 --to "Dev In Process" --yes')
        self.api.get_ticket.return_value = {"status": "Dev In Process", "assigned_to_id": "j1"}
        _code, res = self.go()
        self.assertEqual(res["steps"][0], "settle the plan with the developer")

    def test_a_second_run_after_reopening_is_reported_not_repeated(self):
        self.go(yes=True)
        before = self.state()
        _code, res = self.go(yes=True)
        self.assertEqual(res["status"], "ALREADY_REOPENED")
        self.assertEqual(self.state(), before)

    def test_nothing_to_rework_when_the_ticket_is_not_back_with_me(self):
        self.api.get_ticket.return_value = {"status": "Code Review", "assigned_to_id": "s1"}
        self.assertEqual(self.go(yes=True)[1]["status"], "NO_REJECTION")
        self.api.get_ticket.return_value = {"status": "Dev Done", "assigned_to_id": "j1"}
        self.assertEqual(self.go(yes=True)[1]["status"], "NO_REJECTION")
        self.assertEqual(self.state()["stage"], "done")

    def test_nothing_to_rework_without_a_reject_verdict(self):
        self.comments = []
        self.assertEqual(self.go(yes=True)[1]["status"], "NO_REJECTION")
        approve = dict(REJECT, verdict="APPROVE", findings=[])
        self.comments = [verdict_comment(verdict=approve)]
        self.assertEqual(self.go(yes=True)[1]["status"], "NO_REJECTION")

    def test_the_newest_verdict_wins(self):
        approve = dict(REJECT, verdict="APPROVE", findings=[])
        self.comments = [verdict_comment(cid=8, created="2026-10-05"), verdict_comment(cid=9, created="2026-10-06", verdict=approve)]
        self.assertEqual(self.go(yes=True)[1]["status"], "NO_REJECTION")

    def test_a_verdict_for_a_documentation_only_move_of_the_handoff_is_taken_up(self):
        self.comments = [verdict_comment(head=B)]
        with mock.patch.object(review, "docs_only_delta", return_value=True):
            self.assertEqual(self.go()[1]["status"], "PREVIEW")

    def test_a_verdict_for_other_code_is_not_taken_up(self):
        self.comments = [verdict_comment(head=B)]
        self.assertEqual(self.go(yes=True)[1]["status"], "VERDICT_NOT_FOR_THIS_HANDOFF")
        self.comments = [verdict_comment(round=2)]
        self.assertEqual(self.go(yes=True)[1]["status"], "VERDICT_NOT_FOR_THIS_HANDOFF")
        self.assertEqual(self.state()["stage"], "done")

    def test_a_verdict_not_written_by_the_assigned_reviewer_is_not_trusted(self):
        self.comments = [verdict_comment(user="s2")]  # reviewer2 wrote it, but it claims reviewer1 and reviewer1 was assigned
        self.assertEqual(self.go(yes=True)[1]["status"], "REVIEW_AUTHOR_MISMATCH")
        self.comments = [verdict_comment(user="s2", reviewer="reviewer2")]  # honest, but not the assigned reviewer
        self.assertEqual(self.go(yes=True)[1]["status"], "REVIEW_AUTHOR_MISMATCH")
        self.users["j1"] = "developer1"
        self.comments = [verdict_comment(user="j1", reviewer="developer1")]  # a developer posting a fake verdict
        self.assertEqual(self.go(yes=True)[1]["status"], "REVIEW_AUTHOR_MISMATCH")
        self.assertEqual(self.state()["stage"], "done")

    def test_without_a_recorded_reviewer_a_configured_reviewer_is_accepted_and_anyone_else_is_not(self):
        state = author_state()
        del state["handoff"]["reviewer"]
        self.write_state(state)
        self.assertEqual(self.go()[1]["status"], "PREVIEW")
        self.comments = [verdict_comment(user="j1", reviewer="developer1")]
        self.assertEqual(self.go()[1]["status"], "REVIEW_AUTHOR_MISMATCH")

    def test_only_an_authors_handed_off_folder_is_reworked(self):
        self.assertEqual(self.go(ticket="999")[1]["status"], "NO_STATE")
        self.write_state({"ticket": "100", "role": "reviewer", "kind": "review", "stage": "done"})
        self.assertEqual(self.go()[1]["status"], "NOT_AUTHOR_FOLDER")
        self.write_state({"ticket": "100", "role": "developer", "stage": "done"})
        self.assertEqual(self.go()[1]["status"], "NOT_HANDED_OFF")
        self.write_state(author_state(stage="6-build"))
        self.assertEqual(self.go()[1]["status"], "NOT_HANDED_OFF")

    def test_a_stage_8_handoff_folder_is_accepted(self):
        self.write_state(author_state(stage="8-handoff"))
        self.assertEqual(self.go()[1]["status"], "PREVIEW")

    def test_nothing_is_written_to_assembla(self):
        self.go(yes=True)
        called = {c[0] for c in self.api.method_calls}
        self.assertTrue(called <= self.READ_ONLY, called - self.READ_ONLY)


class LatestVerdictTests(unittest.TestCase):
    def test_none_without_a_valid_block_and_the_writer_is_resolved(self):
        api = mock.Mock()
        api.get_user.side_effect = lambda uid: {"id": uid, "login": {"s1": "reviewer1"}.get(uid)}
        api.get_comments.return_value = [{"id": 1, "user_id": "s1", "created_on": "x", "comment": "a note"},
                                         {"id": 2, "user_id": "s1", "created_on": "y",
                                          "comment": "VERDICT-JSON-BEGIN\n{nope\nVERDICT-JSON-END"}]
        self.assertIsNone(dev_ticket.latest_verdict(api, "100"))
        api.get_comments.return_value.append(verdict_comment(cid=3, user="s1"))
        found = dev_ticket.latest_verdict(api, "100")
        self.assertEqual((found["comment_id"], found["writer_login"], found["payload"]["verdict"]), (3, "reviewer1", "REJECT"))


if __name__ == "__main__":
    unittest.main()
