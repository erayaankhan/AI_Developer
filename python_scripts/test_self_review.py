"""`allow_self_review`: a reviewer assigns their own hand-off to themselves, reviews, approves and deploys it. The
existing happy-path tests are re-run with the caller as the author; the folder hand-over (author folder parked as
DEV_TICKETS/{N}.author during the review, put back by `dt rework`) is tested on its own. Assembla and git are mocked."""
import json
import unittest
from unittest import mock

import dev_ticket
import test_review_deploy as trd
import test_review_flow as trf
import test_review_verdict as trv
import test_rework as trw
import test_role_handoff as trh

SELF = {"allow_self_review": True}


def only(cls, *keep):
    """Run only the named inherited tests (the rest already run in their own module)."""
    for name in dir(cls):
        if name.startswith("test") and name not in keep and name not in cls.__dict__:
            setattr(cls, name, None)
    return cls


class ConfigTests(unittest.TestCase):
    def test_off_by_default_on_in_both_team_profiles(self):
        from config_loader import load_config
        with mock.patch.dict("os.environ", {"DEV_TICKET_PROFILE": "none"}):
            self.assertFalse(load_config().get("allow_self_review"))
        for name in ("launchpad", "c2m"):
            with mock.patch.dict("os.environ", {"DEV_TICKET_PROFILE": name}):
                self.assertTrue(load_config().get("allow_self_review"), name)


@only
class SelfAssignTests(trh.AssignTests):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(dev_ticket.CONFIG, dict(SELF, reviewers=["developer1", "reviewer2"]))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_assigning_to_yourself_records_you_as_the_reviewer_without_an_assembla_write(self):
        self.api.get_space_users.return_value.append({"id": "u1", "login": "developer1"})
        _code, res, _ = self.go(to="developer1", yes=True)
        self.assertEqual((res["status"], res["reviewer"], res["self_review"]), ("ASSIGNED", "developer1", True))
        self.api.assign_ticket.assert_not_called()  # it is already yours
        self.assertEqual(self.state()["handoff"]["reviewer"], "developer1")
        _code, res, _ = self.go(to="developer1", yes=True)
        self.assertEqual(res["status"], "ALREADY_ASSIGNED")

    def test_without_the_setting_it_is_still_refused(self):
        with mock.patch.dict(dev_ticket.CONFIG, {"allow_self_review": False}):
            _code, res, _ = self.go(to="developer1", yes=True)
        self.assertEqual(res["status"], "SELF_ASSIGN")


@only
class SelfFetchTests(trf.ReviewFetchTests):
    """The reviewer (Reviewer1) wrote the hand-off from their own work folder DEV_TICKETS/100."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(dev_ticket.CONFIG, SELF)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.api.get_comments.side_effect = lambda n: [self.handoff_comment(author="reviewer1", uid="s1")]
        self.folder.mkdir(parents=True)
        self.author_state = {"ticket": "100", "role": "reviewer", "stage": "8-handoff",
                             "handoff": {"comment_id": 1, "head": trf.A, "round": 1, "reviewer": "Reviewer1"}}
        (self.folder / "state.json").write_text(json.dumps(self.author_state), encoding="utf-8")
        (self.folder / "PLAN.md").write_text("my plan", encoding="utf-8")
        self.parked = self.folder.with_name("100.author")

    def test_own_hand_off_parks_the_author_folder_and_fetches_the_review(self):
        _code, res = self.go()
        self.assertEqual((res["status"], res["author"], res["self_review"]), ("FETCHED", "Reviewer1", True))
        self.assertEqual(json.loads((self.parked / "state.json").read_text(encoding="utf-8")), self.author_state)
        self.assertEqual((self.parked / "PLAN.md").read_text(encoding="utf-8"), "my plan")
        state = self.state()
        self.assertEqual((state["kind"], state["review"]["author"], state["review"]["self_review"]),
                         ("review", "Reviewer1", True))

    def test_someone_elses_hand_off_never_moves_my_work(self):
        self.api.get_comments.side_effect = lambda n: [self.handoff_comment()]
        self.assertEqual(self.go()[1]["status"], "HAS_AUTHOR_STATE")
        self.assertFalse(self.parked.exists())
        self.assertEqual(self.checkouts, [])

    def test_a_folder_from_another_hand_off_is_not_moved(self):
        self.author_state["handoff"]["comment_id"] = 99
        (self.folder / "state.json").write_text(json.dumps(self.author_state), encoding="utf-8")
        self.assertEqual(self.go()[1]["status"], "HAS_AUTHOR_STATE")
        self.assertFalse(self.parked.exists())

    def test_an_already_parked_folder_is_not_overwritten(self):
        self.parked.mkdir()
        self.assertEqual(self.go()[1]["status"], "AUTHOR_FOLDER_PARKED")
        self.assertEqual(json.loads((self.folder / "state.json").read_text(encoding="utf-8")), self.author_state)

    def test_without_the_setting_it_is_still_refused(self):
        with mock.patch.dict(dev_ticket.CONFIG, {"allow_self_review": False}):
            self.assertEqual(self.go()[1]["status"], "HAS_AUTHOR_STATE")


def _as_own_work(fixture_cls):
    """The review folder's author is the caller (Reviewer1), with self-review allowed."""
    class Own(fixture_cls):
        def setUp(self):
            super().setUp()
            patcher = mock.patch.dict(dev_ticket.CONFIG, SELF)
            patcher.start()
            self.addCleanup(patcher.stop)
            self.write_state(json.loads((self.folder / "state.json").read_text(encoding="utf-8")))

        def write_state(self, state):
            if state.get("kind") == "review":
                state["review"]["author"] = "reviewer1"
            super().write_state(state)
    Own.__name__ = "Own" + fixture_cls.__name__
    return Own


SelfApproveTests = only(_as_own_work(trv.ApproveTests),
                        "test_yes_posts_the_comment_moves_to_dev_done_and_binds_the_approval_to_the_head")
SelfReviewDeployTests = only(_as_own_work(trd.ReviewDeployTests),
                             "test_yes_moves_to_devops_staging_and_records_the_request_without_triggering_anything")
SelfStagingDeployTests = only(_as_own_work(trd.ReviewerStagingDeployTests),
                              "test_an_approved_requested_ticket_is_deployed_once_and_the_attempt_is_recorded")


@only
class SelfReworkTests(trw.ReworkTests):
    """Rejected my own review: DEV_TICKETS/100 is the review, my work waits in 100.author."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(dev_ticket.CONFIG, SELF)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.users["j1"] = "developer1"
        self.comments = [trw.verdict_comment(user="j1", reviewer="developer1")]
        self.parked = self.folder.with_name("100.author")
        self.folder.rename(self.parked)
        own = trw.author_state()
        own["handoff"]["reviewer"] = "developer1"
        (self.parked / "state.json").write_text(json.dumps(own), encoding="utf-8")
        self.folder.mkdir()
        self.write_state({"ticket": "100", "kind": "review", "stage": "done",
                          "review": {"author": "developer1", "self_review": True, "head": trw.A}})

    def test_preview_reads_the_parked_folder_and_moves_nothing(self):
        _code, res = self.go()
        self.assertEqual(res["status"], "PREVIEW")
        self.assertTrue(self.parked.is_dir())
        self.assertEqual(self.state()["kind"], "review")

    def test_yes_puts_my_work_back_and_keeps_the_review(self):
        _code, res = self.go(yes=True)
        self.assertEqual(res["status"], "REWORK_READY")
        self.assertFalse(self.parked.exists())
        kept = self.folder.with_name("100.review-r1")
        self.assertEqual(res["review_folder_kept"], str(kept))
        self.assertEqual(json.loads((kept / "state.json").read_text(encoding="utf-8"))["kind"], "review")
        state = self.state()
        self.assertEqual((state["stage"], state["round"]), ("6-build", 2))
        self.assertTrue((self.folder / "reviews" / "review-feedback-round1.md").is_file())


if __name__ == "__main__":
    unittest.main()
