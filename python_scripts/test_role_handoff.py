"""Phase 1 of the developer hand-off (REVIEW_TICKET_DESIGN.md): the remembered developer role (`dt role`), the role in the
ticket state, and the reviewers config. No Assembla or git call is made."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from confirm_support import preview_hash
import dev_ticket
from develop_ticket import common, review
import functional

ROLES = ["developer", "reviewer"]


def run(handler, **kwargs):
    """Call a dt handler and return (exit code, parsed RESULT dict, full output)."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = handler(None, mock.Mock(**kwargs))
    line = [ln for ln in out.getvalue().splitlines() if ln.startswith("RESULT: ")][-1]
    return code, json.loads(line[len("RESULT: "):]), out.getvalue()


def run_with_api(handler, api, **kwargs):
    """Like run(), for a handler that takes a real API object (mocked)."""
    out = io.StringIO()
    if kwargs.get("yes") and "confirm" not in kwargs:  # as the workflow does: preview first, then apply with its hash
        kwargs["confirm"] = preview_hash(lambda: handler(api, mock.Mock(**{**kwargs, "yes": False})))
    with contextlib.redirect_stdout(out):
        code = handler(api, mock.Mock(**kwargs))
    line = [ln for ln in out.getvalue().splitlines() if ln.startswith("RESULT: ")][-1]
    return code, json.loads(line[len("RESULT: "):]), out.getvalue()


class EnvKeyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "local_env.env"

    def test_read_role_unset_without_file_or_key(self):
        self.assertIsNone(functional.read_role(self.path, "DEV_ROLE", ROLES))
        self.path.write_text("staging.GatewayAPIUrl=https://x\n", encoding="utf-8")
        self.assertIsNone(functional.read_role(self.path, "DEV_ROLE", ROLES))

    def test_read_role_is_case_insensitive_and_validated(self):
        self.path.write_text("DEV_ROLE= Reviewer \n", encoding="utf-8")
        self.assertEqual(functional.read_role(self.path, "DEV_ROLE", ROLES), "reviewer")
        self.path.write_text("DEV_ROLE=manager\n", encoding="utf-8")
        self.assertIsNone(functional.read_role(self.path, "DEV_ROLE", ROLES))

    def test_set_key_creates_the_file(self):
        functional.set_env_key(self.path, "DEV_ROLE", "developer")
        self.assertEqual(self.path.read_text(encoding="utf-8"), "DEV_ROLE=developer\n")

    def test_set_key_keeps_every_other_line(self):
        original = "# my env\nlocal.db.connection=Server=h;Pwd=secret\n\nlocal.GatewayAPIUrl=http://localhost:5000"
        self.path.write_text(original, encoding="utf-8")
        functional.set_env_key(self.path, "DEV_ROLE", "reviewer")
        text = self.path.read_text(encoding="utf-8")
        self.assertTrue(text.startswith(original + "\n"))
        self.assertTrue(text.endswith("DEV_ROLE=reviewer\n"))

    def test_set_key_replaces_an_existing_value_once(self):
        self.path.write_text("DEV_ROLE=developer\nother=1\n", encoding="utf-8")
        functional.set_env_key(self.path, "DEV_ROLE", "reviewer")
        self.assertEqual(self.path.read_text(encoding="utf-8"), "DEV_ROLE=reviewer\nother=1\n")


class RoleCommandTests(unittest.TestCase):
    """`dt role`: the config lists decide the role; someone on neither list is asked once."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "AI_DEV_ENV").mkdir()
        self.env = self.root / "AI_DEV_ENV" / "credentials.env"
        self.login = "tester"
        self.api = mock.Mock()
        self.api.get_current_user.side_effect = lambda: {"login": self.login}
        for patcher in (mock.patch.object(common, "PROJECT_ROOT", self.root),
                        mock.patch.dict(dev_ticket.CONFIG, {"reviewers": ["Rev1", "rev2"], "developers": ["dev1"]})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def go(self, set=None, yes=False):
        return run_with_api(dev_ticket.cmd_role, self.api, set=set, yes=yes)

    def env_text(self):
        return self.env.read_text(encoding="utf-8") if self.env.exists() else None

    def test_a_login_on_reviewers_is_a_reviewer_whatever_the_case(self):
        self.login = "rev1"  # the list says "Rev1"
        code, res, _ = self.go()
        self.assertEqual((code, res["status"], res["role"], res["source"], res["updated"]), (0, "ROLE", "reviewer", "lists", True))
        self.assertEqual(self.env_text(), "DEV_ROLE=reviewer\n")

    def test_a_login_on_developers_is_a_developer(self):
        self.login = "DEV1"
        _code, res, _ = self.go()
        self.assertEqual((res["role"], res["source"]), ("developer", "lists"))
        self.assertEqual(self.env_text(), "DEV_ROLE=developer\n")

    def test_the_lists_win_over_a_stale_remembered_role_and_the_second_run_changes_nothing(self):
        self.env.write_text("DEV_ROLE=developer\nlocal.db.connection=Server=h;Pwd=topsecret\n", encoding="utf-8")
        self.login = "rev2"
        _code, res, out = self.go()
        self.assertEqual((res["role"], res["updated"]), ("reviewer", True))
        self.assertEqual(self.env_text(), "DEV_ROLE=reviewer\nlocal.db.connection=Server=h;Pwd=topsecret\n")
        self.assertNotIn("topsecret", out)
        self.assertEqual(self.go()[1]["updated"], False)

    def test_a_login_on_both_lists_is_a_config_error_and_writes_nothing(self):
        self.login = "rev1"
        with mock.patch.dict(dev_ticket.CONFIG, {"developers": ["dev1", "REV1"]}):
            code, res, _ = self.go()
        self.assertEqual((code, res["status"]), (1, "ROLE_CONFLICT"))
        self.assertIsNone(self.env_text())

    def test_someone_on_neither_list_is_asked_once(self):
        code, res, _ = self.go()
        self.assertEqual((code, res["status"], res["role"]), (0, "ROLE_UNSET", None))
        self.assertIn("reviewers", res["message"])
        self.assertIn("developer", res["message"])
        self.assertIsNone(self.env_text())

    def test_an_unlisted_developer_is_remembered_but_an_unlisted_reviewer_is_not_accepted(self):
        self.env.write_text("DEV_ROLE=developer\n", encoding="utf-8")
        _code, res, _ = self.go()
        self.assertEqual((res["status"], res["role"], res["source"]), ("ROLE", "developer", "remembered"))
        self.env.write_text("DEV_ROLE=reviewer\n", encoding="utf-8")  # remembered, but this login is not on `reviewers`
        self.assertEqual(self.go()[1]["status"], "ROLE_UNSET")

    def test_set_previews_until_yes_for_someone_on_neither_list(self):
        code, res, _ = self.go(set="developer")
        self.assertEqual((code, res["status"]), (0, "PREVIEW"))
        self.assertIsNone(self.env_text())
        code, res, _ = self.go(set="Developer", yes=True)
        self.assertEqual((code, res["status"], res["role"]), (0, "ROLE_SET", "developer"))
        self.assertEqual(self.env_text(), "DEV_ROLE=developer\n")

    def test_set_with_yes_writes_only_that_key(self):
        self.env.write_text("local.db.connection=Server=h;Pwd=topsecret\n", encoding="utf-8")
        _code, _res, out = self.go(set="developer", yes=True)
        self.assertEqual(self.env_text(), "local.db.connection=Server=h;Pwd=topsecret\nDEV_ROLE=developer\n")
        self.assertNotIn("topsecret", out)

    def test_a_reviewer_must_be_on_the_reviewers_list(self):
        code, res, _ = self.go(set="reviewer", yes=True)
        self.assertEqual((code, res["status"]), (1, "NOT_ON_REVIEWERS"))
        self.assertIsNone(self.env_text())

    def test_a_listed_login_cannot_pick_another_role(self):
        self.login = "dev1"
        code, res, _ = self.go(set="reviewer", yes=True)
        self.assertEqual((code, res["status"], res["listed_as"]), (1, "ROLE_FIXED_BY_LIST", "developer"))
        self.login = "rev1"
        self.assertEqual(self.go(set="developer", yes=True)[1]["status"], "ROLE_FIXED_BY_LIST")
        self.assertEqual(self.go(set="reviewer", yes=True)[1]["status"], "ROLE_SET")  # the same as the list: fine
        self.assertEqual(self.env_text(), "DEV_ROLE=reviewer\n")

    def test_unknown_role_is_refused_and_nothing_is_written(self):
        code, res, _ = self.go(set="manager", yes=True)
        self.assertEqual((code, res["status"]), (1, "BAD_ROLE"))
        self.assertIsNone(self.env_text())


class ListedRoleTests(unittest.TestCase):
    def test_listed_role(self):
        with mock.patch.dict(dev_ticket.CONFIG, {"reviewers": ["Rev1"], "developers": ["dev1", "Both"]}):
            self.assertEqual(dev_ticket._listed_role("REV1"), "reviewer")
            self.assertEqual(dev_ticket._listed_role("dev1"), "developer")
            self.assertIsNone(dev_ticket._listed_role("nobody"))
            self.assertIsNone(dev_ticket._listed_role(None))
        with mock.patch.dict(dev_ticket.CONFIG, {"reviewers": ["both"], "developers": ["BOTH"]}):
            self.assertEqual(dev_ticket._listed_role("both"), "both")

    def test_the_real_config_lists_do_not_overlap(self):
        reviewers = {n.lower() for n in dev_ticket.CONFIG["reviewers"]}
        developers = {n.lower() for n in dev_ticket.CONFIG["developers"]}
        self.assertEqual(reviewers & developers, set())
        self.assertTrue(reviewers and developers)


class RememberedRoleTests(unittest.TestCase):
    def test_fetch_gets_the_remembered_role_or_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "AI_DEV_ENV").mkdir()
            with mock.patch.object(common, "PROJECT_ROOT", root):
                self.assertIsNone(dev_ticket._remembered_role())
                (root / "AI_DEV_ENV" / "credentials.env").write_text("DEV_ROLE=developer\n", encoding="utf-8")
                self.assertEqual(dev_ticket._remembered_role(), "developer")


class NotAReviewerTests(unittest.TestCase):
    """A developer never moves a ticket past Code Review and never triggers a deploy (Jenkins credentials are the
    real gate; this gives a clear message first)."""

    def test_state_role_decides(self):
        self.assertIsNotNone(dev_ticket.developer_problem({"role": "developer"}))
        with mock.patch.object(common, "_remembered_role", return_value="developer"):
            self.assertIsNone(dev_ticket.developer_problem({"role": "reviewer"}))

    def test_missing_state_role_falls_back_to_the_remembered_role(self):
        with mock.patch.object(common, "_remembered_role", return_value="developer"):
            self.assertIsNotNone(dev_ticket.developer_problem({}))
        with mock.patch.object(common, "_remembered_role", return_value="reviewer"):
            self.assertIsNone(dev_ticket.developer_problem({}))
        with mock.patch.object(common, "_remembered_role", return_value=None):
            self.assertIsNone(dev_ticket.developer_problem({}))

    def test_status_moves_past_code_review_are_refused_for_a_developer(self):
        with mock.patch.object(common, "load_state", return_value=({"role": "developer"}, Path("state.json"))):
            for target in (dev_ticket.STATUS["done"], dev_ticket.STATUS["staging"]):
                status, fields = dev_ticket.status_guard(mock.Mock(), "1", {}, target)
                self.assertEqual(status, "NOT_A_REVIEWER")
                self.assertIn("message", fields)

    def test_a_reviewer_is_not_stopped_by_the_role_guard(self):
        with mock.patch.object(common, "load_state", return_value=({"role": "reviewer"}, Path("state.json"))), \
                mock.patch.object(common, "push_state", return_value={"status": "PUSHED"}):
            self.assertIsNone(dev_ticket.status_guard(mock.Mock(), "1", {}, dev_ticket.STATUS["staging"]))

    def test_a_reviewers_own_ticket_is_not_a_review_folder(self):
        # the role (who runs the workflow) and the kind (a review of someone else's ticket) are different things
        own = ({"role": "reviewer", "stage": "done"}, Path("state.json"))
        review = ({"role": "reviewer", "kind": "review", "stage": "done"}, Path("state.json"))
        with mock.patch.object(common, "push_state", return_value={"status": "PUSHED"}):
            with mock.patch.object(common, "load_state", return_value=own):
                for target in (dev_ticket.STATUS["code_review"], dev_ticket.STATUS["staging"]):
                    self.assertIsNone(dev_ticket.status_guard(mock.Mock(), "1", {}, target), target)
            with mock.patch.object(common, "load_state", return_value=review):
                status, _fields = dev_ticket.status_guard(mock.Mock(), "1", {}, dev_ticket.STATUS["staging"])
        self.assertEqual(status, "REVIEW_FOLDER")

    def test_deploy_is_refused_for_a_developer_before_anything_else(self):
        import staging_deploy
        api = mock.Mock()
        api.get_ticket.return_value = {"number": 1}
        with mock.patch.object(staging_deploy, "load_state", return_value=({"role": "developer"}, Path("state.json"))), \
                mock.patch.object(staging_deploy, "is_db_node", return_value=False):
            code, res, _ = run_with_api(staging_deploy._deploy, api, ticket="1", requested=True, yes=True)
        self.assertEqual(res["status"], "NOT_A_REVIEWER")
        self.assertEqual(code, 0)  # a decided "stop and ask" outcome, like NOT_HANDED_OFF

    def test_db_ticket_deploy_checks_the_owners_role(self):
        import staging_deploy
        api = mock.Mock()
        api.get_ticket.return_value = {"number": 2}
        owner = ("1", {"role": "developer"}, Path("state.json"))
        with mock.patch.object(staging_deploy, "owner_of", return_value=owner), \
                mock.patch.object(staging_deploy, "is_db_node", return_value=True):
            _code, res, _ = run_with_api(staging_deploy._deploy, api, ticket="2", requested=True, yes=True)
        self.assertEqual(res["status"], "NOT_A_REVIEWER")


SUMMARY = ("*Ticket #100: Fix the save button*\n*Impact Analysis Report*\n* Saving a node now works.\n"
           "*Approach Taken*\n* Changed the save handler.\n")


def good_state(**over):
    state = {"ticket": "100", "type": "CR", "branch": "FB_100", "role": "developer", "stage": "7-verify",
             "risk": "MEDIUM", "path": "STANDARD",
             "risk_check": {"min_tier": "MEDIUM", "reasons": ["MEDIUM: 160 changed lines > 150"], "files": 2},
             "gates": {"status": "PASS", "at": "2026-10-03T10:00:00", "fingerprint": "fp1",
                       "files": ["c2mwf_admin/src/a.ts", "c2mwf_admin/src/a.spec.ts"]},
             "functional_mode": "run",
             "functional": {"status": "PASS", "passed": 3, "failed": 0, "planned": 3, "profile": "staging",
                            "fingerprint": "fp1"},
             "baseline": {"status": "PASS"}}
    state.update(over)
    return state


class HandoffFormatTests(unittest.TestCase):
    def payload(self, **over):
        return dev_ticket.handoff_payload(good_state(**over), ticket="100", author="developer1", head="a" * 40,
                                          db_head=None, readme_impact="none")

    def test_payload_carries_the_facts_from_state(self):
        p = self.payload()
        self.assertEqual((p["ticket"], p["author"], p["branch"], p["head"], p["round"]),
                         ("100", "developer1", "FB_100", "a" * 40, 1))
        self.assertEqual((p["risk"], p["risk_check"]["min_tier"]), ("MEDIUM", "MEDIUM"))
        self.assertEqual((p["gates"]["result"], p["gates"]["files"]), ("PASS", good_state()["gates"]["files"]))
        self.assertEqual((p["functional"]["mode"], p["functional"]["passed"], p["functional"]["failed"]), ("run", 3, 0))
        self.assertEqual(p["baseline"], "PASS")
        self.assertIsNone(p["db"])

    def test_skipped_functional_checks_show_the_reason(self):
        p = self.payload(functional_mode="skip", functional=None, functional_skip={"reason": "no UI path"})
        self.assertEqual((p["functional"]["mode"], p["functional"]["skip_reason"]), ("skip", "no UI path"))

    def test_db_part_is_included_when_the_ticket_has_db_files(self):
        p = dev_ticket.handoff_payload(good_state(db={"ticket": 200, "files": ["Database/C2M/SP/x.sql"]}),
                                       ticket="100", author="j", head="a" * 40, db_head="b" * 40, readme_impact="none")
        self.assertEqual(p["db"], {"ticket": 200, "branch": "FB_200", "head": "b" * 40,
                                   "files": ["Database/C2M/SP/x.sql"]})

    def test_accepted_baseline_failure_is_visible(self):
        self.assertEqual(self.payload(baseline={"status": "BASELINE_FAIL", "accepted": True})["baseline"], "accepted")
        self.assertEqual(self.payload(baseline=None)["baseline"], "n/a")

    def test_round_comes_from_state(self):
        self.assertEqual(self.payload(round=2)["round"], 2)

    def test_render_then_parse_round_trips_even_with_angle_brackets(self):
        p = self.payload()
        p["risk_check"]["reasons"] = ["MEDIUM: 200 changed lines > 150 <b>"]
        text = dev_ticket.render_handoff(p)
        self.assertNotIn(">", text.split("HANDOFF-JSON-BEGIN")[1])  # Assembla reads < and > as HTML
        self.assertEqual(dev_ticket.parse_handoff(text), p)

    def test_render_is_assembla_wiki_markup_without_local_paths_or_secrets(self):
        text = dev_ticket.render_handoff(self.payload())
        human = text.split("HANDOFF-JSON-BEGIN")[0]
        self.assertTrue(human.startswith("*Hand-off #100 (round 1)"))
        self.assertNotIn("`", human)
        self.assertNotIn("**", human)
        self.assertNotRegex(text, r"[A-Za-z]:\\")
        self.assertTrue(all(ln.startswith("* ") or ln.startswith("*Hand-off") for ln in human.strip().splitlines()))

    def test_parse_returns_none_without_a_block_or_with_bad_json(self):
        self.assertIsNone(dev_ticket.parse_handoff("just a comment"))
        self.assertIsNone(dev_ticket.parse_handoff("HANDOFF-JSON-BEGIN\n{nope\nHANDOFF-JSON-END"))


class HandoffCommandTests(unittest.TestCase):
    def setUp(self):
        import local_gates
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.folder = self.root / "DEV_TICKETS" / "100"
        self.folder.mkdir(parents=True)
        (self.folder / "SUMMARY.md").write_text(SUMMARY, encoding="utf-8")
        self.write_state(good_state())
        self.api = mock.Mock()
        self.api.get_ticket.return_value = {}
        self.api.get_current_user.return_value = {"id": "u1", "login": "developer1"}
        self.api.add_comment.return_value = 555
        self.api.get_comments.return_value = []
        git_out = mock.Mock(stdout="a" * 40 + "\n")
        for name, patcher in {
            "root": mock.patch.object(common, "PROJECT_ROOT", self.root),
            "push": mock.patch.object(common, "push_state", return_value={"status": "PUSHED"}),
            "git": mock.patch.object(common, "git", return_value=git_out),
            "fp": mock.patch.object(local_gates, "diff_fingerprint", return_value="fp1"),
            "files": mock.patch.object(local_gates, "changed_files", return_value=["c2mwf_admin/src/a.ts"]),
        }.items():
            patcher.start()
            self.addCleanup(patcher.stop)

    def write_state(self, state):
        (self.folder / "state.json").write_text(json.dumps(state), encoding="utf-8")

    def state(self):
        return json.loads((self.folder / "state.json").read_text(encoding="utf-8"))

    def go(self, yes=False):
        return run_with_api(dev_ticket.cmd_handoff, self.api, ticket="100", yes=yes)

    def test_preview_shows_the_comment_and_posts_nothing(self):
        code, res, out = self.go()
        self.assertEqual((code, res["status"]), (0, "PREVIEW"))
        self.assertIn("HANDOFF-JSON-BEGIN", out)
        self.api.add_comment.assert_not_called()
        self.assertTrue((self.folder / "HANDOFF.md").is_file())
        self.assertNotIn("handoff", self.state())

    def test_the_preview_returns_a_confirm_hash(self):
        _code, res, _ = self.go()
        self.assertRegex(res["confirm"], r"^[0-9a-f]{8}$")

    def test_yes_without_confirm_posts_nothing(self):
        code, res, _ = run_with_api(dev_ticket.cmd_handoff, self.api, ticket="100", yes=True, confirm=None)
        self.assertEqual((code, res["status"]), (1, "CONFIRM_REQUIRED"))
        self.api.add_comment.assert_not_called()
        self.assertNotIn("handoff", self.state())

    def test_yes_with_another_hash_posts_nothing(self):
        _code, res, _ = run_with_api(dev_ticket.cmd_handoff, self.api, ticket="100", yes=True, confirm="deadbeef")
        self.assertEqual(res["status"], "PREVIEW_CHANGED")
        self.api.add_comment.assert_not_called()

    def test_a_hash_from_an_earlier_state_is_refused_after_the_evidence_changed(self):
        _code, res, _ = self.go()
        state = self.state()
        state["gates"]["at"] = "2026-10-04T09:00:00"  # the gates were re-run since the preview: the comment text differs
        (self.folder / "state.json").write_text(json.dumps(state), encoding="utf-8")
        _code, refused, _ = run_with_api(dev_ticket.cmd_handoff, self.api, ticket="100", yes=True, confirm=res["confirm"])
        self.assertEqual(refused["status"], "PREVIEW_CHANGED")
        self.api.add_comment.assert_not_called()

    def test_yes_posts_once_and_remembers_the_comment(self):
        _code, res, _ = self.go(yes=True)
        self.assertEqual((res["status"], res["comment_id"]), ("POSTED", 555))
        saved = self.state()["handoff"]
        self.assertEqual((saved["comment_id"], saved["head"], saved["round"], saved["author"]),
                         (555, "a" * 40, 1, "developer1"))
        posted = self.api.add_comment.call_args.args[1]
        self.assertEqual(dev_ticket.parse_handoff(posted)["head"], "a" * 40)

    def test_second_run_edits_the_same_comment_in_place(self):
        self.go(yes=True)
        self.api.get_comments.return_value = [{"id": 555, "user_id": "u1"}]
        _code, res, _ = self.go(yes=True)
        self.assertEqual((res["status"], res["comment_id"]), ("EDITED", 555))
        self.api.edit_comment.assert_called_once()
        self.assertEqual(self.api.add_comment.call_count, 1)

    def test_someone_elses_comment_is_never_edited(self):
        self.write_state(good_state(handoff={"comment_id": 555, "head": "x", "round": 1}))
        self.api.get_comments.return_value = [{"id": 555, "user_id": "other"}]
        _code, res, _ = self.go(yes=True)
        self.assertEqual(res["status"], "NOT_YOUR_COMMENT")
        self.api.edit_comment.assert_not_called()

    def test_refuses_without_a_work_folder(self):
        code, res, _ = run_with_api(dev_ticket.cmd_handoff, self.api, ticket="999", yes=True)
        self.assertEqual((code, res["status"]), (1, "NO_STATE"))

    def test_refuses_unpushed_code(self):
        with mock.patch.object(common, "push_state", return_value={"status": "NOT_PUSHED", "branch": "FB_100"}):
            _code, res, _ = self.go(yes=True)
        self.assertEqual(res["status"], "NOT_PUSHED")
        self.api.add_comment.assert_not_called()

    def test_refuses_an_unpushed_db_branch(self):
        self.write_state(good_state(db={"ticket": 200, "files": ["Database/C2M/SP/x.sql"]}))
        pushes = {"100": {"status": "PUSHED"}, "200": {"status": "NOT_PUSHED", "branch": "FB_200"}}
        with mock.patch.object(common, "push_state", side_effect=lambda n, repo=None: pushes[n]):
            _code, res, _ = self.go(yes=True)
        self.assertEqual((res["status"], res.get("repo")), ("NOT_PUSHED", "db"))

    def test_refuses_without_a_passing_gates_run(self):
        self.write_state(good_state(gates={"status": "FAIL", "fingerprint": "fp1"}))
        _code, res, _ = self.go(yes=True)
        self.assertEqual(res["status"], "GATES_REQUIRED")

    def test_refuses_stale_gates(self):
        self.write_state(good_state(gates={"status": "PASS", "fingerprint": "old", "files": []}))
        _code, res, _ = self.go(yes=True)
        self.assertEqual(res["status"], "GATES_STALE")

    def test_refuses_without_a_functional_choice(self):
        self.write_state(good_state(functional_mode=None, functional=None))
        _code, res, _ = self.go(yes=True)
        self.assertEqual(res["status"], "FUNCTIONAL_MODE_REQUIRED")

    def test_refuses_a_missing_or_invalid_summary(self):
        (self.folder / "SUMMARY.md").write_text("# not wiki markup\n", encoding="utf-8")
        _code, res, _ = self.go(yes=True)
        self.assertEqual(res["status"], "SUMMARY_CHECK_FAILED")
        (self.folder / "SUMMARY.md").unlink()
        _code, res, _ = self.go(yes=True)
        self.assertEqual(res["status"], "NOT_FOUND")

    def test_readme_impact_is_taken_from_the_brief(self):
        (self.folder / "BRIEF.md").write_text("# 100\n## Approach\nx\n## README impact\n"
                                              "c2mwf_admin/README.md, Routes section\n## Assumptions\n", encoding="utf-8")
        self.go(yes=True)
        posted = dev_ticket.parse_handoff(self.api.add_comment.call_args.args[1])
        self.assertEqual(posted["readme_impact"], "c2mwf_admin/README.md, Routes section")


class AssignTests(unittest.TestCase):
    """`dt assign`: the developer hands the ticket to a reviewer from the configured list, after the HANDOFF is posted."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.folder = self.root / "DEV_TICKETS" / "100"
        self.folder.mkdir(parents=True)
        self.write_state(good_state(handoff={"comment_id": 555, "head": "a" * 40, "round": 1, "author": "developer1"}))
        self.api = mock.Mock()
        self.api.get_ticket.return_value = {"status": "Code Review", "assigned_to_id": "u1"}
        self.api.get_current_user.return_value = {"id": "u1", "login": "developer1"}
        self.api.get_space_users.return_value = [
            {"id": "u1", "login": "developer1", "name": "Jun Ior"},
            {"id": "s1", "login": "Reviewer1", "name": "Sam Reviewer"},
            {"id": "s2", "login": "reviewer2", "name": "Sue Reviewer"}]
        self.api.get_comments.return_value = [{"id": 555, "user_id": "u1"}]
        for patcher in (mock.patch.object(common, "PROJECT_ROOT", self.root),
                        mock.patch.dict(dev_ticket.CONFIG, {"reviewers": ["reviewer1", "reviewer2", "ghost"]}),
                        mock.patch.object(common, "push_state", return_value={"status": "PUSHED"}),
                        mock.patch.object(common, "git", return_value=mock.Mock(stdout="a" * 40 + "\n"))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def write_state(self, state):
        (self.folder / "state.json").write_text(json.dumps(state), encoding="utf-8")

    def state(self):
        return json.loads((self.folder / "state.json").read_text(encoding="utf-8"))

    def go(self, to=None, yes=False, list_=False):
        return run_with_api(dev_ticket.cmd_assign, self.api, ticket="100", to=to, yes=yes, list=list_)

    def test_list_shows_login_and_name_but_no_email_or_id(self):
        _code, res, out = self.go(list_=True)
        self.assertEqual(res["status"], "REVIEWERS")
        self.assertEqual(res["reviewers"][0], {"login": "reviewer1", "name": "Sam Reviewer"})
        self.assertEqual(res["reviewers"][2], {"login": "ghost", "name": None, "note": "not found in the space"})
        self.assertNotIn("s1", json.dumps(res["reviewers"]))

    def test_empty_reviewer_list_is_refused_cleanly(self):
        with mock.patch.dict(dev_ticket.CONFIG, {"reviewers": []}):
            for kwargs in ({"list_": True}, {"to": "reviewer1", "yes": True}):
                _code, res, _ = self.go(**kwargs)
                self.assertEqual(res["status"], "REVIEWERS_NOT_CONFIGURED")

    def test_needs_list_or_to(self):
        code, res, _ = self.go()
        self.assertEqual((code, res["status"]), (1, "BAD_ARG"))

    def test_refuses_without_a_work_folder(self):
        code, res, _ = run_with_api(dev_ticket.cmd_assign, self.api, ticket="999", to="reviewer1", yes=True, list=False)
        self.assertEqual((code, res["status"]), (1, "NO_STATE"))

    def test_refuses_a_ticket_not_in_code_review(self):
        self.api.get_ticket.return_value = {"status": "Dev In Process", "assigned_to_id": "u1"}
        _code, res, _ = self.go(to="reviewer1", yes=True)
        self.assertEqual(res["status"], "WRONG_STATUS")
        self.api.assign_ticket.assert_not_called()

    def test_refuses_a_login_that_is_not_a_reviewer(self):
        _code, res, _ = self.go(to="developer1", yes=True)
        self.assertEqual(res["status"], "NOT_A_REVIEWER")
        self.assertEqual(res["reviewers"], ["reviewer1", "reviewer2", "ghost"])
        self.api.assign_ticket.assert_not_called()

    def test_refuses_assigning_to_yourself(self):
        with mock.patch.dict(dev_ticket.CONFIG, {"reviewers": ["developer1"]}):
            _code, res, _ = self.go(to="developer1", yes=True)
        self.assertEqual(res["status"], "SELF_ASSIGN")

    def test_refuses_without_a_posted_handoff(self):
        self.write_state(good_state())
        _code, res, _ = self.go(to="reviewer1", yes=True)
        self.assertEqual(res["status"], "NO_HANDOFF")
        self.write_state(good_state(handoff={"comment_id": 555, "head": "a" * 40, "round": 1}))
        self.api.get_comments.return_value = []  # the comment was deleted on Assembla
        _code, res, _ = self.go(to="reviewer1", yes=True)
        self.assertEqual(res["status"], "NO_HANDOFF")

    def test_refuses_when_the_code_moved_after_the_handoff(self):
        with mock.patch.object(common, "git", return_value=mock.Mock(stdout="b" * 40 + "\n")):
            _code, res, _ = self.go(to="reviewer1", yes=True)
        self.assertEqual(res["status"], "STALE_HANDOFF")
        self.api.assign_ticket.assert_not_called()

    def test_a_documentation_only_move_after_the_handoff_can_still_be_assigned(self):
        with mock.patch.object(common, "git", return_value=mock.Mock(stdout="b" * 40 + "\n")), \
                mock.patch.object(review, "docs_only_delta", return_value=True):
            _code, res, _ = self.go(to="reviewer1", yes=True)
        self.assertEqual(res["status"], "ASSIGNED")

    def test_refuses_unpushed_code(self):
        with mock.patch.object(common, "push_state", return_value={"status": "NOT_PUSHED", "branch": "FB_100"}):
            _code, res, _ = self.go(to="reviewer1", yes=True)
        self.assertEqual(res["status"], "NOT_PUSHED")

    def test_refuses_a_reviewer_missing_from_the_space(self):
        _code, res, _ = self.go(to="ghost", yes=True)
        self.assertEqual(res["status"], "REVIEWER_NOT_FOUND")

    def test_preview_changes_nothing(self):
        _code, res, _ = self.go(to="reviewer1")
        self.assertEqual((res["status"], res["reviewer"]), ("PREVIEW", "reviewer1"))
        self.api.assign_ticket.assert_not_called()

    def test_yes_assigns_with_the_users_id_matching_the_login_in_any_case(self):
        _code, res, _ = self.go(to="REVIEWER1", yes=True)
        self.assertEqual((res["status"], res["reviewer"]), ("ASSIGNED", "reviewer1"))
        self.api.assign_ticket.assert_called_once_with("100", "s1")
        self.assertEqual(self.state()["handoff"]["reviewer"], "reviewer1")

    def test_already_assigned_is_not_written_again(self):
        self.api.get_ticket.return_value = {"status": "Code Review", "assigned_to_id": "s1"}
        _code, res, _ = self.go(to="reviewer1", yes=True)
        self.assertEqual(res["status"], "ALREADY_ASSIGNED")
        self.api.assign_ticket.assert_not_called()


class AssignApiTests(unittest.TestCase):
    def api(self, assignee_after):
        from assembla_api import AssemblaApi
        api = AssemblaApi.__new__(AssemblaApi)  # no credentials needed: the HTTP layer is mocked
        api.update_ticket = mock.Mock()
        api.get_ticket = mock.Mock(return_value={"assigned_to_id": assignee_after})
        return api

    def test_assign_puts_the_assignee_and_rereads(self):
        api = self.api("s1")
        api.assign_ticket("100", "s1")
        api.update_ticket.assert_called_once_with("100", {"assigned_to_id": "s1"})

    def test_an_ignored_assignment_is_an_error_not_a_success(self):
        from assembla_api import AssemblaError
        with self.assertRaises(AssemblaError):
            self.api("u1").assign_ticket("100", "s1")


class StateRoleTests(unittest.TestCase):
    def test_role_is_a_validated_state_key(self):
        check = dev_ticket.STATE_FIELDS["role"]
        self.assertTrue(check("developer"))
        self.assertTrue(check("reviewer"))
        self.assertFalse(check("manager"))
        self.assertFalse(check(""))


class ConfigTests(unittest.TestCase):
    def test_roles_and_reviewers_are_configured(self):
        self.assertEqual(dev_ticket.CONFIG["roles"]["values"], ROLES)
        self.assertEqual(dev_ticket.CONFIG["roles"]["env_key"], "DEV_ROLE")
        self.assertIsInstance(dev_ticket.CONFIG["reviewers"], list)


if __name__ == "__main__":
    unittest.main()
