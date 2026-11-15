"""`dt comment`, `dt summary-check` and `dt fix-attributes`: the commands that write to Assembla (improvements item 23).
The Assembla API is mocked; nothing is posted or changed anywhere."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from confirm_support import preview_hash
import dev_ticket
from develop_ticket import common

ATTRS = dev_ticket.CONFIG["deploy_attributes"]
EPICS = dev_ticket.CONFIG["epics"]
INACTIVE = sorted(EPICS["inactive_statuses"])[0]


def run(command, api, **kwargs):
    out = io.StringIO()
    args = SimpleNamespace(ticket="100", file=None, yes=False, edit_id=None, epic=None)
    vars(args).update(kwargs)
    if args.yes and not hasattr(args, "confirm"):  # as the workflow does: preview first, then apply with its hash
        args.confirm = preview_hash(lambda: command(api, SimpleNamespace(**{**vars(args), "yes": False})))
    with contextlib.redirect_stdout(out):
        code = command(api, args)
    line = [ln for ln in out.getvalue().splitlines() if ln.startswith("RESULT: ")][-1]
    return code, json.loads(line[len("RESULT: "):])


class TicketFolderCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.folder = self.root / dev_ticket.CONFIG["tickets_dir"] / "100"
        self.folder.mkdir(parents=True)
        (self.folder / "state.json").write_text("{}", encoding="utf-8")
        patcher = mock.patch.object(common, "PROJECT_ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.api = mock.Mock()
        self.api.get_current_user.return_value = {"id": 55}
        self.api.add_comment.return_value = 9001
        self.api.get_ticket.return_value = {"number": 100, "id": 9100, "custom_fields": {}}

    def write(self, name, text):
        path = self.folder / name
        path.write_text(text, encoding="utf-8")
        return str(path)


class CommentTests(TicketFolderCase):
    def test_a_file_outside_the_ticket_folder_is_never_posted(self):
        outside = self.root / "credentials.env"
        outside.write_text("KEY=secret", encoding="utf-8")
        code, res = run(dev_ticket.cmd_comment, self.api, file=str(outside), yes=True)
        self.assertEqual((code, res["status"]), (1, "FILE_OUTSIDE_TICKET_FOLDER"))
        self.api.add_comment.assert_not_called()

    def test_an_empty_file_is_refused(self):
        code, res = run(dev_ticket.cmd_comment, self.api, file=self.write("note.md", "  \n"), yes=True)
        self.assertEqual((code, res["status"]), (1, "EMPTY"))
        self.api.add_comment.assert_not_called()

    def test_without_yes_only_previews(self):
        code, res = run(dev_ticket.cmd_comment, self.api, file=self.write("note.md", "Hello"))
        self.assertEqual((code, res["status"], res["chars"]), (0, "PREVIEW", 5))
        self.api.add_comment.assert_not_called()

    def test_yes_posts_the_file_text(self):
        code, res = run(dev_ticket.cmd_comment, self.api, file=self.write("note.md", "Hello\n"), yes=True)
        self.assertEqual((code, res["status"], res["comment_id"]), (0, "POSTED", 9001))
        self.api.add_comment.assert_called_once_with("100", "Hello")

    def test_edit_replaces_my_own_comment(self):
        self.api.get_comments.return_value = [{"id": 7, "user_id": 55, "comment": "old"}]
        code, res = run(dev_ticket.cmd_comment, self.api, file=self.write("note.md", "new"), yes=True, edit_id="7")
        self.assertEqual((code, res["status"]), (0, "EDITED"))
        self.api.edit_comment.assert_called_once_with("100", "7", "new")
        self.api.add_comment.assert_not_called()

    def test_someone_elses_comment_is_never_edited(self):
        self.api.get_comments.return_value = [{"id": 7, "user_id": 99, "comment": "theirs"}]
        code, res = run(dev_ticket.cmd_comment, self.api, file=self.write("note.md", "new"), yes=True, edit_id="7")
        self.assertEqual((code, res["status"]), (1, "NOT_YOUR_COMMENT"))
        self.api.edit_comment.assert_not_called()

    def test_editing_an_unknown_comment_is_refused(self):
        self.api.get_comments.return_value = []
        code, res = run(dev_ticket.cmd_comment, self.api, file=self.write("note.md", "new"), yes=True, edit_id="7")
        self.assertEqual((code, res["status"]), (1, "COMMENT_NOT_FOUND"))

    def test_a_summary_that_fails_the_format_check_is_not_posted(self):
        code, res = run(dev_ticket.cmd_comment, self.api, file=self.write("SUMMARY.md", "just some words"), yes=True)
        self.assertEqual((code, res["status"]), (1, "SUMMARY_CHECK_FAILED"))
        self.assertTrue(res["errors"])
        self.api.add_comment.assert_not_called()


class SummaryCheckTests(TicketFolderCase):
    def test_a_file_outside_the_ticket_folder_is_refused(self):
        code, res = run(dev_ticket.cmd_summary_check, self.api, file=str(self.root / "other.md"))
        self.assertEqual((code, res["status"]), (1, "FILE_OUTSIDE_TICKET_FOLDER"))

    def test_a_missing_summary_is_reported(self):
        code, res = run(dev_ticket.cmd_summary_check, self.api)
        self.assertEqual((code, res["status"]), (1, "NOT_FOUND"))

    def test_a_malformed_summary_fails_with_errors(self):
        self.write("SUMMARY.md", "just some words")
        code, res = run(dev_ticket.cmd_summary_check, self.api)
        self.assertEqual((code, res["status"]), (1, "SUMMARY_CHECK_FAILED"))
        self.assertTrue(res["errors"])

    def test_a_defect_category_is_detected(self):
        rules = dev_ticket.CONFIG["ticket_type_rules"]
        self.api.get_ticket.return_value = {"number": 100, "id": 9100,
                                            "custom_fields": {rules["custom_field"]: rules["defect_values"][0]}}
        self.write("SUMMARY.md", "just some words")
        _code, res = run(dev_ticket.cmd_summary_check, self.api)
        self.assertTrue(res["defect"])


class FixAttributesTests(TicketFolderCase):
    def setUp(self):
        super().setUp()
        self.epic = {"number": 50, "id": 9050, "summary": "Epic", "status": "Dev In Process", "hierarchy_type": 3,
                     "custom_fields": {ATTRS["node_field"]: EPICS["node_value"]}}
        self.ticket = {"number": 100, "id": 9100, "hierarchy_type": 99, "custom_fields": {}}
        self.tags = []
        self.links = []
        self.api.get_ticket.side_effect = lambda n: self.epic if str(n) == "50" else self.ticket
        self.api.get_ticket_by_id.side_effect = lambda i: self.epic if i == 9050 else {}
        self.api.get_tags.side_effect = lambda n: list(self.tags)
        self.api.get_associations.side_effect = lambda n: list(self.links)
        self.api.update_ticket.side_effect = self.apply_update
        self.api.add_parent.side_effect = lambda _n, ticket_id, epic_id: self.links.append(
            {"id": 6, "ticket1_id": ticket_id, "ticket2_id": epic_id, "relationship": 0})

    def apply_update(self, _number, fields):
        for key, value in fields.items():
            if key == "tags":
                self.tags = list(value)
            elif key == "custom_fields":
                self.ticket["custom_fields"].update(value)
            else:
                self.ticket[key] = value

    def make_correct(self, with_epic=True):
        self.ticket["hierarchy_type"] = ATTRS["plan_level"]
        self.ticket["custom_fields"] = {ATTRS["node_field"]: ATTRS["node_value"]}
        self.tags = [ATTRS["tag"]]
        if with_epic:
            self.links = [{"id": 1, "ticket1_id": 9100, "ticket2_id": 9050, "relationship": 0}]

    def test_a_correct_ticket_needs_no_fix(self):
        self.make_correct()
        code, res = run(dev_ticket.cmd_fix_attributes, self.api, yes=True)
        self.assertEqual((code, res["status"]), (0, "NOTHING_TO_FIX"))
        self.api.update_ticket.assert_not_called()

    def test_without_yes_only_previews(self):
        self.make_correct(with_epic=False)
        self.ticket["hierarchy_type"] = 99
        code, res = run(dev_ticket.cmd_fix_attributes, self.api, epic="50")
        self.assertEqual((code, res["status"]), (0, "PREVIEW"))
        self.assertTrue(any(f.startswith("Plan level") for f in res["planned_fixes"]))
        self.api.update_ticket.assert_not_called()
        self.api.add_parent.assert_not_called()
        self.api.add_comment.assert_not_called()

    def test_a_missing_epic_asks_for_one_and_changes_nothing(self):
        self.make_correct(with_epic=False)
        code, res = run(dev_ticket.cmd_fix_attributes, self.api, yes=True)
        self.assertEqual((code, res["status"]), (0, "NEEDS_EPIC"))
        self.api.update_ticket.assert_not_called()

    def test_an_epic_that_is_not_an_active_wf_epic_is_refused(self):
        self.make_correct(with_epic=False)
        self.epic["status"] = INACTIVE
        code, res = run(dev_ticket.cmd_fix_attributes, self.api, epic="50", yes=True)
        self.assertEqual((code, res["status"]), (1, "BAD_EPIC"))
        self.api.add_parent.assert_not_called()

    def test_yes_fixes_the_attributes_parents_the_epic_and_comments(self):
        code, res = run(dev_ticket.cmd_fix_attributes, self.api, epic="50", yes=True)
        self.api.add_parent.assert_called_once_with("100", 9100, 9050)
        sent = self.api.update_ticket.call_args[0][1]
        self.assertEqual(sent["hierarchy_type"], ATTRS["plan_level"])
        self.assertEqual(sent["custom_fields"], {ATTRS["node_field"]: ATTRS["node_value"]})
        self.assertIn(ATTRS["tag"], sent["tags"])
        self.assertEqual(self.api.add_comment.call_count, 1)
        self.assertEqual(res["status"], "FIXED")  # the verification read-back saw no remaining problem
        self.assertEqual(code, 0)

    def test_a_fix_that_does_not_stick_is_reported_as_failed(self):
        self.api.update_ticket.side_effect = None  # the write is accepted but changes nothing
        code, res = run(dev_ticket.cmd_fix_attributes, self.api, epic="50", yes=True)
        self.assertEqual((code, res["status"]), (1, "FAILED"))
        self.assertTrue(res["remaining"])
        self.api.add_comment.assert_not_called()

    def test_an_inactive_epic_is_unlinked_before_the_new_one_is_parented(self):
        self.make_correct()
        old = {"number": 40, "id": 9040, "summary": "Old", "status": INACTIVE, "hierarchy_type": 3, "custom_fields": {}}
        self.links = [{"id": 5, "ticket1_id": 9100, "ticket2_id": 9040, "relationship": 0}]
        self.api.get_ticket_by_id.side_effect = lambda i: {9040: old, 9050: self.epic}.get(i, {})

        def add_parent(_number, _ticket_id, epic_id):
            self.links = [{"id": 6, "ticket1_id": 9100, "ticket2_id": epic_id, "relationship": 0}]
        self.api.add_parent.side_effect = add_parent
        self.api.delete_association.side_effect = lambda *_: None
        code, res = run(dev_ticket.cmd_fix_attributes, self.api, epic="50", yes=True)
        self.api.delete_association.assert_called_once_with("100", 5)
        self.assertEqual((code, res["status"]), (0, "FIXED"))


if __name__ == "__main__":
    unittest.main()
