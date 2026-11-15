"""Tests for deps. Run from this folder: python -m unittest test_deps -v"""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import deps


def done(code=0, out="", err=""):
    return subprocess.CompletedProcess([], code, out, err)


class Runner:
    """Fake subprocess.run: records calls, answers by command."""

    def __init__(self, tracked=False, install=None, modified=""):
        self.calls, self.tracked, self.modified = [], tracked, modified
        self.install = install or done()

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        if cmd[:2] == ["git", "ls-files"]:
            return done(0 if self.tracked else 1)
        if cmd[:2] == ["git", "status"]:
            return done(0, self.modified)
        return self.install


def make_app(root: Path, lock=True, core_declared="22.2.0", core_installed="22.2.0"):
    app = root / "c2mwf_admin"
    (app / "node_modules" / "@angular" / "core").mkdir(parents=True)
    (app / "package.json").write_text(json.dumps({"dependencies": {"@angular/core": core_declared}}))
    (app / "node_modules" / "@angular" / "core" / "package.json").write_text(json.dumps({"version": core_installed}))
    if lock:
        (app / "package-lock.json").write_text("{}")
    return app


class ProjectTests(unittest.TestCase):
    def test_projects_for_files(self):
        files = ["c2mwf_admin/src/a.ts", "C2MWFAng\\src\\b.ts", "C2MWebAPI/x.cs", "c2mwf_admin/src/c.ts"]
        self.assertEqual(deps.projects_for_files(files, ("c2mwf_admin", "C2MWFAng", "c2mwf_runtime")),
                         ["c2mwf_admin", "C2MWFAng"])

    def test_no_angular_files(self):
        self.assertEqual(deps.projects_for_files(["C2MWebAPI/x.cs"], ("c2mwf_admin",)), [])


class PrepareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_deletes_an_untracked_lockfile_and_installs_without_a_lock(self):
        app = make_app(self.root)
        run = Runner(tracked=False)
        out = deps.prepare(self.root, "c2mwf_admin", run=run, which=lambda n: n)
        self.assertEqual(out["status"], "DEPS_READY")
        self.assertFalse((app / "package-lock.json").exists())
        install = run.calls[-2] if run.calls[-1][:2] == ["git", "status"] else run.calls[-1]
        self.assertEqual(install[:2], ["npm", "install"])
        self.assertIn("--no-package-lock", install)
        self.assertNotIn("--legacy-peer-deps", install)

    def test_never_deletes_a_tracked_lockfile(self):
        app = make_app(self.root)
        out = deps.prepare(self.root, "c2mwf_admin", run=Runner(tracked=True), which=lambda n: n)
        self.assertTrue((app / "package-lock.json").exists())
        self.assertIn("tracked", out["lockfile"])

    def test_dry_run_keeps_the_lockfile_and_adds_the_flag(self):
        app = make_app(self.root)
        run = Runner(tracked=False)
        out = deps.prepare(self.root, "c2mwf_admin", dry_run=True, run=run, which=lambda n: n)
        self.assertTrue((app / "package-lock.json").exists())
        self.assertTrue(any("--dry-run" in c for c in run.calls))
        self.assertTrue(out["dry_run"])

    def test_peer_conflict_is_reported_not_worked_around(self):
        make_app(self.root)
        run = Runner(install=done(1, "", "npm error code ERESOLVE\nnpm error Could not resolve dependency:\n"))
        out = deps.prepare(self.root, "c2mwf_admin", run=run, which=lambda n: n)
        self.assertEqual(out["status"], "DEPS_CONFLICT")
        self.assertFalse(any("--legacy-peer-deps" in c for c in run.calls))
        self.assertTrue(out["output"])

    def test_legacy_peer_deps_only_when_asked(self):
        make_app(self.root)
        run = Runner()
        deps.prepare(self.root, "c2mwf_admin", legacy=True, run=run, which=lambda n: n)
        self.assertTrue(any("--legacy-peer-deps" in c for c in run.calls))

    def test_other_install_failures_are_errors(self):
        make_app(self.root)
        out = deps.prepare(self.root, "c2mwf_admin", run=Runner(install=done(1, "", "ENOTFOUND registry")),
                           which=lambda n: n)
        self.assertEqual(out["status"], "DEPS_ERROR")

    def test_a_changed_package_json_is_flagged(self):
        make_app(self.root)
        out = deps.prepare(self.root, "c2mwf_admin", run=Runner(modified=" M c2mwf_admin/package.json"),
                           which=lambda n: n)
        self.assertEqual(out["status"], "DEPS_DIRTY")

    def test_timeout(self):
        make_app(self.root)

        def timing_out(cmd, **kwargs):
            if cmd[0] == "npm":
                raise subprocess.TimeoutExpired(cmd, 1)
            return done(1)

        out = deps.prepare(self.root, "c2mwf_admin", timeout=1, run=timing_out, which=lambda n: n)
        self.assertEqual(out["status"], "DEPS_ERROR")
        self.assertIn("timed out", out["message"])

    def test_missing_package_json(self):
        self.assertEqual(deps.prepare(self.root, "nope", run=Runner())["status"], "NO_PACKAGE_JSON")


class AngularCoreCheckTests(unittest.TestCase):
    def test_match_and_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = make_app(Path(tmp), core_declared="^22.2.0", core_installed="22.2.1")
            self.assertTrue(deps.angular_core_check(app)["match"])
        with tempfile.TemporaryDirectory() as tmp:
            app = make_app(Path(tmp), core_declared="22.2.0", core_installed="20.3.17")
            check = deps.angular_core_check(app)
            self.assertFalse(check["match"])
            self.assertEqual(check["installed"], "20.3.17")

    def test_unreadable_is_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(deps.angular_core_check(Path(tmp))["match"])


if __name__ == "__main__":
    unittest.main()
