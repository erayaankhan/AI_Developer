"""`dt db-create`, `dt db-files` and `dt rca` (improvements item 23). The Assembla API, the DB repo helpers and the
file system root are mocked; nothing is created, read from git or posted anywhere."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import db_checks
from confirm_support import preview_hash
import dev_ticket
from assembla_api import AssemblaError
from develop_ticket import common

CFG = dev_ticket.CONFIG
ATTRS = CFG["deploy_attributes"]
DB_NODE = CFG["db"]["node_value"]
RELATED = common.RELATED
INACTIVE = sorted(CFG["epics"]["inactive_statuses"])[0]
TITLE = "DB | #100 add column to Foo"


def run(command, api, **kwargs):
    out = io.StringIO()
    args = SimpleNamespace(ticket="100", yes=False)
    vars(args).update(kwargs)
    if args.yes and not hasattr(args, "confirm"):  # as the workflow does: preview first, then apply with its hash
        args.confirm = preview_hash(lambda: command(api, SimpleNamespace(**{**vars(args), "yes": False})))
    with contextlib.redirect_stdout(out):
        code = command(api, args)
    text = out.getvalue()
    line = [ln for ln in text.splitlines() if ln.startswith("RESULT: ")][-1]
    return code, json.loads(line[len("RESULT: "):]), text


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.folder = self.root / CFG["tickets_dir"] / "100"
        self.folder.mkdir(parents=True)
        self.state_path = self.folder / "state.json"
        self.state_path.write_text("{}", encoding="utf-8")
        patcher = mock.patch.object(common, "PROJECT_ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.api = mock.Mock()

    def state(self):
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def set_state(self, **state):
        self.state_path.write_text(json.dumps(state), encoding="utf-8")

    def write(self, name, text):
        path = self.folder / name
        path.write_text(text, encoding="utf-8")
        return str(path)


class DbCreateTests(Base):
    def setUp(self):
        super().setUp()
        self.main = {"number": 100, "id": 9100, "milestone_id": 7, "status": "Dev In Process", "priority": 3,
                     "summary": "Main", "custom_fields": {"Category": "Bug", "Node": "WF"}}
        self.new = {"number": 200, "id": 9200, "hierarchy_type": ATTRS["plan_level"], "status": "Dev In Process",
                    "milestone_id": 7, "assigned_to_id": 55, "summary": TITLE, "custom_fields": {"Node": DB_NODE}}
        self.links = []
        self.api.get_ticket.side_effect = lambda n: self.main if str(n) == "100" else self.new
        self.api.get_current_milestones.return_value = [{"id": 7}]
        self.api.get_current_user.return_value = {"id": 55, "login": "dev"}
        self.api.get_associations.side_effect = lambda n: self.links
        self.api.create_ticket.return_value = {"number": 200, "id": 9200}
        self.api.get_tags.return_value = [ATTRS["tag"]]
        self.api.user_label.return_value = "someone"

    def create(self, **kw):
        kw.setdefault("title", TITLE)
        kw.setdefault("description_file", "")
        return run(dev_ticket.cmd_db_create, self.api, **kw)

    def link_created(self):
        self.links.append({"relationship": RELATED, "ticket1_id": 9100, "ticket2_id": 9200})

    def test_the_preview_returns_a_confirm_hash_and_yes_needs_it(self):
        _code, preview, _ = self.create()
        self.assertRegex(preview["confirm"], r"^[0-9a-f]{8}$")
        code, res, _ = self.create(yes=True, confirm=None)
        self.assertEqual((code, res["status"]), (1, "CONFIRM_REQUIRED"))
        _code, res, _ = self.create(yes=True, confirm="deadbeef")
        self.assertEqual(res["status"], "PREVIEW_CHANGED")
        self.api.create_ticket.assert_not_called()
        self.assertNotIn("ticket", self.state().get("db", {}))

    def test_a_title_changed_after_the_preview_is_not_created(self):
        _code, preview, _ = self.create()
        _code, res, _ = self.create(yes=True, confirm=preview["confirm"], title="DB | #100 something else")
        self.assertEqual(res["status"], "PREVIEW_CHANGED")
        self.api.create_ticket.assert_not_called()

    def test_the_hash_from_the_preview_creates_it(self):
        _code, preview, _ = self.create()
        self.link_created()
        _code, res, _ = self.create(yes=True, confirm=preview["confirm"])
        self.assertIn(res["status"], ("CREATED", "CREATED_WITH_PROBLEMS"))
        self.api.create_ticket.assert_called_once()

    def test_no_state(self):
        self.state_path.unlink()
        code, res, _ = self.create()
        self.assertEqual((code, res["status"]), (1, "NO_STATE"))

    def test_recorded_db_ticket_is_idempotent(self):
        self.set_state(db={"ticket": 150})
        code, res, _ = self.create(yes=True)
        self.assertEqual((code, res["status"], res["db_ticket"]), (0, "ALREADY_EXISTS", 150))
        self.api.create_ticket.assert_not_called()

    def test_not_current_milestone(self):
        self.api.get_current_milestones.return_value = [{"id": 8}]
        code, res, _ = self.create(yes=True)
        self.assertEqual((code, res["status"]), (1, "NOT_CURRENT_MILESTONE"))
        self.api.create_ticket.assert_not_called()

    def test_related_db_ticket_is_recorded_not_recreated(self):
        self.link_created()
        self.api.get_ticket_by_id.return_value = dict(self.new)
        code, res, _ = self.create(yes=True)
        self.assertEqual((code, res["status"], res["db_ticket"]), (0, "ALREADY_EXISTS", 200))
        self.assertEqual(self.state()["db"]["ticket"], 200)
        self.api.create_ticket.assert_not_called()

    def test_inactive_related_db_ticket_is_ignored(self):
        self.link_created()
        self.api.get_ticket_by_id.return_value = dict(self.new, status=INACTIVE)
        code, res, _ = self.create()
        self.assertEqual((code, res["status"]), (0, "PREVIEW"))

    def test_multiple_db_tickets(self):
        self.links += [{"relationship": RELATED, "ticket1_id": 9100, "ticket2_id": 9200},
                       {"relationship": RELATED, "ticket1_id": 9100, "ticket2_id": 9201}]
        self.api.get_ticket_by_id.side_effect = lambda i: dict(self.new, number=200 + i - 9200, id=i)
        code, res, _ = self.create(yes=True)
        self.assertEqual((code, res["status"], len(res["candidates"])), (1, "MULTIPLE_DB_TICKETS", 2))
        self.api.create_ticket.assert_not_called()

    def test_bad_titles(self):
        for title in ("", "Foo", "DB | #999 other ticket", "DB | #100 ", "DB | #100 " + "x" * 250):
            code, res, _ = self.create(title=title, yes=True)
            self.assertEqual((code, res["status"]), (1, "BAD_TITLE"), title)
        self.api.create_ticket.assert_not_called()

    def test_description_file_outside_ticket_folder(self):
        outside = self.root / "other.md"
        outside.write_text("x", encoding="utf-8")
        code, res, _ = self.create(description_file=str(outside), yes=True)
        self.assertEqual((code, res["status"]), (1, "FILE_OUTSIDE_TICKET_FOLDER"))
        self.api.create_ticket.assert_not_called()

    def test_without_yes_only_previews(self):
        code, res, _ = self.create(description_file=self.write("db.md", "Adds Foo.bar\n"))
        self.assertEqual((code, res["status"]), (0, "PREVIEW"))
        plan = res["create"]
        self.assertEqual((plan["summary"], plan["Node"], plan["Category"], plan["tag"]), (TITLE, DB_NODE, "Bug", "WF"))
        self.assertEqual((plan["milestone_id"], plan["priority"], plan["status"], plan["assigned_to"]),
                         (7, 3, "Dev In Process", "dev"))
        self.assertEqual(plan["relation"], "Related -> #100")
        self.assertEqual(plan["description"], "Adds Foo.bar")
        for method in (self.api.create_ticket, self.api.update_ticket, self.api.add_association, self.api.update_status):
            method.assert_not_called()
        self.assertEqual(self.state(), {})

    def test_default_description_when_no_file(self):
        code, res, _ = self.create()
        self.assertIn("DB changes for #100: Main", res["create"]["description"])
        self.assertIn("Related to #100.", res["create"]["description"])

    def test_yes_creates_records_and_links(self):
        self.api.add_association.side_effect = lambda *a: self.link_created()
        code, res, _ = self.create(yes=True)
        self.assertEqual((code, res["status"], res["db_ticket"], res["problems"]), (0, "CREATED", 200, []))
        payload = self.api.create_ticket.call_args[0][0]
        self.assertEqual((payload["summary"], payload["milestone_id"], payload["assigned_to_id"], payload["priority"]),
                         (TITLE, 7, 55, 3))
        self.api.update_ticket.assert_called_once_with(
            200, {"hierarchy_type": ATTRS["plan_level"], "custom_fields": {"Node": DB_NODE, "Category": "Bug"},
                  "tags": [ATTRS["tag"]]})
        self.api.add_association.assert_called_once_with("100", 9100, 9200, RELATED)
        self.api.update_status.assert_not_called()
        self.assertEqual(self.state()["db"]["ticket"], 200)

    def test_yes_moves_status_when_it_differs(self):
        self.new["status"] = "New"
        self.api.update_status.side_effect = lambda n, s: self.new.update(status=s)
        self.api.add_association.side_effect = lambda *a: self.link_created()
        code, res, _ = self.create(yes=True)
        self.api.update_status.assert_called_once_with(200, "Dev In Process")
        self.assertEqual((code, res["status"]), (0, "CREATED"))

    def test_problems_are_reported_and_ticket_stays_recorded(self):
        self.new.update(hierarchy_type=1, assigned_to_id=1)
        self.new["custom_fields"] = {"Node": "WF"}
        self.api.get_tags.return_value = []
        code, res, _ = self.create(yes=True)  # no link was made either
        self.assertEqual((code, res["status"]), (2, "CREATED_WITH_PROBLEMS"))
        joined = " | ".join(res["problems"])
        for part in ("Plan level", "Node is not", "tag WF missing", "assignee", "Related link"):
            self.assertIn(part, joined)
        self.assertEqual(self.state()["db"]["ticket"], 200)

    def test_assembla_error_after_create_keeps_record(self):
        self.api.update_ticket.side_effect = AssemblaError("HTTP 422")
        code, res, _ = self.create(yes=True)
        self.assertEqual((code, res["status"]), (2, "CREATED_WITH_PROBLEMS"))
        self.assertIn("HTTP 422", res["problems"])
        self.assertEqual(self.state()["db"]["ticket"], 200)


class DbFilesTests(Base):
    def derived(self, **kw):
        return dict(dict(files=["a.sql", "b.sql"], commits=[], other_commits=[], unconventional_commits=[]), **kw)

    def files(self, derived):
        with mock.patch.object(db_checks, "load_ticket_db", return_value={"ticket": 150}) as load, \
                mock.patch.object(db_checks, "derive_db_files", return_value=derived) as derive:
            out = run(dev_ticket.cmd_db_files, self.api)
        self.load, self.derive = load, derive
        return out

    def test_no_state(self):
        self.state_path.unlink()
        code, res, _ = run(dev_ticket.cmd_db_files, self.api)
        self.assertEqual((code, res["status"]), (1, "NO_STATE"))

    def test_no_db_work(self):
        for db in ({}, {"ticket": 150}, {"base": "abc"}):
            self.set_state(db=db)
            code, res, _ = run(dev_ticket.cmd_db_files, self.api)
            self.assertEqual((code, res["status"]), (1, "NO_DB_WORK"), db)

    def test_writes_derived_files_with_diff(self):
        self.set_state(type="CR", db={"ticket": 150, "base": "abc", "files": ["b.sql", "old.sql"]})
        code, res, _ = self.files(self.derived(commits=["c1"], other_commits=["c2"]))
        self.assertEqual((code, res["status"], res["db_ticket"]), (0, "DB_FILES", 150))
        self.assertEqual((res["files"], res["added"], res["removed"]), (["a.sql", "b.sql"], ["a.sql"], ["old.sql"]))
        self.assertEqual((res["commits"], res["other_commits"]), (["c1"], ["c2"]))
        self.assertEqual(self.state()["db"], {"ticket": 150, "base": "abc", "files": ["a.sql", "b.sql"]})
        self.assertEqual(self.state()["type"], "CR")
        self.load.assert_called_once_with("100")
        self.assertNotIn("Commits without", res["message"])
        self.assertIn("2 DB file(s)", res["message"])

    def test_unconventional_commits_are_flagged(self):
        self.set_state(db={"ticket": 150, "base": "abc"})
        code, res, _ = self.files(self.derived(unconventional_commits=["fix stuff"]))
        self.assertEqual(res["unconventional_commits"], ["fix stuff"])
        self.assertIn("Commits without", res["message"])

    def test_no_files_clears_the_list(self):
        self.set_state(db={"ticket": 150, "base": "abc", "files": ["old.sql"]})
        code, res, _ = self.files(self.derived(files=[]))
        self.assertEqual((res["files"], res["removed"]), ([], ["old.sql"]))
        self.assertEqual(self.state()["db"]["files"], [])


class RcaTests(Base):
    def setUp(self):
        super().setUp()
        self.cf = {"Category": "Bug", "Node": "WF", "RCA": "-NA-", "CLCA": ""}
        self.ticket = {"number": 100, "id": 9100, "custom_fields": self.cf}
        self.api.get_ticket.side_effect = lambda n: self.ticket
        self.api.update_ticket.side_effect = lambda n, p: self.cf.update(p["custom_fields"])
        self.brief_ok = "# Brief\n\n## Same flaw elsewhere\n- a.cs decision: fix here\n- b.cs decision: follow-up - ticket: #301\n"

    def rca(self, **kw):
        kw.setdefault("rca_file", None)
        kw.setdefault("clca_file", None)
        kw.setdefault("plain", False)
        return run(dev_ticket.cmd_rca, self.api, **kw)

    def test_db_ticket_is_refused(self):
        self.cf["Node"] = DB_NODE
        code, res, _ = self.rca()
        self.assertEqual((code, res["status"]), (1, "NOT_MAIN_TICKET"))

    def test_show_missing(self):
        code, res, _ = self.rca()
        self.assertEqual((code, res["status"], res["missing"]), (0, "MISSING", ["RCA", "CLCA"]))
        self.assertTrue(res["required"])
        self.assertEqual(res["current"], {"RCA": "-NA-", "CLCA": ""})

    def test_show_defect_without_sweep(self):
        self.cf.update(RCA="Cause", CLCA="Fix")
        code, res, _ = self.rca()
        self.assertEqual((code, res["status"]), (0, "SWEEP_MISSING"))
        self.assertTrue(res["sweep_problems"])

    def test_show_ok_with_sweep(self):
        self.cf.update(RCA="Cause", CLCA="Fix")
        self.write("BRIEF.md", self.brief_ok)
        code, res, _ = self.rca()
        self.assertEqual((code, res["status"], res["sweep_problems"]), (0, "OK", []))

    def test_show_ok_for_non_defect_needs_no_sweep(self):
        self.cf.update(Category="Story", RCA="Cause", CLCA="Fix")
        code, res, _ = self.rca()
        self.assertEqual((code, res["status"], res["required"]), (0, "OK", False))

    def test_file_outside_ticket_folder(self):
        outside = self.root / "rca.md"
        outside.write_text("x", encoding="utf-8")
        code, res, _ = self.rca(rca_file=str(outside), yes=True)
        self.assertEqual((code, res["status"]), (1, "FILE_OUTSIDE_TICKET_FOLDER"))
        self.api.update_ticket.assert_not_called()

    def test_placeholder_file_is_empty(self):
        code, res, _ = self.rca(rca_file=self.write("rca.md", " N/A \n"))
        self.assertEqual((code, res["status"]), (1, "EMPTY"))

    def test_too_long(self):
        self.write("BRIEF.md", self.brief_ok)
        code, res, _ = self.rca(clca_file=self.write("clca.md", "x" * 1020))
        self.assertEqual((code, res["status"], res["field"]), (1, "TOO_LONG", "CLCA"))
        self.assertGreater(res["length"], res["max_length"])
        self.assertIn("closing line", res["message"])
        self.assertFalse((self.folder / "clca-final.md").exists())

    def test_preview_writes_nothing_to_assembla(self):
        self.write("BRIEF.md", self.brief_ok)
        code, res, out = self.rca(rca_file=self.write("rca.md", "Cause"), clca_file=self.write("clca.md", "Fix it"))
        self.assertEqual((code, res["status"], res["will_write"]), (0, "PREVIEW", ["CLCA", "RCA"]))
        self.api.update_ticket.assert_not_called()
        self.assertIn("CLCA preview (not written)", out)
        self.assertEqual((self.folder / "rca-final.md").read_text(encoding="utf-8"), "Cause\n")

    def test_yes_writes_clca_with_sweep_line(self):
        self.write("BRIEF.md", self.brief_ok)
        src = self.write("clca.md", "Fix it")
        code, res, _ = self.rca(rca_file=self.write("rca.md", "Cause"), clca_file=src, yes=True)
        expected = "Fix it\nSame flaw elsewhere: 2 hits (1 fixed here, 1 follow-up (#301))."
        self.assertEqual((code, res["status"], res["written"]), (0, "UPDATED", ["CLCA", "RCA"]))
        self.api.update_ticket.assert_called_once_with("100", {"custom_fields": {"RCA": "Cause", "CLCA": expected}})
        self.assertEqual((self.folder / "clca-final.md").read_text(encoding="utf-8"), expected + "\n")
        self.assertEqual(Path(src).read_text(encoding="utf-8"), "Fix it")  # source never changed
        self.assertEqual(res["still_missing"], [])

    def test_sweep_line_lists_raised_followups(self):
        self.write("BRIEF.md", self.brief_ok)
        self.rca(clca_file=self.write("clca.md", "Fix it"), yes=True)
        self.assertIn("1 follow-up (#301)", self.cf["CLCA"])

    def test_sweep_not_applicable_line(self):
        self.write("BRIEF.md", "## Same flaw elsewhere\nnot applicable: single isolated call site\n")
        self.rca(clca_file=self.write("clca.md", "Fix it"), yes=True)
        self.assertEqual(self.cf["CLCA"], "Fix it\nSame flaw elsewhere: not applicable.")

    def test_plain_and_existing_line_and_non_defect_add_nothing(self):
        self.write("BRIEF.md", self.brief_ok)
        self.rca(clca_file=self.write("clca.md", "Fix it"), plain=True, yes=True)
        self.assertEqual(self.cf["CLCA"], "Fix it")
        own = "Fix it\nSame flaw elsewhere: none."
        self.rca(clca_file=self.write("clca.md", own), yes=True)
        self.assertEqual(self.cf["CLCA"], own)
        self.cf["Category"] = "Story"
        self.rca(clca_file=self.write("clca.md", "Fix it"), yes=True)
        self.assertEqual(self.cf["CLCA"], "Fix it")

    def test_no_usable_sweep_leaves_text_alone(self):
        self.rca(clca_file=self.write("clca.md", "Fix it"), yes=True)
        self.assertEqual(self.cf["CLCA"], "Fix it")

    def test_rca_only_leaves_clca_missing(self):
        code, res, _ = self.rca(rca_file=self.write("rca.md", "Cause"), yes=True)
        self.assertEqual((code, res["status"], res["written"], res["still_missing"]), (0, "UPDATED", ["RCA"], ["CLCA"]))
        self.assertFalse((self.folder / "clca-final.md").exists())

    def test_failed_when_reread_differs(self):
        self.api.update_ticket.side_effect = lambda n, p: None  # Assembla silently ignores the write
        code, res, _ = self.rca(rca_file=self.write("rca.md", "Cause"), yes=True)
        self.assertEqual((code, res["status"], res["not_stored"]), (1, "FAILED", ["RCA"]))

    def test_failed_when_other_field_changed(self):
        def update(n, p):
            self.cf.update(p["custom_fields"])
            self.cf["Category"] = "Other"
        self.api.update_ticket.side_effect = update
        code, res, _ = self.rca(rca_file=self.write("rca.md", "Cause"), yes=True)
        self.assertEqual((code, res["status"], res["other_fields_changed"]), (1, "FAILED", ["Category"]))


if __name__ == "__main__":
    unittest.main()
