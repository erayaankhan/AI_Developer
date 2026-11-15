"""Team-profile support added for the Launchpad repos: config layering, repo detection, the per-repo Node check, the
status meanings (dev_done / approved / rejected), the DB ticket found through a requirement ticket, and the flat DB
layout. Without a profile the engine must behave exactly as before (the rest of the suite checks that).
Nothing here calls Assembla or touches a real repo: the API is mocked and git runs only in temporary folders."""
import contextlib
import importlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import config_loader
import dev_ticket
from develop_ticket import common, db
import test_ticket_flow_cmds as flow  # module import: its test classes must not be collected twice

STATUS = dev_ticket.STATUS


@contextlib.contextmanager
def chdir(path):
    old = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(old)


def git_repo(folder: Path, origin: str) -> Path:
    subprocess.run(["git", "init", "-q", str(folder)], check=True)
    subprocess.run(["git", "-C", str(folder), "remote", "add", "origin", origin], check=True)
    return folder


class ConfigLayeringTests(unittest.TestCase):
    def load(self, profile, cwd):
        with mock.patch.dict(os.environ, {"DEV_TICKET_PROFILE": profile}), chdir(cwd):
            return config_loader.load_config()

    def test_no_profile_keeps_the_engine_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self.load("none", tmp)
        self.assertNotIn("profile", config)
        self.assertEqual(config["project_root"], "../..")
        self.assertNotIn("approved", config["statuses"])

    def test_the_repo_overlay_follows_the_origin_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = git_repo(Path(tmp) / "anything", "git@git.assembla.com:Plasma-Portfolio/mai-dc.git")
            config = self.load("launchpad", repo)
            with chdir(repo):
                self.assertEqual(config_loader.resolve_project_root(dict(config)), repo.resolve())
        self.assertEqual((config["profile"], config["repo_key"], config["repo_node"]), ("launchpad", "mai-dc", "MAI-DC"))
        self.assertEqual(config["deploy_attributes"]["node_value"], "MAI-DC")
        self.assertIn("launchpad-admin-portal/frontend", config["gates"]["angular_projects"])
        self.assertEqual(config["statuses"]["approved"], "DevOps Staging")

    def test_mai_dc_quality_apps_follow_their_jenkins_jobs(self):
        import quality
        with tempfile.TemporaryDirectory() as tmp:
            config = self.load("launchpad", git_repo(Path(tmp) / "x", "git@git.assembla.com:Plasma-Portfolio/mai-dc.git"))
        apps = quality.apps(config)
        self.assertEqual(len(apps), 9)
        self.assertNotIn("launchpad-admin-portal", apps)  # no Sonar / Snyk in its job
        self.assertEqual(apps["line-management-mfe"]["sonar"]["scanner"], "cli")
        self.assertEqual(apps["timelineretrieval"]["sonar"]["scanner"], "maven")
        self.assertEqual(apps["launchpad-api"]["snyk"]["severity_threshold"], "high")
        self.assertEqual(apps["opc-comm"]["snyk"]["severity_threshold"], "low")
        self.assertEqual(quality.touched_apps(config, ["opc-comm/src/main/java/A.java", "README.md"]), ["opc-comm"])

    def test_the_db_repo_is_not_mistaken_for_the_cmgmt_code_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = git_repo(Path(tmp) / "db", "git@git.assembla.com:Plasma-Portfolio/cmgmt-db.git")
            config = self.load("launchpad", repo)
        self.assertEqual(config["repo_key"], "")
        self.assertNotIn("repo_node", config)

    def test_an_unknown_profile_stops_with_its_name(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(SystemExit) as stop:
            self.load("nosuchteam", tmp)
        self.assertIn("nosuchteam", str(stop.exception))

    def test_auto_root_outside_a_git_repo_stops(self):
        with tempfile.TemporaryDirectory() as tmp, chdir(tmp), self.assertRaises(SystemExit):
            config_loader.resolve_project_root({"project_root": "auto"})


class RepoNodeTests(unittest.TestCase):
    PROFILE = {"repo_node": "CMGMT",
               "repos": {"cmgmt": {"folder": "x/CMGMT", "config": {"repo_node": "CMGMT"}},
                         "mai-dc": {"folder": "x/MAI-DC", "config": {"repo_node": "MAI-DC"}}}}

    def ticket(self, node):
        return {"number": 7, "custom_fields": {common.CONFIG["deploy_attributes"]["node_field"]: node}}

    def test_without_a_repo_node_every_ticket_is_accepted(self):
        self.assertIsNone(common.repo_node_problem(self.ticket("anything")))

    def test_this_repos_node_is_accepted(self):
        with mock.patch.dict(common.CONFIG, self.PROFILE):
            self.assertIsNone(common.repo_node_problem(self.ticket("CMGMT")))

    def test_another_repos_ticket_names_that_repo(self):
        with mock.patch.dict(common.CONFIG, self.PROFILE):
            status, message = common.repo_node_problem(self.ticket("MAI-DC"))
        self.assertEqual(status, "WRONG_REPO")
        self.assertIn("x/MAI-DC", message)

    def test_a_requirement_ticket_is_not_a_node_ticket(self):
        with mock.patch.dict(common.CONFIG, self.PROFILE):
            self.assertEqual(common.repo_node_problem(self.ticket("-NA-"))[0], "NOT_A_NODE_TICKET")

    def test_a_db_ticket_is_worked_from_its_code_ticket(self):
        with mock.patch.dict(common.CONFIG, self.PROFILE):
            self.assertEqual(common.repo_node_problem(self.ticket(common.CONFIG["db"]["node_value"]))[0], "DB_TICKET")


class StatusMeaningTests(unittest.TestCase):
    def test_defaults_are_the_original_meanings(self):
        self.assertEqual(common.approved_status(), STATUS["done"])
        self.assertEqual(common.rejected_status(), STATUS["queue"])
        self.assertIsNone(common.dev_done_status())

    def test_a_profile_can_change_them(self):
        with mock.patch.dict(STATUS, {"approved": "Staging X", "rejected": "Back X", "dev_done": "Done X"}):
            self.assertEqual((common.approved_status(), common.rejected_status(), common.dev_done_status()),
                             ("Staging X", "Back X", "Done X"))


class DevDoneHopTests(flow.StatusTests):
    """With statuses.dev_done the hand-off to code review passes through it; the DB ticket follows both steps."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(STATUS, {"dev_done": "Dev Done X"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_preview_names_the_intermediate_status(self):
        _code, res = self.status(STATUS["code_review"])
        self.assertEqual((res["status"], res["via"]), ("PREVIEW", "Dev Done X"))
        self.api.update_status.assert_not_called()

    def test_the_hand_off_moves_through_dev_done(self):
        _code, res = self.status(STATUS["code_review"], yes=True)
        self.assertEqual((res["status"], res["via"], res["current"]), ("UPDATED", "Dev Done X", STATUS["code_review"]))
        self.assertEqual(self.api.update_status.call_args_list,
                         [mock.call("100", "Dev Done X"), mock.call("100", STATUS["code_review"])])

    def test_the_db_ticket_follows_both_steps(self):
        self.make_state(stage="8-handoff", role="developer", db={"ticket": 200, "files": ["a.sql"]})
        self.other["200"] = {"number": 200, "status": STATUS["in_process"]}
        self.api.update_status.side_effect = lambda n, s: self.other.get(str(n), {}).update(status=s)
        _code, res = self.status(STATUS["code_review"], yes=True)
        self.assertEqual(res["db_companion"], {"db_ticket": 200, "moved": True, "to": STATUS["code_review"]})
        self.assertEqual(self.api.update_status.call_args_list,
                         [mock.call("100", "Dev Done X"), mock.call(200, "Dev Done X"),
                          mock.call("100", STATUS["code_review"]), mock.call(200, STATUS["code_review"])])


# Reuse StatusTests' fixture only: its own tests expect the default single move, so they are not re-run here.
for _name in [n for n in dir(flow.StatusTests) if n.startswith("test_") and n not in DevDoneHopTests.__dict__]:
    setattr(DevDoneHopTests, _name, None)


class PickFromQueueTests(flow.PickTests):
    """pick_from_queue: with nothing in progress, the queue is offered; a queue ticket can be picked by number; the move
    to in-process happens later (after the branch), never in pick."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(dev_ticket.CONFIG, {"pick_from_queue": True})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_single_queue_ticket_is_picked_and_marked_to_start(self):
        self.api.get_my_active_tickets.return_value = [flow.ticket(7, STATUS["queue"])]
        _code, res = self.pick()
        self.assertEqual((res["status"], res["ticket"], res["start_needed"]), ("PICKED", 7, True))
        self.api.update_status.assert_not_called()

    def test_several_queue_tickets_ask_which_one(self):
        self.api.get_my_active_tickets.return_value = [flow.ticket(9, STATUS["queue"]), flow.ticket(7, STATUS["queue"])]
        _code, res = self.pick()
        self.assertEqual(res["status"], "CHOOSE_FROM_QUEUE")
        self.assertEqual([c["ticket"] for c in res["candidates"]], [7, 9])

    def test_a_ticket_already_in_process_is_resumed_before_the_queue(self):
        self.api.get_my_active_tickets.return_value = [flow.ticket(7, STATUS["queue"]), flow.ticket(8)]
        _code, res = self.pick()
        self.assertEqual((res["status"], res["ticket"]), ("PICKED", 8))
        self.assertNotIn("start_needed", res)

    def test_a_queue_ticket_by_number_is_accepted(self):
        self.api.get_ticket.return_value = flow.ticket(100, STATUS["queue"])
        _code, res = self.pick(ticket="100")
        self.assertEqual((res["status"], res["start_needed"]), ("PICKED", True))

    def test_a_queue_ticket_while_another_is_in_process_is_refused(self):
        self.api.get_my_active_tickets.return_value = [flow.ticket(8)]
        self.api.get_ticket.return_value = flow.ticket(100, STATUS["queue"])
        _code, res = self.pick(ticket="100")
        self.assertEqual(res["status"], "OTHER_IN_PROCESS")
        self.assertIn("pause 8", res["message"])


for _name in [n for n in dir(flow.PickTests) if n.startswith("test_") and n not in PickFromQueueTests.__dict__]:
    setattr(PickFromQueueTests, _name, None)  # the base tests expect the default (no queue picking)


class DbTicketViaRequirementTests(unittest.TestCase):
    """#10 (code) and #11 (DB) are not linked to each other, only to the requirement ticket #9 (Node -NA-)."""

    def setUp(self):
        node = common.CONFIG["deploy_attributes"]["node_field"]
        dbn = common.CONFIG["db"]["node_value"]
        self.tickets = {
            10: {"id": "t10", "number": 10, "status": "Dev in Process", "custom_fields": {node: "CMGMT"}},
            9: {"id": "t9", "number": 9, "status": "Dev in Process", "custom_fields": {node: "-NA-"}},
            11: {"id": "t11", "number": 11, "status": "Dev Queue", "custom_fields": {node: dbn}, "hierarchy_type": 2},
        }
        by_id = {t["id"]: t for t in self.tickets.values()}
        links = {10: [("t10", "t9")], 9: [("t9", "t10"), ("t9", "t11")], 11: [("t11", "t9")]}
        self.api = mock.Mock()
        self.api.get_ticket.side_effect = lambda n: self.tickets[int(n)]
        self.api.get_ticket_by_id.side_effect = lambda i: by_id[i]
        self.api.get_associations.side_effect = lambda n: [
            {"relationship": common.RELATED, "ticket1_id": a, "ticket2_id": b} for a, b in links[int(n)]]
        self.api.user_label.return_value = "someone"

    def test_without_the_setting_it_is_not_found(self):
        self.assertEqual(db.find_db_ticket(self.api, "10")["status"], "NO_DB_TICKET")

    def test_with_the_setting_it_is_found_through_the_requirement(self):
        with mock.patch.dict(common.CONFIG["db"], {"find_via_requirement": True}):
            info = db.find_db_ticket(self.api, "10")
        self.assertEqual((info["status"], info["db_ticket"]), ("FOUND", 11))
        self.assertEqual(info["candidates"][0]["via_requirement"], 9)


class FlatDbLayoutTests(unittest.TestCase):
    def reload(self, **db_settings):
        import local_gates
        with mock.patch.dict(local_gates.CONFIG["db"], db_settings):
            import db_checks
            return importlib.reload(db_checks)

    def tearDown(self):
        self.reload()  # back to the configured layout for the other tests

    def test_the_folder_layout_is_the_default(self):
        db_checks = self.reload()
        self.assertFalse(db_checks.FLAT)
        self.assertIsNone(db_checks.FOLDER_RE.match("Database/C2M/LAUNCH-1-x.sql"))

    def test_the_flat_layout_takes_scripts_directly_under_the_schema(self):
        db_checks = self.reload(layout="flat")
        m = db_checks.FOLDER_RE.match("Database/C2M/LAUNCH-4263-mother-workspace.sql")
        self.assertEqual((m.group("schema"), m.group("kind"), m.group("stem")),
                         ("C2M", "", "LAUNCH-4263-mother-workspace"))
        self.assertIsNone(db_checks.FOLDER_RE.match("Database/C2M/sub/x.sql"))

    def test_a_flat_script_gets_the_safety_checks_but_not_the_naming_rules(self):
        db_checks = self.reload(layout="flat")
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "Database" / "C2M").mkdir(parents=True)
            (Path(tmp) / "Database" / "C2M" / "anything.sql").write_text("DROP TABLE users;\n", encoding="utf-8")
            out = {"errors": [], "warnings": [], "stops": []}
            with mock.patch.object(db_checks, "DB_REPO", Path(tmp)), \
                    mock.patch.object(db_checks, "exists_at", return_value=False):
                db_checks.check_file("Database/C2M/anything.sql", "11", set(), "base", out)
        self.assertEqual(out["errors"], [])
        self.assertTrue(any("DROP TABLE" in s for s in out["stops"]))


if __name__ == "__main__":
    unittest.main()
