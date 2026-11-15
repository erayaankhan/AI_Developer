"""`dt sync-check`, `merge-master`, `sync-branch`, `pushed`, `baseline` and `deps` (improvements item 23).
git is faked and the baseline / local_gates / deps tooling is mocked: no real git, npm, dotnet or test run happens."""
import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import baseline
import deps
import dev_ticket
import local_gates
from develop_ticket import common

BRANCH = "FB_100"
REMOTE = f"refs/remotes/origin/{BRANCH}"


def run(command, **kwargs):
    out = io.StringIO()
    args = SimpleNamespace(ticket="100", yes=False, db=False, files=None, project=None,
                           legacy_peer_deps=False, dry_run=False)
    vars(args).update(kwargs)
    with contextlib.redirect_stdout(out):
        code = command(mock.Mock(), args)
    line = [ln for ln in out.getvalue().splitlines() if ln.startswith("RESULT: ")][-1]
    return code, json.loads(line[len("RESULT: "):])


class FakeGit:
    """Stands in for common.git. `replies` maps a leading args tuple to (returncode, stdout[, stderr]) or a list of
    those (consumed in order, the last one repeats); the longest matching prefix wins. Unlisted commands succeed
    with empty output."""

    def __init__(self, replies):
        self.replies = {k: (list(v) if isinstance(v, list) else [v]) for k, v in replies.items()}
        self.calls = []

    def __call__(self, *args, check=True, timeout=120, repo=None):
        self.calls.append((args, repo))
        best = max((k for k in self.replies if args[:len(k)] == k), key=len, default=None)
        reply = (0, "")
        if best is not None:
            queue = self.replies[best]
            reply = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(reply, Exception):
            raise reply
        rc, out, err = (tuple(reply) + ("",))[:3]
        if check and rc != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {err or out}")
        return subprocess.CompletedProcess(args, rc, out, err)

    def ran(self, *prefix):
        return [a for a, _ in self.calls if a[:len(prefix)] == prefix]


class GitCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.folder = self.root / dev_ticket.CONFIG["tickets_dir"] / "100"
        self.folder.mkdir(parents=True)
        self.write_state({})
        patcher = mock.patch.object(common, "PROJECT_ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_state(self, state):
        (self.folder / "state.json").write_text(json.dumps(state), encoding="utf-8")

    def read_state(self):
        return json.loads((self.folder / "state.json").read_text(encoding="utf-8"))

    def metrics(self):
        path = self.folder / "metrics.jsonl"
        return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()] if path.is_file() else []

    def git(self, **replies):
        """Install a FakeGit on common.git; keys are space-joined arg prefixes."""
        table = {"branch --show-current": (0, BRANCH + "\n"), **replies}
        fake = FakeGit({tuple(k.split()): v for k, v in table.items()})
        patcher = mock.patch.object(common, "git", fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake


class SyncCheckTests(GitCase):
    def test_other_branch_is_refused(self):
        self.git(**{"branch --show-current": (0, "master\n")})
        code, res = run(dev_ticket.cmd_sync_check)
        self.assertEqual((code, res["status"], res["current_branch"]), (0, "WRONG_BRANCH", "master"))

    def test_up_to_date(self):
        self.git(**{"rev-list --count HEAD..origin/master": (0, "0\n"), "rev-list --count origin/master..HEAD": (0, "3\n")})
        code, res = run(dev_ticket.cmd_sync_check)
        self.assertEqual((code, res["status"], res["ahead"], res["behind"]), (0, "UP_TO_DATE", 3, 0))

    def test_behind_without_conflicts(self):
        self.git(**{"rev-list --count HEAD..origin/master": (0, "4\n")})
        code, res = run(dev_ticket.cmd_sync_check)
        self.assertEqual((code, res["status"], res["behind"], res["merge_conflicts"]), (0, "BEHIND", 4, []))

    def test_behind_with_conflicts_lists_the_files(self):
        self.git(**{"rev-list --count HEAD..origin/master": (0, "4\n"),
                    "merge-tree": (1, "treehash\nA.cs\nB.cs\n")})
        code, res = run(dev_ticket.cmd_sync_check)
        self.assertEqual((code, res["status"], res["merge_conflicts"]), (0, "CONFLICTS", ["A.cs", "B.cs"]))

    def test_old_git_without_merge_tree_write_tree_is_a_failure_not_no_conflicts(self):
        self.git(**{"rev-list --count HEAD..origin/master": (0, "1\n"), "merge-tree": (129, "", "usage")})
        code, res = run(dev_ticket.cmd_sync_check)
        self.assertEqual((code, res["status"]), (1, "FAILED"))
        self.assertIn("git >= 2.38", res["message"])

    def test_fetch_failure_is_reported(self):
        self.git(**{"fetch": (1, "", "no network")})
        code, res = run(dev_ticket.cmd_sync_check)
        self.assertEqual((code, res["status"]), (1, "FAILED"))

    def test_git_timeout_is_reported(self):
        self.git(**{"fetch": subprocess.TimeoutExpired("git", 120)})
        code, res = run(dev_ticket.cmd_sync_check)
        self.assertEqual((code, res["status"]), (1, "FAILED"))


class MergeMasterTests(GitCase):
    PUSHED = {"rev-parse --verify --quiet": (0, "abc\n"), "rev-parse HEAD": (0, "abc\n")}

    def test_other_branch_is_refused(self):
        self.git(**{"branch --show-current": (0, "master\n")})
        code, res = run(dev_ticket.cmd_merge_master)
        self.assertEqual(res["status"], "WRONG_BRANCH")

    def test_uncommitted_changes_are_refused(self):
        fake = self.git(**{"status --porcelain": (0, " M C2MWebAPI/a.cs\n")})
        code, res = run(dev_ticket.cmd_merge_master)
        self.assertEqual((res["status"], res["files"]), ("NOT_COMMITTED", [" M C2MWebAPI/a.cs"]))
        self.assertEqual(fake.ran("fetch", "--quiet", "origin", "master"), [])

    def test_unpushed_commit_is_refused(self):
        self.git(**{"rev-parse --verify --quiet": (0, "old\n"), "rev-parse HEAD": (0, "new\n")})
        code, res = run(dev_ticket.cmd_merge_master)
        self.assertEqual((res["status"], res["push_state"]), ("NOT_PUSHED_YET", "NOT_PUSHED"))

    def test_nothing_to_merge_is_up_to_date(self):
        self.git(**self.PUSHED, **{"rev-list --count origin/master..HEAD": (0, "2\n")})
        code, res = run(dev_ticket.cmd_merge_master)
        self.assertEqual((code, res["status"], res["ahead"]), (0, "UP_TO_DATE", 2))
        self.assertEqual(self.metrics(), [])

    def test_behind_previews_the_command_and_never_pulls(self):
        fake = self.git(**self.PUSHED, **{"rev-list --count HEAD..origin/master": (0, "3\n")})
        code, res = run(dev_ticket.cmd_merge_master, yes=True)
        self.assertEqual((code, res["status"], res["behind"]), (0, "PREVIEW", 3))
        self.assertEqual(res["command"], "git pull --no-rebase --no-edit origin master")
        self.assertIn("no longer runs the pull", res["message"])
        self.assertEqual(fake.ran("pull"), [])
        self.assertEqual(fake.ran("push"), [])
        self.assertEqual(self.metrics()[0]["event"], "merge_master")

    def test_after_the_developers_pull_the_next_run_asks_for_the_gates(self):
        self.git(**self.PUSHED, **{"rev-list --count HEAD..origin/master": (0, "3\n")})
        self.assertEqual(run(dev_ticket.cmd_merge_master)[1]["status"], "PREVIEW")
        self.assertTrue(self.read_state()["merge_expected"])
        self.git(**self.PUSHED, **{"rev-list --count origin/master..HEAD": (0, "4\n")})  # the developer ran the pull
        res = run(dev_ticket.cmd_merge_master)[1]
        self.assertEqual((res["status"], res["verify_gates"]), ("UP_TO_DATE", True))
        self.assertIn("local_gates.py --ticket 100", res["message"])
        self.assertFalse(run(dev_ticket.cmd_merge_master)[1]["verify_gates"])

    def test_conflicting_files_are_listed(self):
        self.git(**self.PUSHED, **{"rev-list --count HEAD..origin/master": (0, "3\n"), "merge-tree": (1, "t\nX.cs\n")})
        code, res = run(dev_ticket.cmd_merge_master)
        self.assertEqual((res["status"], res["merge_conflicts"]), ("PREVIEW", ["X.cs"]))
        self.assertIn("WILL conflict", res["message"])

    def test_git_failure_is_reported(self):
        self.git(**self.PUSHED, **{"fetch --quiet origin master": (1, "", "boom")})
        code, res = run(dev_ticket.cmd_merge_master)
        self.assertEqual((code, res["status"]), (1, "FAILED"))


class SyncBranchTests(GitCase):
    """Defaults: on FB_100, clean tree, branch on origin (rev-parse ok), nothing behind, nothing unpushed."""
    ON_ORIGIN = {"rev-parse --verify --quiet": (0, "abc\n")}

    def test_other_branch_is_refused(self):
        fake = self.git(**{"branch --show-current": (0, "master\n")})
        code, res = run(dev_ticket.cmd_sync_branch, yes=True)
        self.assertEqual((res["status"], res["current_branch"]), ("WRONG_BRANCH", "master"))
        self.assertEqual(fake.ran("pull"), [])

    def test_uncommitted_changes_are_refused(self):
        fake = self.git(**{"status --porcelain": (0, "M  a.cs\n")})
        code, res = run(dev_ticket.cmd_sync_branch, yes=True)
        self.assertEqual(res["status"], "NOT_COMMITTED")
        self.assertEqual(fake.ran("fetch"), [])

    def test_without_yes_it_only_previews_the_plan(self):
        fake = self.git(**self.ON_ORIGIN, **{
            f"rev-list --count HEAD..{REMOTE}": (0, "1\n"), "rev-list --count HEAD..origin/master": (0, "2\n"),
            f"merge-tree --write-tree --name-only --no-messages HEAD {REMOTE}": (1, "t\nB.cs\n"),
            "merge-tree --write-tree --name-only --no-messages HEAD origin/master": (1, "t\nM.cs\n")})
        code, res = run(dev_ticket.cmd_sync_branch)
        self.assertEqual((code, res["status"]), (0, "PREVIEW"))
        self.assertEqual((res["behind_branch"], res["behind_master"], res["branch_on_origin"]), (1, 2, True))
        self.assertEqual((res["branch_conflicts"], res["master_conflicts"]), (["B.cs"], ["M.cs"]))
        for write in ("pull", "push", "merge"):
            self.assertEqual(fake.ran(write), [], write)
        self.assertEqual(self.metrics(), [])

    def test_preview_of_a_branch_not_on_origin(self):
        self.git(**{"rev-parse --verify --quiet": (1, "")})
        code, res = run(dev_ticket.cmd_sync_branch)
        self.assertEqual((res["status"], res["branch_on_origin"], res["behind_branch"]), ("PREVIEW", False, 0))
        self.assertEqual((res["branch_conflicts"], res["master_conflicts"]), ([], []))

    def test_nothing_to_do_is_up_to_date(self):
        fake = self.git(**self.ON_ORIGIN, **{"rev-parse --short HEAD": (0, "abc1234\n")})
        code, res = run(dev_ticket.cmd_sync_branch, yes=True)
        self.assertEqual((code, res["status"], res["actions"], res["head"]), (0, "UP_TO_DATE", [], "abc1234"))
        self.assertEqual(fake.ran("pull"), [])
        self.assertEqual(fake.ran("push"), [])
        self.assertEqual(self.metrics()[0]["status"], "UP_TO_DATE")

    def test_unpushed_commits_are_pushed_even_with_nothing_to_pull(self):
        fake = self.git(**self.ON_ORIGIN, **{f"rev-list --count {REMOTE}..HEAD": (0, "2\n")})
        code, res = run(dev_ticket.cmd_sync_branch, yes=True)
        self.assertEqual((res["status"], res["actions"]), ("SYNCED", [f"pushed origin/{BRANCH}"]))
        self.assertEqual(fake.ran("push"), [("push", "origin", BRANCH)])

    def test_a_branch_not_on_origin_is_pushed_with_upstream(self):
        fake = self.git(**{"rev-parse --verify --quiet": (1, "")})
        code, res = run(dev_ticket.cmd_sync_branch, yes=True)
        self.assertEqual(res["status"], "SYNCED")
        self.assertEqual(fake.ran("push"), [("push", "-u", "origin", BRANCH)])

    def test_pulls_branch_then_master_pushing_after_each(self):
        fake = self.git(**self.ON_ORIGIN, **{
            f"rev-list --count HEAD..{REMOTE}": (0, "1\n"),
            "rev-list --count HEAD..origin/master": [(0, "2\n"), (0, "2\n"), (0, "0\n")],
            f"rev-list --count {REMOTE}..HEAD": [(0, "1\n"), (0, "0\n"), (0, "1\n"), (0, "0\n")]})
        code, res = run(dev_ticket.cmd_sync_branch, yes=True)
        self.assertEqual((code, res["status"]), (0, "SYNCED"))
        self.assertEqual(res["actions"], [f"merged origin/{BRANCH}", f"pushed origin/{BRANCH}",
                                          "merged origin/master", f"pushed origin/{BRANCH}"])
        order = [a[:4] for a, _ in fake.calls if a[0] in ("pull", "push")]
        self.assertEqual(order, [("pull", "--no-rebase", "--no-edit", "origin"), ("push", "origin", BRANCH),
                                 ("pull", "--no-rebase", "--no-edit", "origin"), ("push", "origin", BRANCH)])
        self.assertEqual([a[-1] for a in fake.ran("pull")], [BRANCH, "master"])
        self.assertEqual(self.metrics()[0]["actions"], 4)

    def test_master_already_brought_in_by_the_branch_pull_is_not_pulled_again(self):
        fake = self.git(**self.ON_ORIGIN, **{
            f"rev-list --count HEAD..{REMOTE}": (0, "1\n"),
            "rev-list --count HEAD..origin/master": [(0, "2\n"), (0, "0\n")]})
        code, res = run(dev_ticket.cmd_sync_branch, yes=True)
        self.assertEqual(res["status"], "SYNCED")
        self.assertEqual([a[-1] for a in fake.ran("pull")], [BRANCH])

    def test_branch_pull_conflict_aborts_the_merge_and_reports_files(self):
        fake = self.git(**self.ON_ORIGIN, **{
            f"rev-list --count HEAD..{REMOTE}": (0, "1\n"),
            "pull": (1, "", "CONFLICT"), "diff --name-only": (0, "A.cs\nB.cs\n")})
        code, res = run(dev_ticket.cmd_sync_branch, yes=True)
        self.assertEqual((code, res["status"]), (0, "CONFLICTS"))
        self.assertEqual((res["step"], res["merge_conflicts"], res["done"]), (f"pull origin/{BRANCH}", ["A.cs", "B.cs"], []))
        self.assertEqual(fake.ran("merge", "--abort"), [("merge", "--abort")])
        self.assertEqual(fake.ran("push"), [])
        self.assertEqual(self.metrics()[0]["status"], "CONFLICTS")

    def test_master_conflict_keeps_the_branch_push_in_done(self):
        fake = self.git(**self.ON_ORIGIN, **{
            f"rev-list --count HEAD..{REMOTE}": (0, "1\n"),
            "rev-list --count HEAD..origin/master": (0, "2\n"),
            f"rev-list --count {REMOTE}..HEAD": [(0, "1\n"), (0, "0\n")],
            "pull": [(0, ""), (1, "", "CONFLICT")], "diff --name-only": (0, "M.cs\n")})
        code, res = run(dev_ticket.cmd_sync_branch, yes=True)
        self.assertEqual((res["status"], res["step"]), ("CONFLICTS", "pull origin/master"))
        self.assertEqual(res["done"], [f"merged origin/{BRANCH}", f"pushed origin/{BRANCH}"])
        self.assertEqual(len(fake.ran("merge", "--abort")), 1)

    def test_failed_pull_without_conflicts_is_a_failure_and_needs_no_abort(self):
        fake = self.git(**self.ON_ORIGIN, **{
            f"rev-list --count HEAD..{REMOTE}": (0, "1\n"),
            "pull": (1, "", "fatal: unable to access"), "rev-parse -q --verify MERGE_HEAD": (1, "")})
        code, res = run(dev_ticket.cmd_sync_branch, yes=True)
        self.assertEqual((code, res["status"]), (1, "FAILED"))
        self.assertIn("unable to access", res["message"])
        self.assertEqual(fake.ran("merge", "--abort"), [])

    def test_half_merged_tree_is_aborted_even_when_no_unmerged_files_are_listed(self):
        fake = self.git(**self.ON_ORIGIN, **{
            f"rev-list --count HEAD..{REMOTE}": (0, "1\n"),
            "pull": (1, "", "stopped"), "rev-parse -q --verify MERGE_HEAD": (0, "sha\n")})
        code, res = run(dev_ticket.cmd_sync_branch, yes=True)
        self.assertEqual(res["status"], "FAILED")
        self.assertEqual(len(fake.ran("merge", "--abort")), 1)

    def test_a_merge_made_by_the_script_asks_for_the_gates_before_any_edit(self):
        self.git(**self.ON_ORIGIN, **{
            "rev-list --count HEAD..origin/master": [(0, "2\n"), (0, "2\n"), (0, "0\n")],
            f"rev-list --count {REMOTE}..HEAD": [(0, "1\n"), (0, "0\n")]})
        res = run(dev_ticket.cmd_sync_branch, yes=True)[1]
        self.assertEqual((res["status"], res["verify_gates"]), ("SYNCED", True))
        self.assertIn("local_gates.py --ticket 100", res["message"])
        self.assertIn("before editing any code", res["message"])

    def test_only_pushing_does_not_ask_for_the_gates(self):
        self.git(**self.ON_ORIGIN, **{f"rev-list --count {REMOTE}..HEAD": (0, "2\n")})
        res = run(dev_ticket.cmd_sync_branch, yes=True)[1]
        self.assertEqual((res["status"], res["verify_gates"]), ("SYNCED", False))

    def test_nothing_to_do_does_not_ask_for_the_gates(self):
        self.git(**self.ON_ORIGIN)
        res = run(dev_ticket.cmd_sync_branch, yes=True)[1]
        self.assertEqual((res["status"], res["verify_gates"]), ("UP_TO_DATE", False))

    def test_a_merge_the_developer_resolved_asks_for_the_gates_on_the_next_run(self):
        self.git(**self.ON_ORIGIN, **{
            f"rev-list --count HEAD..{REMOTE}": (0, "1\n"), "pull": (1, "", "CONFLICT"), "diff --name-only": (0, "A.cs\n")})
        self.assertEqual(run(dev_ticket.cmd_sync_branch, yes=True)[1]["status"], "CONFLICTS")
        self.assertTrue(self.read_state()["merge_expected"])
        self.git(**self.ON_ORIGIN)  # the developer resolved the files and committed: nothing is left to pull
        res = run(dev_ticket.cmd_sync_branch, yes=True)[1]
        self.assertEqual((res["status"], res["verify_gates"]), ("UP_TO_DATE", True))
        self.assertIn("before editing any code", res["message"])
        self.assertNotIn("merge_expected", self.read_state())
        self.assertFalse(run(dev_ticket.cmd_sync_branch, yes=True)[1]["verify_gates"])  # consumed: no second prompt

    def test_push_failure_is_reported(self):
        self.git(**self.ON_ORIGIN, **{f"rev-list --count {REMOTE}..HEAD": (0, "1\n"), "push": (1, "", "rejected")})
        code, res = run(dev_ticket.cmd_sync_branch, yes=True)
        self.assertEqual((code, res["status"]), (1, "FAILED"))
        self.assertIn("rejected", res["message"])


class PushedTests(GitCase):
    def test_other_branch_is_refused(self):
        self.git(**{"branch --show-current": (0, "master\n")})
        code, res = run(dev_ticket.cmd_pushed)
        self.assertEqual(res["status"], "WRONG_BRANCH")

    def test_uncommitted_changes_are_refused(self):
        self.git(**{"status --porcelain": (0, " M a.cs\n")})
        code, res = run(dev_ticket.cmd_pushed)
        self.assertEqual((res["status"], res["uncommitted"]), ("NOT_COMMITTED", [" M a.cs"]))

    def test_branch_missing_on_origin(self):
        self.git(**{"rev-parse --verify --quiet": (1, ""), "rev-parse HEAD": (0, "abc\n")})
        code, res = run(dev_ticket.cmd_pushed)
        self.assertEqual((code, res["status"]), (0, "NOT_PUSHED"))

    def test_unpushed_commits_are_counted(self):
        self.git(**{"rev-parse --verify --quiet": (0, "old\n"), "rev-parse HEAD": (0, "new\n"),
                    "rev-list --count old..HEAD": (0, "2\n"), "rev-list --count HEAD..old": (0, "1\n")})
        code, res = run(dev_ticket.cmd_pushed)
        self.assertEqual((res["status"], res["unpushed_commits"], res["origin_ahead_by"]), ("NOT_PUSHED", 2, 1))

    def test_only_origin_ahead_is_behind_origin(self):
        self.git(**{"rev-parse --verify --quiet": (0, "new\n"), "rev-parse HEAD": (0, "old\n"),
                    "rev-list --count new..HEAD": (0, "0\n"), "rev-list --count HEAD..new": (0, "3\n")})
        code, res = run(dev_ticket.cmd_pushed)
        self.assertEqual((res["status"], res["origin_ahead_by"]), ("BEHIND_ORIGIN", 3))

    def test_same_commit_is_pushed(self):
        self.git(**{"rev-parse --verify --quiet": (0, "a" * 40 + "\n"), "rev-parse HEAD": (0, "a" * 40 + "\n")})
        code, res = run(dev_ticket.cmd_pushed)
        self.assertEqual((code, res["status"], res["head"]), (0, "PUSHED", "a" * 12))

    def test_db_flag_checks_the_db_repo(self):
        fake = self.git(**{"rev-parse --verify --quiet": (0, "abc\n"), "rev-parse HEAD": (0, "abc\n")})
        code, res = run(dev_ticket.cmd_pushed, db=True)
        self.assertEqual(res["status"], "PUSHED")
        self.assertTrue(fake.calls and all(repo == common.DB_REPO for _, repo in fake.calls))

    def test_git_failure_is_reported(self):
        self.git(**{"rev-parse HEAD": (1, "", "no head")})
        code, res = run(dev_ticket.cmd_pushed)
        self.assertEqual((code, res["status"]), (1, "FAILED"))


class BaselineTests(GitCase):
    START = {"branch_origin": "created", "start_head": "abc1234"}

    def setUp(self):
        super().setUp()
        self.write_state(dict(self.START))
        self.git(**{"rev-parse --short HEAD": (0, "abc1234\n"), "status --porcelain": (0, "")})
        proj = self.root / "C2MWebAPI.Tests" / "T.csproj"
        self.run_baseline = self.patch(baseline, "run_baseline",
                                       return_value=[{"project": "C2MWebAPI.Tests/T.csproj", "status": "PASS",
                                                      "total": 5, "failed": 0}])
        self.patch(local_gates, "nearest_csproj", return_value=self.root / "C2MWebAPI" / "A.csproj")
        self.test_projects = self.patch(local_gates, "test_projects_for", return_value=[proj])
        self.plan_gates = self.patch(local_gates, "plan_gates", return_value=[])
        self.run_gate = self.patch(local_gates, "run_gate")

    def patch(self, target, name, **kwargs):
        patcher = mock.patch.object(target, name, **kwargs)
        mocked = patcher.start()
        self.addCleanup(patcher.stop)
        return mocked

    def test_no_state_folder(self):
        (self.folder / "state.json").unlink()
        code, res = run(dev_ticket.cmd_baseline, files=["C2MWebAPI/A.cs"])
        self.assertEqual((code, res["status"]), (1, "NO_STATE"))
        self.run_baseline.assert_not_called()

    def test_existing_branch_has_no_baseline(self):
        self.write_state({"branch_origin": "existing", "start_head": "abc1234"})
        code, res = run(dev_ticket.cmd_baseline, files=["C2MWebAPI/A.cs"])
        self.assertEqual((code, res["status"], res["branch_origin"]), (1, "NOT_NEW_BRANCH", "existing"))
        self.run_baseline.assert_not_called()

    def test_head_moved_since_the_start(self):
        self.git(**{"rev-parse --short HEAD": (0, "def5678\n")})
        code, res = run(dev_ticket.cmd_baseline, files=["C2MWebAPI/A.cs"])
        self.assertEqual((code, res["status"], res["head"]), (1, "NOT_AT_START", "def5678"))

    def test_no_recorded_start_head_is_not_at_start(self):
        self.write_state({"branch_origin": "created"})
        code, res = run(dev_ticket.cmd_baseline, files=["C2MWebAPI/A.cs"])
        self.assertEqual(res["status"], "NOT_AT_START")

    def test_dirty_tree_is_refused(self):
        self.git(**{"rev-parse --short HEAD": (0, "abc1234\n"),
                    "status --porcelain": (0, " M C2MWebAPI/A.cs\n?? new.txt\n")})
        code, res = run(dev_ticket.cmd_baseline, files=["C2MWebAPI/A.cs"])
        self.assertEqual((code, res["status"]), (1, "NOT_CLEAN_START"))
        self.assertEqual(len(res["files"]), 2)
        self.run_baseline.assert_not_called()

    def test_no_planned_paths(self):
        code, res = run(dev_ticket.cmd_baseline)  # no --files and no PLAN.md
        self.assertEqual((code, res["status"]), (1, "NO_FILES"))
        self.run_baseline.assert_not_called()

    def test_planned_paths_are_read_from_plan_md(self):
        (self.folder / "PLAN.md").write_text("Files: `C2MWebAPI/Svc/A.cs` and `docs/readme.md`", encoding="utf-8")
        code, res = run(dev_ticket.cmd_baseline)
        self.assertEqual((code, res["status"], res["files"]), (0, "BASELINE_PASS", ["C2MWebAPI/Svc/A.cs"]))

    def test_docs_only_plan_has_nothing_to_baseline_and_passes(self):
        self.run_baseline.return_value = []
        code, res = run(dev_ticket.cmd_baseline, files=["docs/readme.md"])
        self.assertEqual((code, res["status"]), (0, "BASELINE_PASS"))
        self.assertIn("nothing to baseline", res["message"])
        self.assertEqual(self.read_state()["baseline"]["files"], [])

    def test_pass_runs_the_test_projects_and_records_the_baseline(self):
        code, res = run(dev_ticket.cmd_baseline, files=["C2MWebAPI\\Svc\\A.cs"])
        self.assertEqual((code, res["status"]), (0, "BASELINE_PASS"))
        self.assertEqual(self.run_baseline.call_args.args[1], ["C2MWebAPI.Tests/T.csproj"])
        base = self.read_state()["baseline"]
        self.assertEqual((base["status"], base["head"], base["files"]), ("PASS", "abc1234", ["C2MWebAPI/Svc/A.cs"]))
        self.assertEqual(base["projects"], [{"project": "C2MWebAPI.Tests/T.csproj", "status": "PASS", "total": 5, "failed": 0}])
        self.assertEqual(self.read_state()["branch_origin"], "created")

    def test_failing_tests_exit_2_and_are_recorded(self):
        self.run_baseline.return_value = [{"project": "T", "status": "FAIL", "total": 5, "failed": 3}]
        code, res = run(dev_ticket.cmd_baseline, files=["C2MWebAPI/A.cs"])
        self.assertEqual((code, res["status"]), (2, "BASELINE_FAIL"))
        self.assertIn("3 test(s) fail", res["message"])
        self.assertEqual(self.read_state()["baseline"]["status"], "FAIL")

    def test_a_project_without_results_is_an_error(self):
        self.run_baseline.return_value = [{"project": "T", "status": "ERROR"}]
        code, res = run(dev_ticket.cmd_baseline, files=["C2MWebAPI/A.cs"])
        self.assertEqual((code, res["status"]), (2, "BASELINE_ERROR"))

    def test_angular_specs_run_as_gates_and_a_failure_fails_the_baseline(self):
        self.run_baseline.return_value = []
        self.plan_gates.return_value = [{"name": "ng-test:c2mwf_admin"}, {"name": "dotnet-build"}]
        self.run_gate.return_value = {"status": "FAIL", "errors": ["x"], "log": "ng.log"}
        code, res = run(dev_ticket.cmd_baseline, files=["c2mwf_admin/src/a.ts"])
        self.assertEqual((code, res["status"]), (2, "BASELINE_FAIL"))
        self.run_gate.assert_called_once()
        self.assertEqual(self.run_gate.call_args.args[0]["name"], "ng-test:c2mwf_admin")
        self.assertEqual(res["projects"][0]["errors"], ["x"])
        self.assertEqual(self.read_state()["baseline"]["projects"][0]["project"], "ng-test:c2mwf_admin")


class DepsTests(GitCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(deps, "prepare", return_value={"status": "DEPS_READY", "project": "c2mwf_admin"})
        self.prepare = patcher.start()
        self.addCleanup(patcher.stop)

    def test_no_state_folder(self):
        (self.folder / "state.json").unlink()
        code, res = run(dev_ticket.cmd_deps, files=["c2mwf_admin/src/a.ts"])
        self.assertEqual((code, res["status"]), (1, "NO_STATE"))
        self.prepare.assert_not_called()

    def test_files_without_an_angular_app(self):
        code, res = run(dev_ticket.cmd_deps, files=["C2MWebAPI/A.cs"])
        self.assertEqual((code, res["status"]), (0, "NO_ANGULAR_APP"))
        self.prepare.assert_not_called()
        self.assertNotIn("deps", self.read_state())

    def test_unknown_project_is_refused(self):
        code, res = run(dev_ticket.cmd_deps, project="C2MWebAPI")
        self.assertEqual((code, res["status"]), (1, "BAD_PROJECT"))
        self.assertEqual(res["allowed"], list(local_gates.ANGULAR_PROJECTS))
        self.prepare.assert_not_called()

    def test_apps_come_from_the_planned_files_in_first_seen_order(self):
        self.prepare.side_effect = lambda root, p, **kw: {"status": "DEPS_READY", "project": p}
        code, res = run(dev_ticket.cmd_deps, files=["C2MWFAng/x.ts", "c2mwf_admin/y.ts", "C2MWFAng/z.ts"])
        self.assertEqual((code, res["status"]), (0, "DEPS_READY"))
        self.assertEqual([c.args[1] for c in self.prepare.call_args_list], ["C2MWFAng", "c2mwf_admin"])
        self.assertEqual(self.read_state()["deps"]["projects"], ["C2MWFAng", "c2mwf_admin"])

    def test_project_flag_overrides_the_plan_and_passes_the_options(self):
        code, res = run(dev_ticket.cmd_deps, project="c2mwf_runtime", legacy_peer_deps=True)
        self.assertEqual((code, res["status"]), (0, "DEPS_READY"))
        self.assertEqual(self.prepare.call_args.args, (self.root, "c2mwf_runtime"))
        self.assertEqual((self.prepare.call_args.kwargs["legacy"], self.prepare.call_args.kwargs["dry_run"]), (True, False))
        deps_state = self.read_state()["deps"]
        self.assertEqual((deps_state["projects"], deps_state["legacy_peer_deps"]), (["c2mwf_runtime"], True))
        self.assertIn("at", deps_state)

    def test_dry_run_installs_nothing_and_records_nothing(self):
        code, res = run(dev_ticket.cmd_deps, project="c2mwf_admin", dry_run=True)
        self.assertEqual((code, res["status"]), (0, "DEPS_READY"))
        self.assertTrue(self.prepare.call_args.kwargs["dry_run"])
        self.assertNotIn("deps", self.read_state())

    def test_peer_conflict_is_reported_with_exit_2_and_nothing_recorded(self):
        self.prepare.return_value = {"status": "DEPS_CONFLICT", "project": "c2mwf_admin"}
        code, res = run(dev_ticket.cmd_deps, project="c2mwf_admin")
        self.assertEqual((code, res["status"]), (2, "DEPS_CONFLICT"))
        self.assertNotIn("deps", self.read_state())

    def test_first_problem_is_the_status_and_state_is_not_recorded(self):
        self.prepare.side_effect = [{"status": "DEPS_READY", "project": "a"}, {"status": "DEPS_ERROR", "project": "b"}]
        code, res = run(dev_ticket.cmd_deps, files=["c2mwf_admin/a.ts", "C2MWFAng/b.ts"])
        self.assertEqual((code, res["status"]), (2, "DEPS_ERROR"))
        self.assertEqual(len(res["projects"]), 2)
        self.assertNotIn("deps", self.read_state())


if __name__ == "__main__":
    unittest.main()
