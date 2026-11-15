"""Teams review request at hand-off (teams_notify.py, `dt assign`, `dt teams-notify`). Nothing is posted: the webhook
call is mocked."""
import json
import os
import unittest
import urllib.error
from unittest import mock

import dev_ticket
import teams_notify
import test_role_handoff as trh

GOOD_URL = "https://prod-01.westeurope.logic.azure.com/workflows/abc/triggers/manual/paths/invoke?sig=SECRET"
CFG = {"teams": {"enabled": True, "env_key": "TEAMS_WEBHOOK_URL_TEST"}, "assembla_space": "c2m-ci"}


class FakeResponse:
    def __init__(self, status=202):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class CardTests(unittest.TestCase):
    def test_the_card_mentions_the_reviewer_by_email_and_links_the_ticket(self):
        msg = teams_notify.review_request(number="100", title="Story: Fix save", url="https://x/100", reviewer="rev1",
                                          reviewer_name="Sam Reviewer", email="sam@plasmacomp.com", author="dev1",
                                          round_no=2, risk="MEDIUM")
        card = msg["payload"]["attachments"][0]["content"]
        self.assertEqual(card["msteams"]["entities"][0]["mentioned"], {"id": "sam@plasmacomp.com", "name": "Sam Reviewer"})
        self.assertIn("<at>Sam Reviewer</at>", card["body"][0]["text"])
        self.assertEqual(card["actions"][0]["url"], "https://x/100")
        self.assertIn("/review-and-deploy 100", json.dumps(card))
        self.assertIn("round 2, risk MEDIUM", msg["text"])

    def test_without_an_email_the_name_is_plain_text(self):
        msg = teams_notify.review_request(number="1", title="t", url="u", reviewer="rev1", reviewer_name="", email="",
                                          author="a", round_no=None, risk=None)
        card = msg["payload"]["attachments"][0]["content"]
        self.assertNotIn("msteams", card)
        self.assertTrue(card["body"][0]["text"].startswith("rev1,"))

    def test_an_email_override_wins(self):
        cfg = {"teams": {"emails": {"Rev1": "other@x.com"}}}
        self.assertEqual(teams_notify.reviewer_email(cfg, "rev1", {"email": "a@x.com"}), "other@x.com")
        self.assertEqual(teams_notify.reviewer_email({}, "rev1", {"email": "a@x.com"}), "a@x.com")


class WebhookTests(unittest.TestCase):
    def go(self, url, opener=None):
        with mock.patch.dict(os.environ, {"TEAMS_WEBHOOK_URL_TEST": url} if url else {}, clear=False):
            os.environ.pop("TEAMS_WEBHOOK_URL_TEST", None) if not url else None
            return teams_notify.post(CFG, {"x": 1}, opener=opener or (lambda req, timeout: FakeResponse()))

    def test_sent(self):
        seen = []
        res = self.go(GOOD_URL, lambda req, timeout: seen.append(json.loads(req.data)) or FakeResponse(202))
        self.assertEqual((res, seen), ({"status": "SENT"}, [{"x": 1}]))

    def test_missing_and_foreign_urls_are_refused_before_any_call(self):
        called = mock.Mock()
        self.assertEqual(self.go("", called)["status"], "TEAMS_NOT_CONFIGURED")
        self.assertEqual(self.go("https://evil.example.com/hook", called)["status"], "TEAMS_BAD_URL")
        self.assertEqual(self.go("http://prod-01.logic.azure.com/x", called)["status"], "TEAMS_BAD_URL")
        self.assertEqual(self.go("https://logic.azure.com.evil.com/x", called)["status"], "TEAMS_BAD_URL")
        called.assert_not_called()

    def test_a_failure_never_echoes_the_url(self):
        def boom(req, timeout):
            raise urllib.error.HTTPError(GOOD_URL, 400, "Bad Request", {}, None)
        res = self.go(GOOD_URL, boom)
        self.assertEqual(res, {"status": "TEAMS_FAILED", "error": "HTTP 400"})
        self.assertNotIn("SECRET", json.dumps(res))


class _TeamsAssignFixture(trh.AssignTests):
    def setUp(self):
        super().setUp()
        self.posted = []
        for patcher in (mock.patch.dict(dev_ticket.CONFIG, CFG),
                        mock.patch.dict(os.environ, {"TEAMS_WEBHOOK_URL_TEST": GOOD_URL}),
                        mock.patch.object(teams_notify, "post", side_effect=self.fake_post)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.api.get_space_users.return_value[1]["email"] = "sam@plasmacomp.com"
        self.api.get_ticket.return_value = {"status": "Code Review", "assigned_to_id": "u1", "summary": "Story: Fix save"}
        state = self.state()
        state["handoff"].update(risk="MEDIUM")
        self.write_state(state)

    answer = {"status": "SENT"}

    def fake_post(self, config, payload):
        self.posted.append(payload)
        return dict(self.answer)


for _name in dir(_TeamsAssignFixture):  # the inherited assign tests run in test_role_handoff
    if _name.startswith("test"):
        setattr(_TeamsAssignFixture, _name, None)


class AssignPostsOnTeamsTests(_TeamsAssignFixture):
    def test_preview_shows_the_message_and_posts_nothing(self):
        _code, res, _ = self.go(to="reviewer1")
        self.assertEqual(res["teams"]["status"], "WILL_POST")
        self.assertIn("#100 Story: Fix save is ready for your review", res["teams"]["message"])
        self.assertTrue(res["teams"]["mentions"])
        self.assertEqual(self.posted, [])

    def test_yes_assigns_then_posts_once_and_records_it(self):
        _code, res, _ = self.go(to="reviewer1", yes=True)
        self.assertEqual((res["status"], res["teams"]["status"]), ("ASSIGNED", "SENT"))
        self.assertEqual(len(self.posted), 1)
        card = self.posted[0]["attachments"][0]["content"]
        self.assertEqual(card["msteams"]["entities"][0]["mentioned"]["id"], "sam@plasmacomp.com")
        self.assertIn("https://app.assembla.com/spaces/c2m-ci/tickets/100", json.dumps(card))
        self.assertEqual(self.state()["handoff"]["teams"]["status"], "SENT")

    def test_a_teams_failure_keeps_the_assignment(self):
        self.answer = {"status": "TEAMS_FAILED", "error": "HTTP 400"}
        _code, res, _ = self.go(to="reviewer1", yes=True)
        self.assertEqual((res["status"], res["teams"]["status"]), ("ASSIGNED", "TEAMS_FAILED"))
        self.api.assign_ticket.assert_called_once()

    def test_no_webhook_yet_still_assigns_and_says_so(self):
        with mock.patch.dict(os.environ, {"TEAMS_WEBHOOK_URL_TEST": ""}):
            _code, res, _ = self.go(to="reviewer1", yes=True)
        self.assertEqual((res["status"], res["teams"]["status"]), ("ASSIGNED", "TEAMS_NOT_CONFIGURED"))
        self.assertEqual(self.posted, [])

    def test_teams_off_adds_nothing(self):
        with mock.patch.dict(dev_ticket.CONFIG, {"teams": {"enabled": False}}):
            _code, res, _ = self.go(to="reviewer1", yes=True)
        self.assertIsNone(res.get("teams"))
        self.assertEqual(self.posted, [])

    def test_self_review_posts_nothing(self):
        self.api.get_space_users.return_value.append({"id": "u1", "login": "developer1"})
        with mock.patch.dict(dev_ticket.CONFIG, {"allow_self_review": True, "reviewers": ["developer1"]}):
            _code, res, _ = self.go(to="developer1", yes=True)
        self.assertEqual(res["status"], "ASSIGNED")
        self.assertEqual(self.posted, [])


class TeamsNotifyCommandTests(_TeamsAssignFixture):
    def notify(self, yes=False, again=False):
        return trh.run_with_api(dev_ticket.cmd_teams_notify, self.api, ticket="100", yes=yes, again=again)[1]

    def test_needs_an_assigned_reviewer(self):
        self.assertEqual(self.notify(yes=True)["status"], "NOT_ASSIGNED")

    def test_resend_after_a_failure_then_a_reminder_needs_again(self):
        self.answer = {"status": "TEAMS_FAILED", "error": "HTTP 500"}
        self.go(to="reviewer1", yes=True)
        self.api.get_ticket.return_value = {"status": "Code Review", "assigned_to_id": "s1", "summary": "Story: Fix save"}
        self.answer = {"status": "SENT"}
        self.assertEqual(self.notify()["status"], "PREVIEW")
        self.assertEqual(self.notify(yes=True)["status"], "SENT")
        self.assertEqual(self.notify(yes=True)["status"], "ALREADY_SENT")
        self.assertEqual(self.notify(yes=True, again=True)["status"], "SENT")
        self.assertEqual(len(self.posted), 3)

    def test_not_in_code_review_any_more(self):
        self.go(to="reviewer1", yes=True)
        self.api.get_ticket.return_value = {"status": "Dev Done", "assigned_to_id": "s1"}
        self.assertEqual(self.notify(yes=True)["status"], "WRONG_STATUS")


class ProfileTests(unittest.TestCase):
    def test_on_in_both_team_profiles_with_their_own_webhook_key(self):
        from config_loader import load_config
        for name, key in (("launchpad", "TEAMS_WEBHOOK_URL_LAUNCHPAD"), ("c2m", "TEAMS_WEBHOOK_URL_C2M")):
            with mock.patch.dict(os.environ, {"DEV_TICKET_PROFILE": name}):
                cfg = load_config()
            self.assertEqual((cfg["teams"]["enabled"], cfg["teams"]["env_key"]), (True, key))
        with mock.patch.dict(os.environ, {"DEV_TICKET_PROFILE": "none"}):
            self.assertFalse(load_config()["teams"]["enabled"])

    def test_credentials_load_teams_keys(self):
        import credentials
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "credentials.env"
            path.write_text("TEAMS_WEBHOOK_URL_GENAI=https://a.logic.azure.com/x\nOTHER=1\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"DEV_TICKET_CREDENTIALS": str(path)}):
                os.environ.pop("TEAMS_WEBHOOK_URL_GENAI", None)
                credentials.load()
                self.assertEqual(os.environ.get("TEAMS_WEBHOOK_URL_GENAI"), "https://a.logic.azure.com/x")
                self.assertIsNone(os.environ.get("OTHER"))


if __name__ == "__main__":
    unittest.main()
