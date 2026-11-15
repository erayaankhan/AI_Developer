"""Support for the C2M CI repos (GenAI, IMW2) in one profile, c2m: the "auto" profile choice, the per-repo overlays, and the
Python gates (py-compile, pytest).
Nothing here calls Assembla or touches a real repo: the API is mocked and git runs only in temporary folders."""
import contextlib
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import config_loader
import local_gates


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


GENAI_ORIGIN = "git@git.assembla.com:Plasma-Portfolio/GenAI.git"


IMW2_ORIGIN = "git@git.assembla.com:Plasma-Portfolio/c2mimw2.git"


def load(profile, cwd):
    with mock.patch.dict(os.environ, {"DEV_TICKET_PROFILE": profile}), chdir(cwd):
        return config_loader.load_config()


class AutoProfileTests(unittest.TestCase):
    def test_auto_picks_c2m_in_the_genai_and_imw2_repos(self):
        with tempfile.TemporaryDirectory() as tmp:
            genai = load("auto", git_repo(Path(tmp) / "anything", GENAI_ORIGIN))
            imw2 = load("auto", git_repo(Path(tmp) / "other", IMW2_ORIGIN))
        self.assertEqual((genai["profile"], genai["repo_key"], genai["repo_node"]), ("c2m", "genai", "GenAI"))
        self.assertEqual((imw2["profile"], imw2["repo_key"], imw2["repo_node"]), ("c2m", "imw2", "IMW2"))

    def test_auto_picks_launchpad_in_a_launchpad_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = load("auto", git_repo(Path(tmp) / "x", "git@git.assembla.com:Plasma-Portfolio/CMGMT.git"))
        self.assertEqual((config["profile"], config["repo_node"]), ("launchpad", "CMGMT"))

    def test_auto_in_an_unknown_repo_stops_and_names_the_known_repos(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(SystemExit) as stop:
            load("auto", git_repo(Path(tmp) / "other", "git@example.com:someone/else.git"))
        self.assertIn("C2M - GenAI", str(stop.exception))
        self.assertIn("C2M - IMW2", str(stop.exception))

    def test_auto_outside_a_git_repo_stops(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(SystemExit):
            load("auto", tmp)


class C2MProfileTests(unittest.TestCase):
    """One profile (one workflow folder, one Jenkins) for every C2M CI repo; what differs comes from the repo overlay."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.genai = load("c2m", git_repo(base / "C2M - GenAI", GENAI_ORIGIN))
        self.imw2 = load("c2m", git_repo(base / "C2M - IMW2", IMW2_ORIGIN))
        with mock.patch.dict(os.environ, {"DEV_TICKET_PROFILE": "none"}):
            self.engine = config_loader.load_config()

    def tearDown(self):
        self.tmp.cleanup()

    def test_shared_space_flow_and_code_merge_deploy(self):
        for config in (self.genai, self.imw2):
            for key in ("statuses", "allowed_status_moves", "requested_status_moves", "low_path_status_moves",
                        "reviewer_status_moves"):
                self.assertEqual(config[key], self.engine[key], key)
            deploy = config["staging_deploy"]
            self.assertEqual((config["assembla_space"], deploy["adapter"], deploy["job"], deploy["done_status"]),
                             ("c2m-ci", "code_merge", "Code_Merge", "QA Queue"))
            self.assertIs(config["db"]["enabled"], False)
            self.assertFalse(config["quality"]["enabled"])
            self.assertTrue(config["allow_self_review"])
            self.assertEqual(config["developers"], [])
            self.assertTrue(config["epics"]["inactive_statuses"])  # engine default kept (deep merge)
            self.assertIn("New Change Request", config["ticket_type_rules"]["cr_values"])
        self.assertEqual(self.genai["staging_deploy"]["app_folders"], self.imw2["staging_deploy"]["app_folders"])

    def test_genai_overlay(self):
        c = self.genai
        self.assertEqual((c["deploy_attributes"]["node_value"], c["deploy_attributes"]["tag"], c["epics"]["node_value"]),
                         ("GenAI", "GENAI", "GenAI"))
        self.assertEqual(c["deploy_attributes"]["plan_level"], 2)  # shared part kept
        self.assertTrue(all(m.startswith("GenAI ") for m in c["current_milestones"]))
        self.assertEqual(set(c["reviewers"]), {"shamshadk", "Igor-v", "varadabhyankar"})
        self.assertEqual((c["teams"]["enabled"], c["teams"]["env_key"]), (True, "TEAMS_WEBHOOK_URL_GENAI"))
        self.assertTrue(c["gates"]["python"] and c["gates"]["maven"])
        self.assertEqual(c["gates"]["angular_projects"], ["gen-ai-designer-mfe", "prompt-designer"])
        self.assertIn("^Libs/", c["risk_check"]["high_path_patterns"])

    def test_imw2_overlay(self):
        c = self.imw2
        self.assertEqual((c["deploy_attributes"]["node_value"], c["deploy_attributes"]["tag"], c["epics"]["node_value"]),
                         ("IMW2", "IMW2", "IMW2"))
        self.assertTrue(all(m.lower().startswith("bigdata") for m in c["current_milestones"]))
        self.assertEqual(set(c["reviewers"]), {"shamshadk", "nikhilt", "gaurav-k"})
        self.assertEqual(c["teams"]["env_key"], "TEAMS_WEBHOOK_URL_IMW2")
        self.assertEqual(c["metrics"]["milestone_filter"], "BigData")
        self.assertTrue(c["gates"]["maven"])
        self.assertFalse(c["gates"]["python"])
        self.assertEqual(c["gates"]["angular_projects"], ["apm-chart"])

    def test_imw2_risk_patterns(self):
        risk = self.imw2["risk_check"]
        high = lambda p: any(re.search(x, p, re.I) for x in risk["high_path_patterns"])
        medium = lambda p: any(re.search(x, p, re.I) for x in risk["medium_path_patterns"])
        self.assertTrue(high("icedata/src/main/java/c2m/icedata/util/DecryptionUtil.java"))
        self.assertTrue(high("c2m-api-gateway/src/main/java/c2m/gateway/RateLimitFilter.java"))
        self.assertTrue(medium("c2m-api-gateway/config/files/route.json"))
        self.assertTrue(medium("bigdata_CRUD_utility/config/application.properties"))
        self.assertTrue(medium("bigdata_CRUD_utility/src/main/resources/public/main-ABC.js"))
        self.assertFalse(high("druid-reindexer/src/main/java/c2m/druid/Reindexer.java"))


class PythonGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.patch = mock.patch.object(local_gates, "PROJECT_ROOT", self.root)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def touch(self, rel, text=""):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def test_changed_python_files_are_compiled_and_tests_run_from_their_service(self):
        self.touch("GenAIService/pyproject.toml")
        venv = self.touch("GenAIService/.venv/bin/python")
        self.touch("GenAIService/src/gen_ai/app.py")
        self.touch("GenAIService/tests/test_app.py")
        files = ["GenAIService/src/gen_ai/app.py", "GenAIService/tests/test_app.py", "README.md"]
        with mock.patch.object(local_gates, "IS_WINDOWS", False):
            gates = local_gates.python_gates(files, set())
        compile_gate, pytest_gate = gates
        self.assertEqual(compile_gate["cmd"][:3], [sys.executable, "-m", "py_compile"])
        self.assertEqual(compile_gate["cmd"][3:], files[:2])
        self.assertEqual(pytest_gate["name"], "pytest:GenAIService")
        self.assertEqual(pytest_gate["cwd"], self.root / "GenAIService")
        self.assertEqual(pytest_gate["cmd"], [str(venv), "-m", "pytest", "-q", "tests/test_app.py"])

    def test_tests_without_a_service_environment_are_reported_not_run(self):
        self.touch("tests/pytest.ini")
        self.touch("tests/gai_core/test_x.py")
        gates = local_gates.python_gates(["tests/gai_core/test_x.py"], {"py-compile"})
        self.assertEqual(len(gates), 1)
        self.assertEqual(gates[0]["name"], "pytest:tests")
        self.assertIn("no .venv", gates[0]["missing"])

    def test_deleted_files_and_skips_are_respected(self):
        self.touch("a/test_b.py")
        self.assertEqual(local_gates.python_gates(["gone.py", "a/test_b.py"], {"py-compile", "pytest"}), [])

    def test_the_gates_are_planned_only_when_the_profile_turns_them_on(self):
        self.touch("x.py")
        with mock.patch.object(local_gates, "GATES_CFG", {}):
            self.assertFalse(any(g["name"].startswith("py-") for g in local_gates.plan_gates(["x.py"], {"db-checks"})))
        with mock.patch.object(local_gates, "GATES_CFG", {"python": True}):
            self.assertTrue(any(g["name"] == "py-compile" for g in local_gates.plan_gates(["x.py"], {"db-checks"})))


if __name__ == "__main__":
    unittest.main()
