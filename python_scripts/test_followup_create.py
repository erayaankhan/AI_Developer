"""`dt followup-create`: preview first, one ticket per title, the main ticket's milestone, status New, no priority.
The Assembla API is mocked; nothing is created anywhere."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from confirm_support import preview_hash
import dev_ticket
from develop_ticket import common


NL = chr(10)


def call(api, **kwargs):
    out = io.StringIO()
    args = dict(ticket="100", title="Bug | AFD3 - follow-up", description_file="", category=None, hit=None, allow_similar=False, yes=False)
    args.update(kwargs)
    if args["yes"] and "confirm" not in args:  # as the workflow does: preview first, then apply with its hash
        args["confirm"] = preview_hash(lambda: dev_ticket.cmd_followup_create(api, mock.Mock(**{**args, "yes": False})))
    with contextlib.redirect_stdout(out):
        code = dev_ticket.cmd_followup_create(api, mock.Mock(**args))
    line = [ln for ln in out.getvalue().splitlines() if ln.startswith("RESULT: ")][-1]
    return code, json.loads(line[len("RESULT: "):])


class FollowupCreateTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.folder = self.root / dev_ticket.CONFIG["tickets_dir"] / "100"
        self.folder.mkdir(parents=True)
        self.state = self.folder / "state.json"
        self.state.write_text("{}", encoding="utf-8")
        self.desc = self.folder / "followup.md"
        self.desc.write_text("Repro: ...\n\nRelated to #100.", encoding="utf-8")
        patcher = mock.patch.object(common, "PROJECT_ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.new = {"number": 200, "id": 9200, "hierarchy_type": 2, "status": "New", "milestone_id": 7,
                    "custom_fields": {"Node": "WF", "C2M-Module": "AFD v3", "Category": "Bug"}, "summary": "t"}
        self.api = mock.Mock()
        self.api.get_ticket.side_effect = lambda n: (
            {"number": 100, "id": 9100, "milestone_id": 7, "status": "Dev In Process",
             "custom_fields": {"Category": "Bug", "C2M-Module": "AFD v3", "C2M-Sub-Module": "Start Node"}}
            if str(n) == "100" else self.new)
        self.new["assigned_to_id"] = 55
        self.api.get_current_user.return_value = {"id": 55, "login": "creator"}
        self.api.get_milestone.return_value = {"title": "Workflow 3.17.0_CURRENT"}
        self.api.create_ticket.return_value = {"number": 200, "id": 9200}
        self.api.get_tags.return_value = ["WF"]
        self.api.get_associations.return_value = [{"relationship": dev_ticket.RELATED, "ticket1_id": 9100, "ticket2_id": 9200}]
        self.api.get_ticket_by_id.side_effect = lambda i: self.new if i == 9200 else {}

    def test_without_yes_only_previews(self):
        code, res = call(self.api, description_file=str(self.desc))
        self.assertEqual((code, res["status"]), (0, "PREVIEW"))
        plan = res["create"]
        self.assertEqual((plan["status"], plan["priority"], plan["assignee"]), ("New", "(unset)", "creator"))
        self.assertEqual(plan["milestone"], "Workflow 3.17.0_CURRENT")
        self.assertEqual(plan["C2M-Module"], "AFD v3")
        self.assertNotIn("C2M-Sub-Module", plan)
        self.api.create_ticket.assert_not_called()

    def test_the_preview_returns_a_confirm_hash_and_yes_needs_it(self):
        _code, preview = call(self.api, description_file=str(self.desc))
        self.assertRegex(preview["confirm"], r"^[0-9a-f]{8}$")
        code, res = call(self.api, description_file=str(self.desc), yes=True, confirm=None)
        self.assertEqual((code, res["status"]), (1, "CONFIRM_REQUIRED"))
        _code, res = call(self.api, description_file=str(self.desc), yes=True, confirm="deadbeef")
        self.assertEqual(res["status"], "PREVIEW_CHANGED")
        self.api.create_ticket.assert_not_called()
        _code, res = call(self.api, description_file=str(self.desc), yes=True, confirm=preview["confirm"])
        self.assertEqual(res["status"], "CREATED")

    def test_a_description_edited_after_the_preview_is_not_created(self):
        _code, preview = call(self.api, description_file=str(self.desc))
        self.desc.write_text("A different description.", encoding="utf-8")
        _code, res = call(self.api, description_file=str(self.desc), yes=True, confirm=preview["confirm"])
        self.assertEqual(res["status"], "PREVIEW_CHANGED")
        self.api.create_ticket.assert_not_called()

    def test_a_changed_category_is_not_created_on_the_old_hash(self):
        _code, preview = call(self.api, description_file=str(self.desc))
        _code, res = call(self.api, description_file=str(self.desc), yes=True, confirm=preview["confirm"], category="CR")
        self.assertEqual(res["status"], "PREVIEW_CHANGED")
        self.api.create_ticket.assert_not_called()

    def write_brief(self, *bullets):
        text = NL.join(["# Brief", "", "## Same flaw elsewhere", *bullets, "## Assumptions / Decisions", ""])
        (self.folder / "BRIEF.md").write_text(text, encoding="utf-8")

    def brief(self):
        return (self.folder / "BRIEF.md").read_text(encoding="utf-8")

    def test_hit_tags_the_sweep_bullet_with_the_new_ticket(self):
        self.write_brief("- a/B.cs:12 - same cast - decision: fix here", "- c/D.ts:3 - same pattern - decision: follow-up")
        _code, preview = call(self.api, description_file=str(self.desc), hit="c/D.ts:3")
        self.assertIn("c/D.ts:3", preview["create"]["brief_bullet"])
        self.assertNotIn("ticket:", self.brief())  # a preview changes nothing
        code, res = call(self.api, description_file=str(self.desc), hit="c/D.ts:3", yes=True)
        self.assertEqual((code, res["status"], res["brief_updated"]), (0, "CREATED", True))
        self.assertIn("- c/D.ts:3 - same pattern - decision: follow-up - ticket: #200" + NL, self.brief())
        self.assertIn("- a/B.cs:12 - same cast - decision: fix here" + NL, self.brief())

    def test_an_unknown_hit_creates_nothing(self):
        self.write_brief("- c/D.ts:3 - same pattern - decision: follow-up")
        code, res = call(self.api, description_file=str(self.desc), hit="nowhere.ts", yes=True)
        self.assertEqual((code, res["status"]), (1, "HIT_NOT_FOUND"))
        self.api.create_ticket.assert_not_called()
        self.assertNotIn("ticket:", self.brief())

    def test_an_ambiguous_hit_creates_nothing_and_lists_the_candidates(self):
        self.write_brief("- c/D.ts:3 - a - decision: follow-up", "- c/D.ts:30 - b - decision: follow-up")
        code, res = call(self.api, description_file=str(self.desc), hit="c/D.ts", yes=True)
        self.assertEqual((code, res["status"], len(res["candidates"])), (1, "HIT_AMBIGUOUS", 2))
        self.api.create_ticket.assert_not_called()

    def test_a_hit_with_no_brief_creates_nothing(self):
        code, res = call(self.api, description_file=str(self.desc), hit="c/D.ts:3", yes=True)
        self.assertEqual((code, res["status"]), (1, "HIT_NOT_FOUND"))
        self.api.create_ticket.assert_not_called()

    def test_a_hit_already_ticketed_or_declined_is_not_a_candidate(self):
        self.write_brief("- c/D.ts:3 - a - decision: follow-up - ticket: #7",
                         "- e/F.ts:4 - b - decision: follow-up - declined: the developer says dead code")
        for hit in ("c/D.ts:3", "e/F.ts:4"):
            _code, res = call(self.api, description_file=str(self.desc), hit=hit, yes=True)
            self.assertEqual(res["status"], "HIT_NOT_FOUND", hit)

    def test_the_confirmed_hash_covers_the_hit(self):
        self.write_brief("- c/D.ts:3 - a - decision: follow-up", "- e/F.ts:4 - b - decision: follow-up")
        _code, preview = call(self.api, description_file=str(self.desc), hit="c/D.ts:3")
        _code, res = call(self.api, description_file=str(self.desc), hit="e/F.ts:4", yes=True, confirm=preview["confirm"])
        self.assertEqual(res["status"], "PREVIEW_CHANGED")
        self.api.create_ticket.assert_not_called()

    def test_rerunning_with_the_same_title_still_tags_an_untagged_bullet(self):
        self.write_brief("- c/D.ts:3 - same pattern - decision: follow-up")
        self.state.write_text(json.dumps({"followups": [{"title": "Bug | AFD3 - follow-up", "number": 150}]}), encoding="utf-8")
        _code, res = call(self.api, description_file=str(self.desc), hit="c/D.ts:3", yes=True)
        self.assertEqual((res["status"], res["followup"], res["brief_updated"]), ("ALREADY_EXISTS", 150, True))
        self.assertIn("ticket: #150", self.brief())
        self.api.create_ticket.assert_not_called()

    def test_a_preview_of_an_existing_title_does_not_touch_the_brief(self):
        self.write_brief("- c/D.ts:3 - same pattern - decision: follow-up")
        self.state.write_text(json.dumps({"followups": [{"title": "Bug | AFD3 - follow-up", "number": 150}]}), encoding="utf-8")
        before = self.brief()
        _code, res = call(self.api, description_file=str(self.desc), hit="c/D.ts:3")
        self.assertEqual((res["status"], res["brief_updated"]), ("ALREADY_EXISTS", False))
        self.assertEqual(self.brief(), before)

    def related(self, summary, status="New", relationship=None, number=300):
        """Another ticket Related to #100 in Assembla, as if created on another machine."""
        other = {"number": number, "id": 9000 + number, "summary": summary, "status": status}
        self.api.get_associations.return_value = [
            {"relationship": dev_ticket.RELATED if relationship is None else relationship, "ticket1_id": 9100, "ticket2_id": other["id"]}]
        self.api.get_ticket_by_id.side_effect = lambda i: other if i == other["id"] else {}
        return other

    def test_a_near_identical_open_related_ticket_stops_a_second_creation(self):
        self.related("bug | AFD3 follow up")  # same words, other case and punctuation, no state.json record
        code, res = call(self.api, description_file=str(self.desc), yes=True)
        self.assertEqual((code, res["status"]), (0, "SIMILAR_EXISTS"))
        self.assertEqual((res["similar"][0]["ticket"], res["similar"][0]["similarity"]), (300, 1.0))
        self.api.create_ticket.assert_not_called()

    def test_the_preview_refuses_too_so_the_developer_hears_it_before_asking(self):
        self.related("Bug | AFD3 - follow-up")
        _code, res = call(self.api, description_file=str(self.desc))
        self.assertEqual(res["status"], "SIMILAR_EXISTS")
        self.assertNotIn("confirm", res)

    def test_a_different_title_is_not_similar(self):
        self.related("Bug | Totally different export crash on the report page")
        _code, res = call(self.api, description_file=str(self.desc))
        self.assertEqual(res["status"], "PREVIEW")
        self.assertNotIn("similar_ignored", res["create"])

    def test_a_closed_similar_ticket_is_ignored(self):
        self.related("Bug | AFD3 - follow-up", status=sorted(dev_ticket.CONFIG["epics"]["inactive_statuses"])[0])
        _code, res = call(self.api, description_file=str(self.desc))
        self.assertEqual(res["status"], "PREVIEW")

    def test_only_related_links_count(self):
        self.related("Bug | AFD3 - follow-up", relationship=0)  # a parent link, not Related
        _code, res = call(self.api, description_file=str(self.desc))
        self.assertEqual(res["status"], "PREVIEW")

    def test_allow_similar_creates_it_and_the_preview_names_what_was_overridden(self):
        other = self.related("Bug | AFD3 - follow-up")
        _code, preview = call(self.api, description_file=str(self.desc), allow_similar=True)
        self.assertEqual((preview["status"], preview["create"]["similar_ignored"][0]["ticket"]), ("PREVIEW", 300))
        # after the create, the link to the new ticket is there as well (the command verifies it)
        self.api.get_associations.return_value = [
            {"relationship": dev_ticket.RELATED, "ticket1_id": 9100, "ticket2_id": other["id"]},
            {"relationship": dev_ticket.RELATED, "ticket1_id": 9100, "ticket2_id": 9200}]
        self.api.get_ticket_by_id.side_effect = lambda i: {other["id"]: other, 9200: self.new}.get(i, {})
        _code, res = call(self.api, description_file=str(self.desc), allow_similar=True, yes=True)
        self.assertEqual(res["status"], "CREATED")

    def test_an_existing_title_in_state_is_still_answered_first(self):
        self.related("Bug | AFD3 - follow-up")
        self.state.write_text(json.dumps({"followups": [{"title": "Bug | AFD3 - follow-up", "number": 150}]}), encoding="utf-8")
        _code, res = call(self.api, description_file=str(self.desc), yes=True)
        self.assertEqual(res["status"], "ALREADY_EXISTS")

    def test_without_hit_the_brief_is_left_alone(self):
        self.write_brief("- c/D.ts:3 - same pattern - decision: follow-up")
        before = self.brief()
        _code, res = call(self.api, description_file=str(self.desc), yes=True)
        self.assertEqual((res["status"], res["brief_updated"]), ("CREATED", False))
        self.assertEqual(self.brief(), before)

    def test_yes_creates_with_main_milestone_assigned_to_creator_and_no_priority(self):
        code, res = call(self.api, description_file=str(self.desc), yes=True)
        self.assertEqual((code, res["status"], res["followup"]), (0, "CREATED", 200))
        sent = self.api.create_ticket.call_args[0][0]
        self.assertEqual(sent["milestone_id"], 7)
        self.assertNotIn("priority", sent)
        self.assertEqual(sent["assigned_to_id"], 55)
        self.assertEqual(sent["custom_fields"], {"Node": "WF", "C2M-Module": "AFD v3", "Category": "Bug"})
        self.api.add_association.assert_called_once_with("100", 9100, 9200, dev_ticket.RELATED)

    def test_same_title_is_never_created_twice(self):
        call(self.api, description_file=str(self.desc), yes=True)
        self.assertEqual(json.loads(self.state.read_text(encoding="utf-8"))["followups"],
                         [{"title": "Bug | AFD3 - follow-up", "number": 200}])
        self.api.create_ticket.reset_mock()
        code, res = call(self.api, description_file=str(self.desc), yes=True)
        self.assertEqual(res["status"], "ALREADY_EXISTS")
        self.api.create_ticket.assert_not_called()

    def test_status_other_than_new_is_set_and_a_failed_check_is_reported(self):
        self.new["status"] = "Dev In Process"
        self.new["milestone_id"] = 8  # differs from the main ticket's
        code, res = call(self.api, description_file=str(self.desc), yes=True)
        self.api.update_status.assert_called_once_with(200, "New")
        self.assertEqual((code, res["status"]), (2, "CREATED_WITH_PROBLEMS"))
        self.assertTrue(any("milestone" in p for p in res["problems"]))

    def test_refusals(self):
        outside = self.root / "other.md"
        outside.write_text("x", encoding="utf-8")
        self.assertEqual(call(self.api, description_file=str(outside))[1]["status"], "FILE_OUTSIDE_TICKET_FOLDER")
        self.assertEqual(call(self.api, description_file=str(self.desc), title=" ")[1]["status"], "BAD_TITLE")
        self.desc.write_text("  \n", encoding="utf-8")
        self.assertEqual(call(self.api, description_file=str(self.desc))[1]["status"], "EMPTY")
        self.api.create_ticket.assert_not_called()


if __name__ == "__main__":
    unittest.main()
