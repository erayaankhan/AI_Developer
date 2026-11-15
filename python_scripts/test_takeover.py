"""`dt takeover` and the `dt fetch` guard on a reviewer's review folder (improvements items 58 and 62)."""
import unittest
from unittest import mock

import dev_ticket
from test_functional_review_cmds import CFG, Base, run


class TakeoverTests(Base):
    def setUp(self):
        super().setUp()
        self.set_state({"ticket": "100", "kind": "review", "review": {"stage": "done"}})
        self.write("ticket.md", "# Ticket 100\n")

    def aside(self, k=1):
        return self.root / CFG["tickets_dir"] / f"100-review-r{k}"

    def test_preview_changes_nothing(self):
        code, res = run(dev_ticket.cmd_takeover, yes=False)
        self.assertEqual((code, res["status"]), (0, "PREVIEW"))
        self.assertTrue(self.state_path.is_file())
        self.assertFalse(self.aside().exists())

    def test_yes_moves_the_review_folder_aside_and_keeps_it(self):
        code, res = run(dev_ticket.cmd_takeover, yes=True)
        self.assertEqual((code, res["status"]), (0, "TAKEN_OVER"))
        self.assertFalse(self.folder.exists())
        self.assertTrue((self.aside() / "ticket.md").is_file())
        self.assertTrue((self.aside() / "state.json").is_file())

    def test_a_second_takeover_uses_the_next_free_name(self):
        self.aside(1).mkdir(parents=True)
        res = run(dev_ticket.cmd_takeover, yes=True)[1]
        self.assertEqual(res["status"], "TAKEN_OVER")
        self.assertTrue((self.aside(2) / "state.json").is_file())

    def test_an_unfinished_review_is_named_in_the_preview(self):
        self.set_state({"ticket": "100", "kind": "review", "review": {"stage": "R3-check"}})
        res = run(dev_ticket.cmd_takeover, yes=False)[1]
        self.assertEqual(res["review_stage"], "R3-check")
        self.assertIn("not finished", res["message"])

    def test_only_a_review_folder_can_be_taken_over(self):
        self.set_state({"ticket": "100", "role": "developer", "stage": "6-build"})
        self.assertEqual(run(dev_ticket.cmd_takeover, yes=True)[1]["status"], "NOT_A_REVIEW_FOLDER")
        self.assertTrue(self.state_path.is_file())

    def test_no_folder_is_reported(self):
        self.state_path.unlink()
        self.assertEqual(run(dev_ticket.cmd_takeover, yes=True)[1]["status"], "NO_STATE")

    def test_a_folder_that_cannot_be_moved_is_reported(self):
        with mock.patch("pathlib.Path.rename", side_effect=PermissionError("in use")):
            code, res = run(dev_ticket.cmd_takeover, yes=True)
        self.assertEqual((code, res["status"]), (1, "FAILED"))
        self.assertTrue(self.state_path.is_file())


class FetchGuardTests(Base):
    def api(self):
        api = mock.Mock()
        api.get_ticket.side_effect = RuntimeError("past the guard")
        return api

    def test_fetch_refuses_a_review_folder_and_offers_takeover(self):
        self.set_state({"ticket": "100", "kind": "review", "review": {"stage": "done"}})
        api = self.api()
        code, res = run(dev_ticket.cmd_fetch, api=api, type=None)
        self.assertEqual((code, res["status"]), (1, "REVIEW_FOLDER_EXISTS"))
        self.assertIn("dt takeover 100", res["message"])
        self.assertIn("/review-and-deploy 100", res["message"])
        api.get_ticket.assert_not_called()

    def test_the_review_fetch_itself_is_not_refused(self):
        self.set_state({"ticket": "100", "kind": "review"})
        with self.assertRaises(RuntimeError):
            run(dev_ticket.cmd_fetch, api=self.api(), type=None, from_review=True)

    def test_an_authors_own_folder_is_not_refused(self):
        self.set_state({"ticket": "100", "role": "developer", "stage": "6-build"})
        with self.assertRaises(RuntimeError):
            run(dev_ticket.cmd_fetch, api=self.api(), type=None)


if __name__ == "__main__":
    unittest.main()
