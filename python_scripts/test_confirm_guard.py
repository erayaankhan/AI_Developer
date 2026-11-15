"""Improvements item 21: `--yes` needs `--confirm <hash of the previewed payload>`. The hash helper, and `comment` and
`status` (followup-create, db-create and handoff are tested with their own commands). Assembla is mocked."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import dev_ticket
import test_assembla_writes as writes
from develop_ticket import common

STATUS = dev_ticket.STATUS


def call(command, api, **kwargs):
    """One raw call, no automatic preview: -> (exit code, RESULT)."""
    out = io.StringIO()
    args = SimpleNamespace(ticket="100", file=None, yes=False, edit_id=None, confirm=None)
    vars(args).update(kwargs)
    with contextlib.redirect_stdout(out):
        code = command(api, args)
    line = [ln for ln in out.getvalue().splitlines() if ln.startswith("RESULT: ")][-1]
    return code, json.loads(line[len("RESULT: "):])


class TokenTests(unittest.TestCase):
    def test_the_same_payload_gives_the_same_short_hash(self):
        one = common.confirm_token("comment", {"ticket": "100", "body": "x", "edit": None})
        self.assertEqual(one, common.confirm_token("comment", {"edit": None, "body": "x", "ticket": "100"}))
        self.assertRegex(one, r"^[0-9a-f]{8}$")

    def test_any_change_gives_another_hash(self):
        base = common.confirm_token("comment", {"ticket": "100", "body": "x", "edit": None})
        self.assertNotEqual(base, common.confirm_token("comment", {"ticket": "100", "body": "y", "edit": None}))
        self.assertNotEqual(base, common.confirm_token("comment", {"ticket": "101", "body": "x", "edit": None}))
        self.assertNotEqual(base, common.confirm_token("status", {"ticket": "100", "body": "x", "edit": None}))

    def test_a_missing_hash_is_required_and_a_wrong_one_is_changed(self):
        payload = {"ticket": "100"}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            missing = common.confirm_refusal("status", "100", SimpleNamespace(confirm=None), payload)
            wrong = common.confirm_refusal("status", "100", SimpleNamespace(confirm="deadbeef"), payload)
            right = common.confirm_refusal("status", "100", SimpleNamespace(
                confirm=common.confirm_token("status", payload)), payload)
        self.assertEqual((missing, wrong, right), (1, 1, None))
        statuses = [json.loads(ln[len("RESULT: "):])["status"] for ln in out.getvalue().splitlines()]
        self.assertEqual(statuses, ["CONFIRM_REQUIRED", "PREVIEW_CHANGED"])

    def test_a_refusal_never_prints_the_expected_hash(self):
        payload = {"ticket": "100"}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            common.confirm_refusal("status", "100", SimpleNamespace(confirm="deadbeef"), payload)
            common.confirm_refusal("status", "100", SimpleNamespace(confirm=None), payload)
        self.assertNotIn(common.confirm_token("status", payload), out.getvalue())


class CommentTests(writes.TicketFolderCase):
    def test_the_preview_returns_a_hash_and_the_right_one_posts(self):
        path = self.write("note.md", "Hello")
        _code, preview = call(dev_ticket.cmd_comment, self.api, file=path)
        self.assertRegex(preview["confirm"], r"^[0-9a-f]{8}$")
        code, res = call(dev_ticket.cmd_comment, self.api, file=path, yes=True, confirm=preview["confirm"])
        self.assertEqual((code, res["status"]), (0, "POSTED"))

    def test_yes_without_a_hash_posts_nothing(self):
        code, res = call(dev_ticket.cmd_comment, self.api, file=self.write("note.md", "Hello"), yes=True)
        self.assertEqual((code, res["status"]), (1, "CONFIRM_REQUIRED"))
        self.api.add_comment.assert_not_called()

    def test_a_file_edited_after_the_preview_is_not_posted(self):
        path = self.write("note.md", "Hello")
        _code, preview = call(dev_ticket.cmd_comment, self.api, file=path)
        self.write("note.md", "Hello, and something the developer never saw")
        _code, res = call(dev_ticket.cmd_comment, self.api, file=path, yes=True, confirm=preview["confirm"])
        self.assertEqual(res["status"], "PREVIEW_CHANGED")
        self.api.add_comment.assert_not_called()

    def test_the_hash_of_a_new_comment_does_not_cover_an_edit_of_another(self):
        path = self.write("note.md", "Hello")
        _code, preview = call(dev_ticket.cmd_comment, self.api, file=path)
        self.api.get_comments.return_value = [{"id": 7, "user_id": 55, "comment": "old"}]
        _code, res = call(dev_ticket.cmd_comment, self.api, file=path, yes=True, edit_id="7", confirm=preview["confirm"])
        self.assertEqual(res["status"], "PREVIEW_CHANGED")
        self.api.edit_comment.assert_not_called()


class StatusTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        folder = self.root / dev_ticket.CONFIG["tickets_dir"] / "100"
        folder.mkdir(parents=True)
        (folder / "state.json").write_text(json.dumps({"ticket": "100", "role": "developer", "stage": "8-handoff"}),
                                           encoding="utf-8")
        self.api = mock.Mock()
        self.api.get_ticket.return_value = {"number": 100, "status": STATUS["in_process"], "custom_fields": {}}
        for patcher in (mock.patch.object(common, "PROJECT_ROOT", self.root),
                        mock.patch.object(common, "_remembered_role", return_value="developer"),
                        mock.patch.object(common, "push_state", return_value={"status": "PUSHED"}),
                        mock.patch.object(common, "move_db_companion", return_value=None)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def move(self, **kw):
        return call(dev_ticket.cmd_status, self.api, to=STATUS["code_review"], requested=False, **kw)

    def test_the_preview_returns_a_hash_and_the_right_one_moves_the_ticket(self):
        _code, preview = self.move()
        self.assertRegex(preview["confirm"], r"^[0-9a-f]{8}$")
        _code, res = self.move(yes=True, confirm=preview["confirm"])
        self.assertEqual(res["status"], "UPDATED")
        self.api.update_status.assert_called_once_with("100", STATUS["code_review"])

    def test_yes_without_a_hash_moves_nothing(self):
        code, res = self.move(yes=True)
        self.assertEqual((code, res["status"]), (1, "CONFIRM_REQUIRED"))
        self.api.update_status.assert_not_called()

    def test_a_hash_for_another_target_is_refused(self):
        _code, preview = self.move()
        self.api.get_ticket.return_value = {"number": 100, "status": STATUS["code_review"], "custom_fields": {}}
        _code, res = call(dev_ticket.cmd_status, self.api, to=STATUS["in_process"], requested=False, yes=True,
                          confirm=preview["confirm"])
        self.assertIn(res["status"], ("PREVIEW_CHANGED", "NOT_ALLOWED"))
        self.api.update_status.assert_not_called()

    def test_a_status_that_changed_after_the_preview_is_not_moved_on_the_old_hash(self):
        _code, preview = self.move()
        self.api.get_ticket.return_value = {"number": 100, "status": STATUS["queue"], "custom_fields": {}}
        _code, res = self.move(yes=True, confirm=preview["confirm"])
        self.assertNotEqual(res["status"], "UPDATED")
        self.api.update_status.assert_not_called()

    def test_a_refusal_comes_before_the_confirm_check(self):
        self.api.get_ticket.return_value = {"number": 100, "status": STATUS["done"], "custom_fields": {}}
        _code, res = self.move(yes=True)
        self.assertEqual(res["status"], "NOT_ALLOWED")


if __name__ == "__main__":
    unittest.main()
