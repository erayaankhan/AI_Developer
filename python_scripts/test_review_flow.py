"""Phase 2 of the reviewer review (REVIEW_TICKET_PHASE2_PLAN.md): the reviewer's state keys and the HANDOFF lookup.
No Assembla or git call is made."""
import argparse
import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import dev_ticket
from develop_ticket import common, protocol, review, ticket_fetch


def run(handler, **kwargs):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = handler(None, mock.Mock(**kwargs))
    line = [ln for ln in out.getvalue().splitlines() if ln.startswith("RESULT: ")][-1]
    return code, json.loads(line[len("RESULT: "):])


class ReviewStateKeyTests(unittest.TestCase):
    def test_review_stages_are_configured_in_order(self):
        self.assertEqual(dev_ticket.CONFIG["review_stages"],
                         ["R1-pick", "R2-fetch", "R3-check", "R4-review", "R5-verdict", "R6-deploy", "done"])

    def test_review_stage_is_validated(self):
        check = dev_ticket.STATE_FIELDS["review.stage"]
        self.assertTrue(check("R3-check"))
        self.assertFalse(check("6-build"))
        self.assertFalse(check(""))

    def test_the_review_folder_marker_cannot_be_set_by_hand(self):
        # only `review-fetch` writes kind=review; `dt state --set` knows the role (developer|reviewer) but not the kind
        self.assertNotIn("kind", dev_ticket.STATE_FIELDS)
        self.assertTrue(dev_ticket.STATE_FIELDS["role"]("developer"))
        self.assertTrue(dev_ticket.STATE_FIELDS["role"]("reviewer"))
        self.assertFalse(dev_ticket.STATE_FIELDS["role"]("manager"))

    def test_dt_state_sets_the_review_stage_and_leaves_the_author_stage_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / "DEV_TICKETS" / "100"
            folder.mkdir(parents=True)
            (folder / "state.json").write_text(json.dumps({"ticket": "100", "role": "reviewer", "kind": "review", "stage": "done"}),
                                               encoding="utf-8")
            with mock.patch.object(common, "PROJECT_ROOT", root):
                code, res = run(dev_ticket.cmd_state, ticket="100", set=["review.stage=R2-fetch"])
                self.assertEqual((code, res["status"]), (0, "UPDATED"))
                code, res = run(dev_ticket.cmd_state, ticket="100", set=["review.stage=nonsense"])
                self.assertEqual((code, res["status"]), (1, "BAD_VALUE"))
            saved = json.loads((folder / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["review"], {"stage": "R2-fetch"})
            self.assertEqual(saved["stage"], "done")


def payload(author="developer1", head="a" * 40):
    return {"ticket": "100", "author": author, "branch": "FB_100", "head": head, "round": 1, "risk": "MEDIUM",
            "path": "STANDARD", "risk_check": {"min_tier": "MEDIUM", "reasons": []},
            "gates": {"result": "PASS", "at": "t", "files_count": 1, "files": ["a.ts"]},
            "functional": {"mode": "run", "profile": "staging", "passed": 1, "failed": 0, "skip_reason": None},
            "db": None, "baseline": "PASS", "readme_impact": "none"}


def handoff_comment(cid, user_id, created, author="developer1", head="a" * 40):
    return {"id": cid, "user_id": user_id, "created_on": created,
            "comment": dev_ticket.render_handoff(payload(author, head))}


class LatestHandoffTests(unittest.TestCase):
    def api(self, comments, users=None):
        api = mock.Mock()
        api.get_comments.return_value = comments
        logins = users or {"u1": "developer1", "u2": "other"}
        api.get_user.side_effect = lambda uid: {"id": uid, "login": logins.get(uid)}
        return api

    def test_none_when_the_ticket_has_no_handoff(self):
        api = self.api([{"id": 1, "user_id": "u1", "created_on": "2026-10-01", "comment": "just a note"}])
        self.assertIsNone(dev_ticket.latest_handoff(api, "100"))

    def test_finds_the_block_and_the_writer(self):
        api = self.api([{"id": 1, "user_id": "u2", "created_on": "2026-10-01", "comment": "note"},
                        handoff_comment(7, "u1", "2026-10-02")])
        found = dev_ticket.latest_handoff(api, "100")
        self.assertEqual((found["comment_id"], found["writer_login"], found["authentic"], found["ambiguous"]),
                         (7, "developer1", True, False))
        self.assertEqual(found["payload"]["head"], "a" * 40)

    def test_the_newest_valid_block_wins_and_an_invalid_one_is_ignored(self):
        broken = {"id": 9, "user_id": "u1", "created_on": "2026-10-04",
                  "comment": "HANDOFF-JSON-BEGIN\n{not json\nHANDOFF-JSON-END"}
        api = self.api([handoff_comment(5, "u1", "2026-10-02", head="b" * 40),
                        handoff_comment(7, "u1", "2026-10-03", head="c" * 40), broken])
        found = dev_ticket.latest_handoff(api, "100")
        self.assertEqual((found["comment_id"], found["payload"]["head"]), (7, "c" * 40))
        self.assertFalse(found["ambiguous"])  # the same writer twice is not ambiguous

    def test_a_claimed_author_that_is_not_the_writer_is_not_authentic(self):
        api = self.api([handoff_comment(7, "u2", "2026-10-02", author="developer1")])
        found = dev_ticket.latest_handoff(api, "100")
        self.assertEqual(found["writer_login"], "other")
        self.assertFalse(found["authentic"])

    def test_login_comparison_ignores_case(self):
        api = self.api([handoff_comment(7, "u1", "2026-10-02", author="DEVELOPER1")])
        self.assertTrue(dev_ticket.latest_handoff(api, "100")["authentic"])

    def test_two_different_writers_make_it_ambiguous(self):
        api = self.api([handoff_comment(5, "u1", "2026-10-02"),
                        handoff_comment(7, "u2", "2026-10-03", author="other")])
        found = dev_ticket.latest_handoff(api, "100")
        self.assertTrue(found["ambiguous"])
        self.assertEqual(found["writer_login"], "other")  # the newest, but the caller must ask

    @staticmethod
    def verdict_comment(cid, kind, created="2026-10-03"):
        body = json.dumps({"verdict": kind, "round": 1, "head": "a" * 40, "reviewer": "lead"})
        return {"id": cid, "user_id": "u3", "created_on": created,
                "comment": f"REVIEW_VERDICT\n{dev_ticket.VERDICT_BEGIN}\n{body}\n{dev_ticket.VERDICT_END}"}

    def test_a_takeover_after_a_reject_is_not_ambiguous(self):
        # developer1 hands off, the reviewer rejects, `other` takes the ticket over and hands off: the old hand-off is
        # superseded (kept as the record) and does not block the review
        api = self.api([handoff_comment(5, "u1", "2026-10-02"), self.verdict_comment(6, "REJECT"),
                        handoff_comment(7, "u2", "2026-10-04", author="other", head="b" * 40)])
        found = dev_ticket.latest_handoff(api, "100")
        self.assertEqual((found["comment_id"], found["writer_login"], found["authentic"], found["ambiguous"]),
                         (7, "other", True, False))

    def test_a_takeover_after_a_quality_bounce_is_not_ambiguous(self):
        bounce = {"id": 6, "user_id": "u3", "created_on": "2026-10-03",
                  "comment": f"QUALITY-BOUNCE\n{protocol.BOUNCE_BEGIN}\n{json.dumps({'ticket': '100'})}\n{protocol.BOUNCE_END}"}
        api = self.api([handoff_comment(5, "u1", "2026-10-02"), bounce,
                        handoff_comment(7, "u2", "2026-10-04", author="other")])
        found = dev_ticket.latest_handoff(api, "100")
        self.assertEqual((found["comment_id"], found["ambiguous"]), (7, False))

    def test_two_writers_after_the_last_reject_are_still_ambiguous(self):
        api = self.api([handoff_comment(4, "u1", "2026-10-01"), self.verdict_comment(5, "REJECT", "2026-10-02"),
                        handoff_comment(6, "u1", "2026-10-03"), handoff_comment(7, "u2", "2026-10-04", author="other")])
        found = dev_ticket.latest_handoff(api, "100")
        self.assertEqual((found["comment_id"], found["ambiguous"]), (7, True))

    def test_an_approve_verdict_does_not_close_the_round(self):
        api = self.api([handoff_comment(5, "u1", "2026-10-02"), self.verdict_comment(6, "APPROVE"),
                        handoff_comment(7, "u2", "2026-10-04", author="other")])
        self.assertTrue(dev_ticket.latest_handoff(api, "100")["ambiguous"])

    def test_a_handoff_edited_in_place_after_a_reject_is_current(self):
        # the normal rework: `dt handoff` edits the author's own comment X for round 2 (X was created before the REJECT)
        x = dict(handoff_comment(5, "u1", "2026-10-02T10:00:00Z", head="b" * 40), updated_at="2026-10-04T10:00:00Z")
        v = dict(self.verdict_comment(6, "REJECT", "2026-10-03T10:00:00Z"), updated_at="2026-10-03T10:00:00Z")
        found = dev_ticket.latest_handoff(self.api([x, v]), "100")
        self.assertEqual((found["comment_id"], found["payload"]["head"], found["ambiguous"]), (5, "b" * 40, False))

    def test_a_round_two_reject_edited_in_place_closes_the_round_again(self):
        x = dict(handoff_comment(5, "u1", "2026-10-02T10:00:00Z"), updated_at="2026-10-04T10:00:00Z")
        v = dict(self.verdict_comment(6, "REJECT", "2026-10-03T10:00:00Z"), updated_at="2026-10-05T10:00:00Z")
        self.assertIsNone(dev_ticket.latest_handoff(self.api([x, v]), "100"))

    def test_a_takeover_with_updated_at_is_not_ambiguous(self):
        # ticket 4242: A's X (edited once in round 1), REJECT V, B's new comment Y
        x = dict(handoff_comment(5, "u1", "2026-10-02T10:00:00Z"), updated_at="2026-10-02T11:00:00Z")
        v = dict(self.verdict_comment(6, "REJECT", "2026-10-03T10:00:00Z"), updated_at="2026-10-03T10:00:00Z")
        y = dict(handoff_comment(7, "u2", "2026-10-04T10:00:00Z", author="other"), updated_at="2026-10-04T10:00:00Z")
        found = dev_ticket.latest_handoff(self.api([x, v, y]), "100")
        self.assertEqual((found["comment_id"], found["writer_login"], found["ambiguous"]), (7, "other", False))

    def test_no_handoff_after_the_last_reject_is_none(self):
        api = self.api([handoff_comment(5, "u1", "2026-10-02"), self.verdict_comment(6, "REJECT")])
        self.assertIsNone(dev_ticket.latest_handoff(api, "100"))


def ticket(number, status="Code Review", assignee="s1", milestone=10, level=2, db=False, summary=None):
    return {"number": number, "id": f"id{number}", "summary": summary or f"Ticket {number}", "status": status,
            "assigned_to_id": assignee, "milestone_id": milestone, "hierarchy_type": level, "_db_companion": db}


class ReviewPickTests(unittest.TestCase):
    READ_ONLY = {"get_current_user", "get_current_milestones", "get_my_active_tickets", "get_milestone_tickets",
                 "get_comments", "get_user", "user_label", "get_ticket", "get_milestone"}

    def setUp(self):
        self.api = mock.Mock()
        self.api.get_current_user.return_value = {"id": "s1", "login": "Reviewer1"}
        self.api.get_current_milestones.return_value = [{"id": 10, "title": "Release_CURRENT"}]
        self.api.get_milestone.return_value = {"title": "Old release"}
        self.users = {"s1": "Reviewer1", "j1": "developer1", "j2": "developer2"}
        self.api.get_user.side_effect = lambda uid: {"id": uid, "login": self.users.get(uid)}
        self.api.user_label.side_effect = lambda uid: self.users.get(uid, str(uid))
        self.mine = [ticket(200), ticket(100)]
        self.comments = {"100": [self.handoff(1, "j1", "developer1", "a" * 40)],
                         "200": [self.handoff(2, "j2", "developer2", "b" * 40, risk="HIGH")]}
        self.api.get_my_active_tickets.side_effect = lambda: list(self.mine)
        self.api.get_comments.side_effect = lambda n: self.comments.get(str(n), [])
        self.api.get_ticket.side_effect = lambda n: next(t for t in self.mine if str(t["number"]) == str(n))
        for patcher in (mock.patch.dict(dev_ticket.CONFIG, {"reviewers": ["reviewer1", "reviewer2"]}),
                        mock.patch.object(common, "_remembered_role", return_value="reviewer"),
                        mock.patch.object(common, "is_db_companion",
                                          side_effect=lambda _api, t: t.get("_db_companion", False))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def handoff(self, cid, user_id, author, head, risk="MEDIUM"):
        p = payload(author, head)
        p["risk"], p["round"] = risk, 1
        return {"id": cid, "user_id": user_id, "created_on": f"2026-10-0{cid}", "comment": dev_ticket.render_handoff(p)}

    def go(self, ticket_number=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = dev_ticket.cmd_review_pick(self.api, mock.Mock(ticket=ticket_number))
        line = [ln for ln in out.getvalue().splitlines() if ln.startswith("RESULT: ")][-1]
        return code, json.loads(line[len("RESULT: "):])

    def test_several_reviewable_tickets_are_listed_lowest_first(self):
        _code, res = self.go()
        self.assertEqual(res["status"], "MULTIPLE")
        self.assertEqual([c["ticket"] for c in res["candidates"]], [100, 200])
        self.assertEqual(res["suggested"], 100)
        first = res["candidates"][0]
        self.assertEqual((first["author"], first["risk"], first["head"], first["round"], first["authentic"]),
                         ("developer1", "MEDIUM", "a" * 12, 1, True))

    def test_one_reviewable_ticket_is_picked(self):
        self.mine = [ticket(100)]
        _code, res = self.go()
        self.assertEqual((res["status"], res["ticket"], res["author"], res["reviewer"]),
                         ("PICKED", 100, "developer1", "Reviewer1"))

    def test_a_ticket_without_a_handoff_is_not_reviewable_but_is_reported(self):
        self.comments["100"] = [{"id": 1, "user_id": "j1", "created_on": "2026-10-01", "comment": "note"}]
        _code, res = self.go()
        self.assertEqual((res["status"], res["ticket"]), ("PICKED", 200))
        self.assertEqual([r["ticket"] for r in res["not_handed_off"]], [100])

    def test_none_lists_not_handed_off_and_tickets_assigned_to_others_for_information(self):
        self.mine = [ticket(100)]
        self.comments = {}
        self.api.get_milestone_tickets.return_value = [ticket(100), ticket(300, assignee="s2"),
                                                       ticket(301, status="Dev Done", assignee="s2")]
        _code, res = self.go()
        self.assertEqual(res["status"], "NONE")
        self.assertEqual([r["ticket"] for r in res["not_handed_off"]], [100])
        self.assertEqual([r["ticket"] for r in res["assigned_to_others"]], [300])  # information only

    def test_db_companions_other_milestones_and_other_statuses_are_left_out(self):
        self.mine = [ticket(100), ticket(101, db=True), ticket(102, milestone=99), ticket(103, status="Dev In Process")]
        self.comments = {str(n): [self.handoff(1, "j1", "developer1", "a" * 40)] for n in (100, 101, 102, 103)}
        _code, res = self.go()
        self.assertEqual((res["status"], res["ticket"]), ("PICKED", 100))

    def test_a_non_story_ticket_is_flagged_not_hidden(self):
        self.mine = [ticket(100, level=0)]
        _code, res = self.go()
        self.assertEqual((res["status"], res["story"]), ("PICKED", False))

    def test_ambiguous_and_unauthentic_handoffs_are_flagged(self):
        self.mine = [ticket(100)]
        self.comments["100"] = [self.handoff(1, "j1", "developer1", "a" * 40),
                                self.handoff(2, "j2", "developer1", "a" * 40)]  # developer2 claims developer1's authorship
        _code, res = self.go()
        self.assertEqual((res["ambiguous"], res["authentic"]), (True, False))

    def test_the_caller_must_be_a_configured_reviewer(self):
        with mock.patch.dict(dev_ticket.CONFIG, {"reviewers": ["reviewer2"]}):
            _code, res = self.go()
        self.assertEqual(res["status"], "NOT_A_REVIEWER")
        with mock.patch.dict(dev_ticket.CONFIG, {"reviewers": []}):
            _code, res = self.go()
        self.assertEqual(res["status"], "REVIEWERS_NOT_CONFIGURED")

    def test_a_developer_role_is_refused(self):
        with mock.patch.object(common, "_remembered_role", return_value="developer"):
            _code, res = self.go()
        self.assertEqual(res["status"], "NOT_A_REVIEWER")

    def test_no_current_milestone(self):
        self.api.get_current_milestones.return_value = []
        _code, res = self.go()
        self.assertEqual(res["status"], "NO_CURRENT_MILESTONE")

    def test_named_ticket_checks(self):
        self.mine = [ticket(100), ticket(101, assignee="s2"), ticket(102, status="Dev Done"),
                     ticket(103, milestone=99), ticket(104)]
        self.comments["104"] = []
        for number, expected in ((101, "NOT_ASSIGNED"), (102, "WRONG_STATUS"), (103, "WRONG_MILESTONE"),
                                 (104, "NO_HANDOFF"), (100, "PICKED")):
            _code, res = self.go(str(number))
            self.assertEqual(res["status"], expected, number)

    def test_nothing_is_written_to_assembla(self):
        self.go()
        self.go("100")
        called = {c[0] for c in self.api.method_calls}
        self.assertTrue(called <= self.READ_ONLY, called - self.READ_ONLY)


def proc(stdout="", returncode=0, stderr=""):
    return mock.Mock(stdout=stdout, returncode=returncode, stderr=stderr)


class FakeGit:
    """A scriptable stand-in for dev_ticket.git: `rules` maps an args prefix to a proc (first match wins)."""

    def __init__(self, rules):
        self.rules, self.calls = rules, []

    def __call__(self, *args, check=True, timeout=120, repo=None):
        self.calls.append((args, repo))
        for prefix, answer in self.rules:
            if args[:len(prefix)] == prefix:
                return answer(args) if callable(answer) and not isinstance(answer, mock.Mock) else answer
        return proc()

    def ran(self, *prefix):
        return any(args[:len(prefix)] == prefix for args, _repo in self.calls)


A, B = "a" * 40, "b" * 40


class CheckoutTests(unittest.TestCase):
    """checkout_remote_branch: the developer's branch is switched to or fast-forwarded, never reset."""

    def run_case(self, current, local, remote, ancestor=True, head=None):
        rules = [(("branch", "--show-current"), proc(current + "\n")),
                 (("rev-parse", "--verify", "--quiet", "refs/remotes/origin/FB_100"), proc(remote + "\n" if remote else "", 0 if remote else 1)),
                 (("rev-parse", "--verify", "--quiet", "refs/heads/FB_100"), proc(local + "\n" if local else "", 0 if local else 1)),
                 (("merge-base", "--is-ancestor"), proc("", 0 if ancestor else 1)),
                 (("rev-parse", "HEAD"), proc((head or remote or "") + "\n"))]
        fake = FakeGit(rules)
        with mock.patch.object(common, "git", fake):
            return dev_ticket.checkout_remote_branch("FB_100"), fake

    def test_no_local_branch_tracks_the_remote_one(self):
        (status, info), fake = self.run_case("master", None, A)
        self.assertEqual((status, info["head"]), ("OK", A))
        self.assertTrue(fake.ran("switch", "--track", "origin/FB_100"))

    def test_behind_origin_is_fast_forwarded(self):
        (status, _info), fake = self.run_case("master", B, A, ancestor=True)
        self.assertEqual(status, "OK")
        self.assertTrue(fake.ran("switch", "FB_100"))
        self.assertTrue(fake.ran("merge", "--ff-only", "origin/FB_100"))

    def test_already_on_the_branch_and_equal_does_nothing_but_verify(self):
        (status, _info), fake = self.run_case("FB_100", A, A)
        self.assertEqual(status, "OK")
        self.assertFalse(fake.ran("switch"))
        self.assertFalse(fake.ran("merge"))

    def test_local_commits_origin_lacks_are_never_reset(self):
        (status, info), fake = self.run_case("master", B, A, ancestor=False)
        self.assertEqual((status, info["local"], info["origin"]), ("LOCAL_BRANCH_DIFFERS", B[:12], A[:12]))
        self.assertFalse(fake.ran("switch"))
        self.assertFalse(fake.ran("reset"))
        self.assertFalse(fake.ran("merge"))

    def test_a_wrong_head_after_the_switch_is_reported(self):
        (status, _info), _fake = self.run_case("master", None, A, head=B)
        self.assertEqual(status, "CHECKOUT_FAILED")

    def test_missing_origin_branch(self):
        (status, _info), _fake = self.run_case("master", None, None)
        self.assertEqual(status, "BRANCH_NOT_ON_ORIGIN")

    def test_a_git_error_is_reported_not_raised(self):
        def boom(*args, **kwargs):
            raise RuntimeError("git switch failed: x")
        with mock.patch.object(common, "git", boom):
            status, info = dev_ticket.checkout_remote_branch("FB_100")
        self.assertEqual(status, "CHECKOUT_FAILED")
        self.assertIn("message", info)


class FetchOriginTests(unittest.TestCase):
    def test_sha_after_a_fetch(self):
        fake = FakeGit([(("fetch",), proc()), (("rev-parse", "--verify", "--quiet", "refs/remotes/origin/FB_100"), proc(A + "\n"))])
        with mock.patch.object(common, "git", fake):
            self.assertEqual(dev_ticket.fetch_origin_branch("FB_100"), ("OK", A))
        self.assertTrue(fake.ran("fetch", "--quiet", "origin", "refs/heads/FB_100:refs/remotes/origin/FB_100"))

    def test_a_missing_remote_branch_is_its_own_status(self):
        fake = FakeGit([(("fetch",), proc("", 128, "fatal: couldn't find remote ref refs/heads/FB_100"))])
        with mock.patch.object(common, "git", fake):
            self.assertEqual(dev_ticket.fetch_origin_branch("FB_100"), ("BRANCH_NOT_ON_ORIGIN", None))

    def test_other_fetch_failures_are_not_mistaken_for_a_missing_branch(self):
        fake = FakeGit([(("fetch",), proc("", 128, "fatal: unable to access: could not resolve host"))])
        with mock.patch.object(common, "git", fake):
            status, info = dev_ticket.fetch_origin_branch("FB_100")
        self.assertEqual(status, "FETCH_FAILED")
        self.assertIn("resolve host", info)


class ReviewFetchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "db_repo").mkdir()
        self.folder = self.root / "DEV_TICKETS" / "100"
        self.api = mock.Mock()
        self.api.get_current_user.return_value = {"id": "s1", "login": "Reviewer1"}
        self.api.get_ticket.return_value = ticket(100)
        self.users = {"s1": "Reviewer1", "j1": "developer1"}
        self.api.get_user.side_effect = lambda uid: {"id": uid, "login": self.users.get(uid)}
        self.api.user_label.side_effect = lambda uid: self.users.get(uid, str(uid))
        self.summary = {"id": 3, "user_id": "j1", "created_on": "2026-10-03",
                        "comment": "*Ticket #100: Fix the save button*\n*Impact Analysis Report*\n* x\n*Approach Taken*\n* y\n"}
        self.review = {"id": 4, "user_id": "j1", "created_on": "2026-10-03", "comment": "# Code review — #100 x\n## Verdict: PASS\n"}
        self.api.get_comments.side_effect = lambda n: [self.handoff_comment(), self.summary, self.review]
        self.checkouts = []
        self.fetches = {}
        patches = {
            "root": mock.patch.object(common, "PROJECT_ROOT", self.root),
            "db": mock.patch.object(common, "DB_REPO", self.root / "db_repo"),
            "cfg": mock.patch.dict(dev_ticket.CONFIG, {"reviewers": ["reviewer1"]}),
            "role": mock.patch.object(common, "_remembered_role", return_value="reviewer"),
            "dirty": mock.patch.object(common, "start_dirty_files", return_value=[]),
            "dbdirty": mock.patch.object(review, "dirty_files_in", return_value=[]),
            "fetch_origin": mock.patch.object(review, "fetch_origin_branch", side_effect=self.fake_fetch_origin),
            "checkout": mock.patch.object(review, "checkout_remote_branch", side_effect=self.fake_checkout),
            "fetch_cmd": mock.patch.object(ticket_fetch, "cmd_fetch", side_effect=self.fake_cmd_fetch),
        }
        for patcher in patches.values():
            patcher.start()
            self.addCleanup(patcher.stop)

    def handoff_comment(self, db=None, head=A, author="developer1", uid="j1", cid=1):
        p = payload(author, head)
        p["db"] = db
        return {"id": cid, "user_id": uid, "created_on": "2026-10-02", "comment": dev_ticket.render_handoff(p)}

    def fake_fetch_origin(self, branch, repo=None):
        return self.fetches.get(branch, ("OK", A if branch == "FB_100" else B))

    def fake_checkout(self, branch, repo=None):
        self.checkouts.append((branch, repo))
        return self.checkout_answers.get(branch, ("OK", {"head": A if branch == "FB_100" else B}))

    checkout_answers = {}

    def fake_cmd_fetch(self, api, args):
        self.folder.mkdir(parents=True, exist_ok=True)
        (self.folder / "ticket.md").write_text("# ticket", encoding="utf-8")
        if not (self.folder / "state.json").exists():
            (self.folder / "state.json").write_text(json.dumps({"ticket": "100", "type": "CR", "role": "reviewer",
                                                                "stage": "2-check"}), encoding="utf-8")
        print('RESULT: {"status": "SAVED"}')
        return 0

    def go(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = dev_ticket.cmd_review_fetch(self.api, mock.Mock(ticket="100"))
        line = [ln for ln in out.getvalue().splitlines() if ln.startswith("RESULT: ")][-1]
        return code, json.loads(line[len("RESULT: "):])

    def state(self):
        return json.loads((self.folder / "state.json").read_text(encoding="utf-8"))

    def test_happy_path_writes_the_reviewer_state_and_the_hand_off_files(self):
        _code, res = self.go()
        self.assertEqual((res["status"], res["ticket"], res["author"], res["head"]), ("FETCHED", "100", "developer1", A[:12]))
        state = self.state()
        self.assertEqual((state["role"], state["kind"], state["stage"], state["review"]["stage"]),
                         ("reviewer", "review", "done", "R2-fetch"))
        self.assertEqual((state["review"]["author"], state["review"]["head"]), ("developer1", A))
        self.assertEqual(state["branch_origin"], "existing")
        self.assertEqual(self.checkouts, [("FB_100", None)])
        hand_off = self.folder / "hand-off"
        self.assertIn("HANDOFF-JSON-BEGIN", (hand_off / "handoff.md").read_text(encoding="utf-8"))
        self.assertIn("*Ticket #100", (hand_off / "summary.md").read_text(encoding="utf-8"))
        self.assertIn("# Code review", (hand_off / "author-review.md").read_text(encoding="utf-8"))

    def test_the_db_branch_and_files_are_carried_into_the_state(self):
        db = {"ticket": 200, "branch": "FB_200", "head": B, "files": ["Database/C2M/SP/x.sql"]}
        self.api.get_comments.side_effect = lambda n: [self.handoff_comment(db=db)]
        _code, res = self.go()
        self.assertEqual(res["status"], "FETCHED")
        self.assertEqual(self.checkouts, [("FB_100", None), ("FB_200", self.root / "db_repo")])
        self.assertEqual(self.state()["db"], {"ticket": 200, "files": ["Database/C2M/SP/x.sql"]})

    def test_a_second_fetch_clears_the_evidence_of_the_previous_head(self):
        self.go()
        state = self.state()
        state.update(gates={"status": "PASS"}, risk_check={"min_tier": "LOW"}, functional={"status": "PASS"},
                     functional_mode="run", profile={"name": "staging"})
        state["review"]["compare"] = {"status": "COMPARE_OK"}
        (self.folder / "state.json").write_text(json.dumps(state), encoding="utf-8")
        self.go()
        after = self.state()
        for key in ("gates", "risk_check", "functional", "functional_mode", "profile"):
            self.assertNotIn(key, after)
        self.assertNotIn("compare", after["review"])

    def test_a_code_equivalent_move_keeps_the_evidence(self):
        self.go()
        state = self.state()
        state.update(gates={"status": "PASS"}, functional={"status": "PASS"}, functional_mode="run")
        (self.folder / "state.json").write_text(json.dumps(state), encoding="utf-8")
        self.fetches["FB_100"] = ("OK", B)  # origin moved after the hand-off
        with mock.patch.object(review, "docs_only_delta", return_value=True):
            self.go()
        after = self.state()
        self.assertEqual((after["gates"], after["functional_mode"]), ({"status": "PASS"}, "run"))
        self.assertEqual(after["review"]["docs_only_delta"]["wf"]["to"], B[:12])

    def test_guards_on_the_ticket_and_the_caller(self):
        with mock.patch.dict(dev_ticket.CONFIG, {"reviewers": ["other"]}):
            self.assertEqual(self.go()[1]["status"], "NOT_A_REVIEWER")
        self.api.get_ticket.return_value = ticket(100, assignee="s2")
        self.assertEqual(self.go()[1]["status"], "NOT_ASSIGNED")
        self.api.get_ticket.return_value = ticket(100, status="Dev Done")
        self.assertEqual(self.go()[1]["status"], "WRONG_STATUS")
        self.assertFalse(self.folder.exists())

    def test_an_authors_work_folder_is_never_overwritten(self):
        self.folder.mkdir(parents=True)
        (self.folder / "state.json").write_text(json.dumps({"ticket": "100", "role": "developer", "stage": "6-build"}),
                                                encoding="utf-8")
        self.assertEqual(self.go()[1]["status"], "HAS_AUTHOR_STATE")
        self.assertEqual(self.state()["stage"], "6-build")
        self.assertEqual(self.checkouts, [])

    def test_a_bare_fetch_stub_is_taken_over_but_a_folder_with_work_is_not(self):
        self.folder.mkdir(parents=True)
        (self.folder / "ticket.md").write_text("# old", encoding="utf-8")
        (self.folder / "state.json").write_text(json.dumps(
            {"ticket": "100", "role": "reviewer", "stage": "2-check",
             "history": [{"stage": "1-pick", "result": "PICKED"}]}), encoding="utf-8")
        self.assertEqual(self.go()[1]["status"], "FETCHED")
        self.assertEqual(self.state()["kind"], "review")
        (self.folder / "state.json").write_text(json.dumps({"ticket": "100", "role": "reviewer", "stage": "2-check"}),
                                                encoding="utf-8")
        (self.folder / "PLAN.md").write_text("plan", encoding="utf-8")
        self.assertEqual(self.go()[1]["status"], "HAS_AUTHOR_STATE")

    def test_handoff_problems_stop_before_any_git_action(self):
        self.api.get_comments.side_effect = lambda n: []
        self.assertEqual(self.go()[1]["status"], "NO_HANDOFF")
        self.users["j2"] = "developer2"
        self.api.get_comments.side_effect = lambda n: [self.handoff_comment(), self.handoff_comment(uid="j2", author="developer2", cid=9)]
        self.assertEqual(self.go()[1]["status"], "HANDOFF_AMBIGUOUS")
        self.api.get_comments.side_effect = lambda n: [self.handoff_comment(author="someone-else")]
        self.assertEqual(self.go()[1]["status"], "HANDOFF_AUTHOR_MISMATCH")
        self.users["j1"] = "Reviewer1"
        self.api.get_comments.side_effect = lambda n: [self.handoff_comment(author="reviewer1")]
        self.assertEqual(self.go()[1]["status"], "SELF_REVIEW")
        self.assertEqual(self.checkouts, [])
        self.assertFalse(self.folder.exists())

    def test_a_dirty_tree_stops_and_lists_the_files(self):
        with mock.patch.object(common, "start_dirty_files", return_value=[" M a.ts", "?? b.ts"]):
            _code, res = self.go()
        self.assertEqual((res["status"], res["repo"], res["total"]), ("DIRTY", "wf", 2))
        self.assertEqual(self.checkouts, [])
        self.assertFalse(self.folder.exists())

    def test_a_dirty_db_tree_stops_too(self):
        db = {"ticket": 200, "branch": "FB_200", "head": B, "files": ["Database/C2M/SP/x.sql"]}
        self.api.get_comments.side_effect = lambda n: [self.handoff_comment(db=db)]
        with mock.patch.object(review, "dirty_files_in", return_value=[" M y.sql"]):
            _code, res = self.go()
        self.assertEqual((res["status"], res["repo"]), ("DIRTY", "db"))
        self.assertEqual(self.checkouts, [])

    def test_a_branch_that_moved_after_the_handoff_is_not_checked_out(self):
        self.fetches["FB_100"] = ("OK", B)
        _code, res = self.go()
        self.assertEqual((res["status"], res["handoff_head"], res["origin_head"]), ("STALE_HANDOFF", A[:12], B[:12]))
        self.assertEqual(self.checkouts, [])
        self.assertFalse(self.folder.exists())

    def test_a_moved_db_branch_is_stale_too(self):
        db = {"ticket": 200, "branch": "FB_200", "head": B, "files": ["x.sql"]}
        self.api.get_comments.side_effect = lambda n: [self.handoff_comment(db=db)]
        self.fetches["FB_200"] = ("OK", A)
        _code, res = self.go()
        self.assertEqual((res["status"], res["repo"]), ("STALE_HANDOFF", "db"))
        self.assertEqual(self.checkouts, [])

    def test_missing_branches_and_fetch_failures_are_reported(self):
        self.fetches["FB_100"] = ("BRANCH_NOT_ON_ORIGIN", None)
        self.assertEqual(self.go()[1]["status"], "BRANCH_NOT_ON_ORIGIN")
        self.fetches["FB_100"] = ("FETCH_FAILED", "could not resolve host")
        self.assertEqual(self.go()[1]["status"], "FETCH_FAILED")
        db = {"ticket": 200, "branch": "FB_200", "head": B, "files": ["x.sql"]}
        self.api.get_comments.side_effect = lambda n: [self.handoff_comment(db=db)]
        self.fetches = {"FB_200": ("BRANCH_NOT_ON_ORIGIN", None)}
        _code, res = self.go()
        self.assertEqual((res["status"], res["repo"]), ("DB_BRANCH_MISSING", "db"))

    def test_a_local_branch_with_unpushed_commits_is_refused(self):
        self.checkout_answers = {"FB_100": ("LOCAL_BRANCH_DIFFERS", {"local": B[:12], "origin": A[:12]})}
        self.addCleanup(lambda: setattr(self, "checkout_answers", {}))
        _code, res = self.go()
        self.assertEqual(res["status"], "LOCAL_BRANCH_DIFFERS")
        self.assertFalse(self.folder.exists())

    def test_nothing_is_written_to_assembla(self):
        self.go()
        called = {c[0] for c in self.api.method_calls}
        self.assertTrue(called <= ReviewPickTests.READ_ONLY, called - ReviewPickTests.READ_ONLY)


def reviewer_state(**over):
    state = {"ticket": "100", "role": "reviewer", "kind": "review", "stage": "done", "branch": "FB_100",
             "review": {"stage": "R2-fetch", "author": "developer1", "head": A},
             "gates": {"status": "PASS", "fingerprint": "fp1", "files": ["a.ts"]},
             "risk_check": {"min_tier": "MEDIUM", "reasons": [], "fingerprint": "rfp", "fp_files": ["r.ts"], "fp_auto": False},
             "functional_mode": "run",
             "functional": {"status": "PASS", "passed": 1, "failed": 0, "fingerprint": "fp1"}}
    state.update(over)
    return state


def facts(**over):
    base = {"origin_head": A, "local_head": A, "db_origin_head": None, "db_local_head": None, "dirty": [],
            "behind_master": 0, "conflicts": [], "fingerprint_now": lambda: "fp1", "risk_fingerprint_now": lambda: "rfp"}
    base.update(over)
    return base


def codes(items):
    return [i["code"] for i in items]


class CompareHandoffTests(unittest.TestCase):
    def compare(self, state=None, p=None, early=False, **fact_over):
        return dev_ticket.compare_handoff(p or payload(), state or reviewer_state(), facts(**fact_over), early=early)

    def test_everything_matching_is_clean(self):
        blocking, warnings, tier = self.compare()
        self.assertEqual((blocking, warnings, tier), ([], [], "MEDIUM"))

    def test_a_branch_that_moved_after_the_handoff_is_stale(self):
        blocking, _w, _t = self.compare(origin_head=B)
        self.assertIn("STALE_HANDOFF", codes(blocking))

    def test_a_local_checkout_that_is_not_the_handed_off_head(self):
        blocking, _w, _t = self.compare(local_head=B)
        self.assertEqual(codes(blocking), ["HEAD_DIFFERS"])

    def test_db_heads_are_compared_too(self):
        p = payload()
        p["db"] = {"ticket": 200, "branch": "FB_200", "head": B, "files": ["x.sql"]}
        self.assertEqual(codes(self.compare(p=p, db_origin_head=A, db_local_head=B)[0]), ["STALE_HANDOFF"])
        self.assertEqual(codes(self.compare(p=p, db_origin_head=B, db_local_head=A)[0]), ["HEAD_DIFFERS"])
        self.assertEqual(self.compare(p=p, db_origin_head=B, db_local_head=B)[0], [])

    def test_edits_in_the_review_checkout_are_reported(self):
        self.assertEqual(codes(self.compare(dirty=[" M a.ts"])[0]), ["TREE_MODIFIED"])

    def test_behind_master_blocks_and_names_the_conflicts(self):
        blocking, _w, _t = self.compare(behind_master=3, conflicts=["x.ts"])
        self.assertEqual(codes(blocking), ["BEHIND_MASTER"])
        self.assertEqual((blocking[0]["behind"], blocking[0]["conflicts"]), (3, ["x.ts"]))

    def test_gates_must_have_run_pass_and_match_the_files(self):
        self.assertEqual(codes(self.compare(state=reviewer_state(gates=None))[0]), ["GATES_NOT_RUN"])
        self.assertEqual(codes(self.compare(state=reviewer_state(gates={"status": "FAIL", "fingerprint": "fp1"}))[0]),
                         ["GATES_NOT_PASS"])
        self.assertEqual(codes(self.compare(fingerprint_now=lambda: "other")[0]).count("GATES_STALE"), 1)

    def test_the_risk_check_must_have_run_and_be_current(self):
        self.assertEqual(codes(self.compare(state=reviewer_state(risk_check=None))[0]), ["RISK_NOT_RUN"])
        self.assertEqual(codes(self.compare(risk_fingerprint_now=lambda: "other")[0]), ["RISK_STALE"])

    def test_a_higher_reviewer_tier_is_a_warning_and_becomes_the_tier(self):
        state = reviewer_state()
        state["risk_check"]["min_tier"] = "HIGH"
        blocking, warnings, tier = self.compare(state=state)
        self.assertEqual((blocking, codes(warnings), tier), ([], ["RISK_HIGHER"], "HIGH"))
        self.assertEqual((warnings[0]["handoff"], warnings[0]["review"]), ("MEDIUM", "HIGH"))

    def test_a_lower_reviewer_tier_keeps_the_authors_tier(self):
        state = reviewer_state()
        state["risk_check"]["min_tier"] = "LOW"
        self.assertEqual(self.compare(state=state)[2], "MEDIUM")

    def test_a_different_file_list_is_a_warning_with_both_sides(self):
        _b, warnings, _t = self.compare(state=reviewer_state(gates={"status": "PASS", "fingerprint": "fp1",
                                                                     "files": ["a.ts", "extra.ts"]}))
        self.assertEqual(codes(warnings), ["FILES_DIFFER"])
        self.assertEqual((warnings[0]["only_in_review"], warnings[0]["only_in_handoff"]), (["extra.ts"], []))

    def test_a_capped_handoff_file_list_is_compared_by_count_only(self):
        p = payload()
        p["gates"]["files_count"] = 250  # the comment lists only the first 100
        _b, warnings, _t = self.compare(p=p, state=reviewer_state(gates={"status": "PASS", "fingerprint": "fp1",
                                                                          "files": [f"f{i}.ts" for i in range(250)]}))
        self.assertEqual(warnings, [])

    def test_the_senders_own_functional_result_is_not_used_only_the_reviewers(self):
        blocking, _w, _t = self.compare(state=reviewer_state(functional_mode=None, functional=None))
        self.assertEqual(codes(blocking), ["FUNCTIONAL_MODE_REQUIRED"])
        blocking, _w, _t = self.compare(state=reviewer_state(functional={"status": "FAIL", "fingerprint": "fp1"}))
        self.assertEqual(codes(blocking), ["FUNCTIONAL_REQUIRED"])
        blocking, _w, _t = self.compare(fingerprint_now=lambda: "fp2", state=reviewer_state(
            gates={"status": "PASS", "fingerprint": "fp2", "files": ["a.ts"]}))
        self.assertEqual(codes(blocking), ["FUNCTIONAL_STALE"])

    def test_a_reviewer_recorded_skip_is_accepted_with_its_reason(self):
        state = reviewer_state(functional_mode="skip", functional=None, functional_skip={"reason": "no UI path"})
        self.assertEqual(self.compare(state=state)[0], [])

    def test_early_mode_ignores_only_the_functional_checks(self):
        state = reviewer_state(functional_mode=None, functional=None, gates=None)
        self.assertEqual(codes(self.compare(state=state, early=True)[0]), ["GATES_NOT_RUN"])


class ReviewCompareCommandTests(unittest.TestCase):
    def setUp(self):
        import local_gates
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.folder = self.root / "DEV_TICKETS" / "100"
        (self.folder / "hand-off").mkdir(parents=True)
        (self.folder / "hand-off" / "handoff.md").write_text(dev_ticket.render_handoff(payload()), encoding="utf-8")
        self.write_state(reviewer_state())
        self.git_out = {("branch", "--show-current"): "FB_100", ("rev-parse", "HEAD"): A}
        for patcher in (
                mock.patch.object(common, "PROJECT_ROOT", self.root),
                mock.patch.object(common, "git", side_effect=lambda *a, **k: mock.Mock(stdout=self.git_out.get(a, "") + "\n", returncode=0)),
                mock.patch.object(review, "fetch_origin_branch", return_value=("OK", A)),
                mock.patch.object(common, "uncommitted_tracked", return_value=[]),
                mock.patch.object(common, "master_sync_info", return_value=(2, 0, [])),
                mock.patch.object(local_gates, "diff_fingerprint", side_effect=lambda files: "rfp" if files == ["r.ts"] else "fp1"),
                mock.patch.object(local_gates, "changed_files", return_value=["a.ts"]),
                mock.patch.object(local_gates, "risk_files", return_value=["r.ts"])):
            patcher.start()
            self.addCleanup(patcher.stop)

    def write_state(self, state):
        (self.folder / "state.json").write_text(json.dumps(state), encoding="utf-8")

    def state(self):
        return json.loads((self.folder / "state.json").read_text(encoding="utf-8"))

    def go(self, early=False, ticket="100"):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = dev_ticket.cmd_review_compare(None, mock.Mock(ticket=ticket, early=early))
        line = [ln for ln in out.getvalue().splitlines() if ln.startswith("RESULT: ")][-1]
        return code, json.loads(line[len("RESULT: "):])

    def test_a_clean_comparison_advances_the_review_and_records_the_tier(self):
        _code, res = self.go()
        self.assertEqual((res["status"], res["tier"], res["blocking"]), ("COMPARE_OK", "MEDIUM", []))
        saved = self.state()["review"]
        self.assertEqual((saved["stage"], saved["tier"], saved["compare"]["status"]), ("R3-check", "MEDIUM", "COMPARE_OK"))

    def test_a_blocking_mismatch_does_not_advance_the_review(self):
        with mock.patch.object(common, "uncommitted_tracked", return_value=[" M a.ts"]):
            _code, res = self.go()
        self.assertEqual(res["status"], "COMPARE_MISMATCH")
        self.assertEqual([b["code"] for b in res["blocking"]], ["TREE_MODIFIED"])
        saved = self.state()["review"]
        self.assertEqual(saved["stage"], "R2-fetch")
        self.assertEqual(saved["compare"]["status"], "COMPARE_MISMATCH")

    def test_warnings_do_not_block(self):
        state = reviewer_state()
        state["risk_check"]["min_tier"] = "HIGH"
        self.write_state(state)
        _code, res = self.go()
        self.assertEqual((res["status"], res["tier"], [w["code"] for w in res["warnings"]]), ("COMPARE_OK", "HIGH", ["RISK_HIGHER"]))

    def test_early_mode_never_advances_the_stage(self):
        self.write_state(reviewer_state(functional_mode=None, functional=None))
        _code, res = self.go(early=True)
        self.assertEqual((res["status"], res["blocking"]), ("COMPARE_PARTIAL", []))
        self.assertEqual(self.state()["review"]["stage"], "R2-fetch")

    def test_a_moved_origin_is_stale(self):
        with mock.patch.object(review, "fetch_origin_branch", return_value=("OK", B)), \
                mock.patch.object(review, "docs_only_delta", return_value=False):
            _code, res = self.go()
        self.assertIn("STALE_HANDOFF", [b["code"] for b in res["blocking"]])

    def test_a_documentation_only_move_is_a_warning_not_stale(self):
        self.git_out[("rev-parse", "HEAD")] = B
        with mock.patch.object(review, "fetch_origin_branch", return_value=("OK", B)), \
                mock.patch.object(review, "docs_only_delta", return_value=True):
            _code, res = self.go()
        self.assertEqual(res["blocking"], [])
        self.assertEqual([w["code"] for w in res["warnings"]], ["DOCS_ONLY_DELTA"])

    def test_a_clean_lag_behind_master_is_only_a_warning(self):
        with mock.patch.object(common, "master_sync_info", return_value=(2, 3, [])):
            _code, res = self.go()
        self.assertEqual((res["status"], res["blocking"]), ("COMPARE_OK", []))
        self.assertEqual([w["code"] for w in res["warnings"]], ["BEHIND_MASTER"])

    def test_doc_paths_are_not_code(self):
        self.assertTrue(all(dev_ticket._is_doc_path(f) for f in ("README.md", "a/b/notes.txt", "x/docs/img.png")))
        self.assertFalse(any(dev_ticket._is_doc_path(f) for f in ("a/b.ts", "a/b.component.html", "docs.ts", "p.json")))

    def test_behind_master_is_reported_with_its_numbers(self):
        with mock.patch.object(common, "master_sync_info", return_value=(2, 4, ["x.ts"])):
            _code, res = self.go()
        self.assertEqual([(b["code"], b["behind"]) for b in res["blocking"]], [("BEHIND_MASTER", 4)])

    def test_only_a_review_folder_with_a_saved_handoff_is_compared(self):
        self.assertEqual(self.go(ticket="999")[1]["status"], "NO_STATE")
        self.write_state({"ticket": "100", "role": "developer", "stage": "6-build"})
        self.assertEqual(self.go()[1]["status"], "NOT_A_REVIEW_FOLDER")
        self.write_state(reviewer_state())
        (self.folder / "hand-off" / "handoff.md").unlink()
        self.assertEqual(self.go()[1]["status"], "NO_HANDOFF")

    def test_the_wrong_branch_is_reported(self):
        self.git_out[("branch", "--show-current")] = "master"
        self.assertEqual(self.go()[1]["status"], "WRONG_BRANCH")

    def test_a_fetch_failure_is_reported(self):
        with mock.patch.object(review, "fetch_origin_branch", return_value=("FETCH_FAILED", "offline")):
            self.assertEqual(self.go()[1]["status"], "FETCH_FAILED")


class AuthorPathGuardTests(unittest.TestCase):
    """A reviewer who runs the author path (dt fetch / dt pick --ticket) on a ticket in Code Review is pointed to /review-and-deploy."""
    def setUp(self):
        self.api = mock.Mock()
        self.api.get_current_user.return_value = {"id": "s1"}
        self.t = {"number": 100, "status": dev_ticket.STATUS["code_review"], "assigned_to_id": "s1"}
        self.api.get_ticket.return_value = self.t

    def fetch(self, role="reviewer", **ns):
        out = io.StringIO()
        with mock.patch.object(common, "_remembered_role", return_value=role), contextlib.redirect_stdout(out):
            code = dev_ticket.cmd_fetch(self.api, argparse.Namespace(ticket="100", type=None, **ns))
        return code, out.getvalue()

    def test_a_reviewer_fetching_their_review_is_sent_to_review_and_deploy(self):
        code, out = self.fetch()
        self.assertEqual(code, 1)
        self.assertIn("USE_REVIEW_AND_DEPLOY", out)
        self.api.get_tags.assert_not_called()

    def test_the_guard_is_only_for_a_reviewers_own_code_review_ticket(self):
        for role, status, assignee in (("developer", None, "s1"), ("reviewer", "Dev In Process", "s1"), ("reviewer", None, "s2")):
            self.t.update(status=status or dev_ticket.STATUS["code_review"], assigned_to_id=assignee)
            with mock.patch.object(common, "_remembered_role", return_value=role):
                self.assertFalse(dev_ticket._is_my_review(self.api, self.t), (role, status, assignee))

    def test_the_review_fetch_itself_is_not_blocked(self):
        self.api.get_tags.side_effect = RuntimeError("past the guard")
        with self.assertRaises(RuntimeError):
            self.fetch(from_review=True)


class HandoffDeltaGitTests(unittest.TestCase):
    """docs_only_delta against a real throw-away git repo (no mocks)."""

    def test_a_merge_of_master_keeps_the_handoff_but_a_code_change_does_not(self):
        base = dev_ticket.BASE
        with tempfile.TemporaryDirectory() as tmp:
            origin, work = Path(tmp, "origin.git"), Path(tmp, "work")

            def run(*args, cwd=work):
                return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()

            run("init", "--bare", "-b", base, str(origin), cwd=tmp)
            run("clone", str(origin), str(work), cwd=tmp)
            run("config", "user.email", "t@t")
            run("config", "user.name", "t")
            (work / "a.ts").write_text("a\n")
            run("add", "."); run("commit", "-m", "init"); run("push", "origin", base)
            run("switch", "-c", "FB_1")
            (work / "fix.ts").write_text("fix\n")
            run("add", "."); run("commit", "-m", "fix")
            handed_off = run("rev-parse", "HEAD")
            run("switch", base)
            (work / "other.ts").write_text("other\n")
            run("add", "."); run("commit", "-m", "master moves"); run("push", "origin", base)
            run("switch", "FB_1")
            run("merge", base, "-m", "merge master")
            merged = run("rev-parse", "HEAD")
            (work / "fix.ts").write_text("fix2\n")
            run("add", "."); run("commit", "-m", "more code")
            changed = run("rev-parse", "HEAD")
            with mock.patch.object(common, "PROJECT_ROOT", work):
                self.assertTrue(dev_ticket.docs_only_delta(handed_off, merged, work))
                self.assertFalse(dev_ticket.docs_only_delta(handed_off, changed, work))


if __name__ == "__main__":
    unittest.main()
