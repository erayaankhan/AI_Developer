#!/usr/bin/env python3
"""
Local quality gates for the dev-ticket workflow - replaces the Jenkins CI pass.

Works out which files the ticket changed and runs only the checks those files need:

    1. pre-commit run --files <changed>   hygiene, ng-lint, spec-exists, dotnet-format
    2. dotnet build <nearest .csproj>      for changed .cs / .csproj files
    3. dotnet test <test projects>         test projects that reference a built project
    4. ng test --include <specs>           changed specs + the spec next to each changed .ts

Full output of every command goes to a log file; the console shows only a one-line
summary per gate plus the error lines, and ends with

    RESULT: {"status": "PASS"|"FAIL", "gates": [...], "log_dir": "..."}

Usage
    python local_gates.py --ticket 18474            # logs under the ticket's work folder
    python local_gates.py --files a.ts b.cs         # explicit file list
    python local_gates.py --ticket 18474 --dry-run  # show what would run
    python local_gates.py --skip ng-test            # skip a gate (pre-commit, dotnet-build, dotnet-test, ng-test)
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from config_loader import load_config, resolve_project_root
from state_io import update_state

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG = load_config()  # dev_ticket.config.json + optional dev_ticket.config.local.json
PROJECT_ROOT = resolve_project_root(CONFIG)  # "auto": the git repo of the current directory
BASE = CONFIG["base_branch"]

# The repo's layout. Defaults are the original team's repo; a profile / repo overlay sets its own under "gates":
#   angular_projects  Angular app folders (relative to the repo root; nested paths such as "portal/frontend" work)
#   source_roots      path prefixes of active source code (untracked files elsewhere are ignored as local noise)
#   legacy_prefixes   folders never gated
#   extra_extensions  more file types that count as code when untracked (e.g. ".java")
#   maven             true: a Maven test gate for changed Java modules
#   python            true: py-compile for changed .py files, and pytest for changed test files (test_*.py / *_test.py),
#                     run from the nearest folder with pyproject.toml / pytest.ini, with its .venv when it has one
GATES_CFG = CONFIG.get("gates") or {}
ANGULAR_PROJECTS = tuple(GATES_CFG.get("angular_projects") or ("c2mwf_admin", "C2MWFAng", "c2mwf_runtime"))
LEGACY_PREFIXES = tuple(GATES_CFG["legacy_prefixes"] if "legacy_prefixes" in GATES_CFG
                        else ("C2MWFAppAdmin/", "Nutrien_EAMS_WebApp/", "LegacyApplication/"))
SOURCE_ROOTS = tuple(GATES_CFG.get("source_roots") or ANGULAR_PROJECTS + (
    "C2MWebAPI/", "Private_WF_API/", "wfapi_", "wf_mediaurl_service/", "c2mwf_common_utilities/",
))
UNTRACKED_EXTENSIONS = {".ts", ".html", ".scss", ".css", ".cs", ".csproj", ".sql"} | set(GATES_CFG.get("extra_extensions") or [])
MAVEN_EXTENSIONS = (".java", "pom.xml", ".properties", ".yml", ".yaml")
PYTEST_MARKERS = ("pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini")
TEST_PACKAGE_RE = re.compile(r"Microsoft\.NET\.Test\.Sdk|xunit|nunit", re.I)
ERROR_LINE_RE = re.compile(r"\berror\b|\bFAILED\b|\bFailed\b|✗|Error:|Executed .* \(\d+ FAILED\)", re.I)
MAX_ERROR_LINES = 40

IS_WINDOWS = sys.platform.startswith("win")
GATE_TIMEOUT = int(CONFIG.get("gate_timeout_seconds", 1800))  # a hung gate must not hang the workflow
SECRET_ENV = ("ASSEMBLA_API_KEY", "ASSEMBLA_API_SECRET", "JENKINS_USER", "JENKINS_API_TOKEN",
              "JENKINS_REMOTE_USER", "JENKINS_REMOTE_API_TOKEN", "SONAR_TOKEN", "SNYK_TOKEN",
              "TEAMS_WEBHOOK_URL")  # never leak to child tools
CMD_UNSAFE_CHARS = set('&|<>^%!"')  # cmd.exe metacharacters: relevant when the tool is a .cmd/.bat (npx)
MAX_CMDLINE = 6000  # Windows caps a command line at 8191 characters


def git(*args, check: bool = False) -> str:
    """stdout of a git call. quotepath off so non-ASCII file names come back as-is. A failed call gives ""
    unless check=True, which raises: use that wherever "" would be read as "nothing changed"."""
    proc = subprocess.run(["git", "-C", str(PROJECT_ROOT), "-c", "core.quotepath=false", *args],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        if check:
            raise RuntimeError(f"git {' '.join(args)} failed: {(proc.stderr or proc.stdout).strip()}")
        return ""
    return proc.stdout


_BASE_REFRESHED = False


def refresh_base() -> str:
    """Fetch origin/<base> once per process, before any diff is taken against it. A stale origin/master makes the
    merge base old, so everything master brought in since (after a pull of master) is counted as the ticket's own
    change: wrong file list, wrong line count. "" = refreshed (or already done); otherwise the warning to show.
    A failed fetch is only a warning: a stale ref widens the diff, so the risk check errs on the safe side."""
    global _BASE_REFRESHED
    if _BASE_REFRESHED:
        return ""
    _BASE_REFRESHED = True
    try:
        proc = subprocess.run(["git", "-C", str(PROJECT_ROOT), "fetch", "--quiet", "origin", BASE],
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    except subprocess.TimeoutExpired:
        return f"could not refresh origin/{BASE} (fetch timed out)"
    if proc.returncode != 0:
        return f"could not refresh origin/{BASE} ({(proc.stderr or proc.stdout).strip()[:150]})"
    return ""


LOCAL_ONLY_RES = [re.compile(p) for p in CONFIG.get("local_only_patterns", [])]


def is_local_only(name: str) -> bool:
    """Files the developer changes for their own machine (D-27, e.g. environment.ts pointing
    at a local backend). They are never part of a ticket: no gates, no risk, never staged."""
    return any(r.search(name) for r in LOCAL_ONLY_RES)


def local_only_changes() -> list:
    """Local-only files that currently differ from HEAD, so the workflow can warn about them."""
    return sorted(n for n in git("diff", "--name-only", "HEAD").splitlines() if n.strip() and is_local_only(n))


def changed_files() -> list:
    """Committed on the branch + tracked working-tree changes + new untracked code files,
    minus local-only files (config local_only_patterns)."""
    names = set(git("diff", "--name-only", f"origin/{BASE}...HEAD", check=True).splitlines())
    names |= set(git("diff", "--name-only", "HEAD", check=True).splitlines())
    # Untracked files: this repo carries a lot of local untracked noise (reports, logs,
    # copies), so only new code files inside active source folders count.
    for name in git("ls-files", "--others", "--exclude-standard").splitlines():
        if (name.startswith(SOURCE_ROOTS) and Path(name).suffix.lower() in UNTRACKED_EXTENSIONS
                and " - Copy" not in name):
            names.add(name)
    files = []
    for name in sorted(n.strip() for n in names if n.strip()):
        if name.startswith(LEGACY_PREFIXES) or is_local_only(name):
            continue
        if (PROJECT_ROOT / name).is_file():
            files.append(name.replace("\\", "/"))
    return files


def deleted_files() -> list:
    """Files this ticket deletes (committed on the branch or in the working tree). The gates can't lint them,
    but the risk check must see them: deleting an auth guard or an SP is as risky as editing it."""
    names = set(git("diff", "--name-only", "--diff-filter=D", f"origin/{BASE}...HEAD", check=True).splitlines())
    names |= set(git("diff", "--name-only", "--diff-filter=D", "HEAD", check=True).splitlines())
    return sorted(n.strip() for n in names if n.strip() and not (PROJECT_ROOT / n.strip()).exists()
                  and not n.startswith(LEGACY_PREFIXES) and not is_local_only(n.strip()))


def risk_files() -> list:
    """What the risk check judges: changed_files() + deleted files + untracked files of ANY type that match a
    risk path pattern (appsettings*.json, ocelot routes, .pubxml ... are invisible to the gates' extension
    filter but are exactly what makes a change risky; step 8 stages them only after this check has run).
    Other untracked noise (lock files, docs, scripts) stays out, as in changed_files()."""
    names = set(changed_files()) | set(deleted_files())
    patterns = CONFIG["risk_check"]["high_path_patterns"] + CONFIG["risk_check"]["medium_path_patterns"]
    for name in git("ls-files", "--others", "--exclude-standard").splitlines():
        if (name.startswith(SOURCE_ROOTS) and " - Copy" not in name and not name.startswith(LEGACY_PREFIXES)
                and not is_local_only(name) and (PROJECT_ROOT / name).is_file()
                and any(re.search(p, name, re.I) for p in patterns)):
            names.add(name.replace("\\", "/"))
    return sorted(names)


DOC_SUFFIXES = (".md", ".txt", ".doc", ".docx", ".rtf", ".pdf")
LINE_COMMENT = {".cs": "//", ".ts": "//", ".js": "//", ".scss": "//", ".sql": "--", ".py": "#"}
BLOCK_COMMENT = {".cs": ("/*", "*/"), ".ts": ("/*", "*/"), ".js": ("/*", "*/"), ".scss": ("/*", "*/"),
                 ".css": ("/*", "*/"), ".sql": ("/*", "*/"), ".html": ("<!--", "-->")}


def is_doc_path(name: str) -> bool:
    """A documentation file (*.md, *.txt, *.doc ... or anything under a docs/ folder): never part of the fingerprint."""
    p = name.replace("\\", "/").lower()
    return p.endswith(DOC_SUFFIXES) or "docs" in p.split("/")[:-1]


def code_without_comments(name: str, text: str) -> str:
    """The file's text without its comment-only lines (a full-line `//`, `--` or `#` comment, or a block comment that
    starts a line and ends with nothing after it). A trailing comment after code stays, so editing one still counts as a
    change: only what is certainly a comment is ignored. Unknown file types come back unchanged."""
    suffix = Path(name).suffix.lower()
    line_mark, block = LINE_COMMENT.get(suffix), BLOCK_COMMENT.get(suffix)
    if not line_mark and not block:
        return text
    kept, closer = [], None
    for line in text.splitlines():
        stripped = line.strip()
        if closer:
            end = stripped.find(closer)
            if end == -1:
                continue
            tail, closer = stripped[end + len(closer):], None
            if tail.strip():  # code after the comment on its closing line: keep the line
                kept.append(line)
        elif line_mark and stripped.startswith(line_mark):
            continue
        elif block and stripped.startswith(block[0]):
            end = stripped.find(block[1], len(block[0]))
            if end == -1:
                closer = block[1]
            elif stripped[end + len(block[1]):].strip():
                kept.append(line)
        else:
            kept.append(line)
    return "\n".join(kept)


def diff_fingerprint(files: list) -> str:
    """Hash of the given files' names and current contents. risk_check records it; the LOW-path guard
    recomputes it, so a diff that changed after the risk check is detected (a stale LOW verdict).
    Documentation files and comment-only lines are left out: editing them does not make the gates, risk or functional
    records stale."""
    digest = hashlib.sha256()
    for name in sorted(files):
        if is_doc_path(name):
            continue
        path = PROJECT_ROOT / name
        if path.is_file():
            data = path.read_bytes()
            if Path(name).suffix.lower() in LINE_COMMENT or Path(name).suffix.lower() in BLOCK_COMMENT:
                data = code_without_comments(name, data.decode("utf-8", errors="replace")).encode("utf-8")
            content = hashlib.sha256(data).hexdigest()
        else:
            content = "missing"
        digest.update(f"{name}\0{content}\n".encode("utf-8"))
    return digest.hexdigest()


def ticket_log_dir(ticket: str):
    path = PROJECT_ROOT / CONFIG["tickets_dir"] / ticket  # D-31: DEV_TICKETS/{N}
    if path.is_dir():
        return path / "gates" / datetime.now().strftime("%Y%m%d-%H%M%S")
    return None


def record_gates_in_state(ticket: str, status: str, files: list) -> None:
    """Remember the last gates run (status + a fingerprint of the files it judged) in state.json. dev_ticket.py
    refuses to enter hand-off unless a passing run is on record and the files still match it, so the unit tests
    must have run AFTER the coding, on exactly what is being handed off."""
    state_path = PROJECT_ROOT / CONFIG["tickets_dir"] / ticket / "state.json"
    if state_path.is_file():
        gates = {"status": status, "at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                 "fingerprint": diff_fingerprint(files), "files": files}
        update_state(state_path, lambda state: state.update(gates=gates))


# ---------------------------------------------------------------------- planning

def nearest_csproj(rel_path: str):
    path = (PROJECT_ROOT / rel_path).parent
    while path != PROJECT_ROOT and PROJECT_ROOT in path.parents:
        found = sorted(path.glob("*.csproj"))
        if found:
            return found[0]
        path = path.parent
    return None


def test_projects_for(built: list) -> list:
    """Test .csproj files (same top-level folder) that reference one of the built projects."""
    tests = set()
    for proj in built:
        top = PROJECT_ROOT / proj.relative_to(PROJECT_ROOT).parts[0]
        for candidate in top.rglob("*.csproj"):
            if any(part in ("bin", "obj") for part in candidate.parts):
                continue
            text = candidate.read_text(encoding="utf-8", errors="replace")
            if not TEST_PACKAGE_RE.search(text):
                continue
            if candidate == proj or re.search(rf"[\\/]{re.escape(proj.name)}\"", text):
                tests.add(candidate)
    return sorted(tests)


def angular_project_of(name: str):
    """The configured Angular project folder a repo path belongs to (longest match, so nested folders work), or None."""
    return next((p for p in sorted(ANGULAR_PROJECTS, key=len, reverse=True) if name.startswith(p.rstrip("/") + "/")), None)


def angular_specs(files: list) -> dict:
    """{project: [spec paths relative to the project folder]}"""
    specs = {}
    for name in files:
        project = angular_project_of(name)
        if project is None or not name.endswith(".ts"):
            continue
        spec = name if name.endswith(".spec.ts") else name[:-3] + ".spec.ts"
        if (PROJECT_ROOT / spec).is_file():
            specs.setdefault(project, set()).add(spec[len(project.rstrip("/")) + 1:])
    return {p: sorted(s) for p, s in specs.items()}


def nearest_pom(rel_path: str):
    """The folder of the nearest pom.xml above a changed file (the Maven module it belongs to), or None."""
    path = (PROJECT_ROOT / rel_path).parent
    while path == PROJECT_ROOT or PROJECT_ROOT in path.parents:
        if (path / "pom.xml").is_file():
            return path
        if path == PROJECT_ROOT:
            break
        path = path.parent
    return None


def maven_gates(files: list) -> list:
    """`mvn test` per changed Maven module (the module's own ./mvnw when it has one)."""
    modules = sorted({m for m in (nearest_pom(f) for f in files if f.endswith(MAVEN_EXTENSIONS)) if m})
    gates = []
    for module in modules:
        name = f"maven-test:{module.relative_to(PROJECT_ROOT).as_posix() or '.'}"
        wrapper = module / ("mvnw.cmd" if IS_WINDOWS else "mvnw")
        exe = str(wrapper) if wrapper.is_file() else shutil.which("mvn")
        if not exe:
            gates.append({"name": name, "missing": "Maven is not installed (mvn) and the module has no ./mvnw"})
            continue
        gates.append({"name": name, "cwd": module, "cmd": [exe, "-q", "-B", "test"]})
    return gates


def is_python_test(name: str) -> bool:
    base = name.rsplit("/", 1)[-1]
    return base.endswith(".py") and (base.startswith("test_") or base.endswith("_test.py"))


def nearest_pytest_root(rel_path: str):
    """The nearest folder above a test file that holds pytest settings (pytest.ini, pyproject.toml ...), or the repo root."""
    path = (PROJECT_ROOT / rel_path).parent
    while PROJECT_ROOT in path.parents:
        if any((path / marker).is_file() for marker in PYTEST_MARKERS):
            return path
        path = path.parent
    return PROJECT_ROOT


def venv_python(folder: Path):
    """The Python of a .venv / venv in the folder or above it inside the repo (the service's own environment), or None."""
    path = folder
    while path == PROJECT_ROOT or PROJECT_ROOT in path.parents:
        for venv in (".venv", "venv"):
            exe = path / venv / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")
            if exe.is_file():
                return str(exe)
        if path == PROJECT_ROOT:
            break
        path = path.parent
    return None


def python_gates(files: list, skip: set) -> list:
    """py-compile (syntax) for every changed .py file; pytest for the changed test files, grouped by their pytest root."""
    present = [f for f in files if f.endswith(".py") and (PROJECT_ROOT / f).is_file()]
    gates = []
    if present and "py-compile" not in skip:
        for number, batch in enumerate(batches(present), 1):
            gates.append({"name": "py-compile" if number == 1 else f"py-compile#{number}", "cwd": PROJECT_ROOT,
                          "cmd": [sys.executable, "-m", "py_compile", *batch]})
    if "pytest" not in skip:
        roots = {}
        for name in present:
            if is_python_test(name):
                roots.setdefault(nearest_pytest_root(name), []).append(name)
        for root, tests in sorted(roots.items()):
            label = root.relative_to(PROJECT_ROOT).as_posix() or "."
            exe = venv_python(root)
            if not exe:
                gates.append({"name": f"pytest:{label}", "missing": f"no .venv / venv in {label} or above it: create the "
                              "service's environment (and install its packages) to run its tests"})
                continue
            rel = [str((PROJECT_ROOT / t).relative_to(root)) for t in tests]
            gates.append({"name": f"pytest:{label}", "cwd": root, "cmd": [exe, "-m", "pytest", "-q", *rel]})
    return gates


def is_review_ticket(ticket: str) -> bool:
    """The work folder is a reviewer's read-only review of someone else's branch (state.kind == review)."""
    path = PROJECT_ROOT / CONFIG["tickets_dir"] / (ticket or "") / "state.json"
    return bool(ticket) and path.is_file() and json.loads(path.read_text(encoding="utf-8")).get("kind") == "review"


def tracked_changes() -> set:
    """Tracked files git reports as modified (porcelain status). A file the pre-commit fixers rewrote from CRLF to LF shows
    as modified here although `git diff` is empty (core.autocrlf), so the diff cannot be used for this."""
    fields = git("status", "--porcelain", "-z", "--untracked-files=no").split("\0")
    return {f[3:] for f in fields if len(f) > 3}


def restore_gate_changes(before: set) -> list:
    """The pre-commit fixers (whitespace, end of file, line endings) rewrite files in place. In a review the checkout is
    read-only and was clean, so whatever the gates changed since `before` is put back from HEAD. -> the files restored."""
    touched = sorted(tracked_changes() - before)
    for i in range(0, len(touched), 50):  # keep the command line short
        git("restore", "--source=HEAD", "--staged", "--worktree", "--", *touched[i:i + 50], check=True)
    return touched


def ticket_db_files(ticket: str) -> list:
    path = PROJECT_ROOT / CONFIG["tickets_dir"] / (ticket or "") / "state.json"
    listed = ((json.loads(path.read_text(encoding="utf-8")).get("db") or {}).get("files") or []) if ticket and path.is_file() else []
    if not ticket:
        return listed
    import db_checks  # lazy: it imports this module. DB files changed but never listed in state.db.files still count
    detected, _problem = db_checks.detect_db_files(db_checks.load_ticket_db(ticket))
    return sorted(set(listed) | set(detected))


def batches(files: list) -> list:
    """Split a long file list so each pre-commit command line stays under the OS limit."""
    result, current, size = [], [], 0
    for name in files:
        if current and size + len(name) + 3 > MAX_CMDLINE:
            result.append(current)
            current, size = [], 0
        current.append(name)
        size += len(name) + 3
    return result + ([current] if current else [])


def resolve_cmd(cmd: list):
    """(argv, problem). The tool is resolved to a full path so it runs WITHOUT a shell. A .cmd/.bat launcher
    (npx) is still parsed by cmd.exe, so an argument holding a cmd metacharacter is refused rather than risked."""
    exe = shutil.which(cmd[0])
    if not exe:
        return None, f"'{cmd[0]}' was not found on PATH"
    if exe.lower().endswith((".cmd", ".bat")):
        bad = [a for a in cmd[1:] if CMD_UNSAFE_CHARS & set(a)]
        if bad:
            return None, f"argument(s) {bad[:3]} contain cmd.exe metacharacters; rename the file(s) or run the gate by hand"
    return [exe, *cmd[1:]], None


def plan_gates(files: list, skip: set, ticket: str = None) -> list:
    gates = []
    # D-51: DB repo checks whenever the ticket has DB files, or C# changed (new RunSp* calls).
    if "db-checks" not in skip and (ticket_db_files(ticket) or any(f.endswith(".cs") for f in files)):
        cmd = [sys.executable, str(SCRIPT_DIR / "db_checks.py")] + (["--ticket", ticket] if ticket else [])
        gates.append({"name": "db-checks", "cwd": SCRIPT_DIR, "cmd": cmd, "stop_code": 3})
    if "pre-commit" not in skip and (PROJECT_ROOT / ".pre-commit-config.yaml").is_file():
        if shutil.which("pre-commit"):
            for number, batch in enumerate(batches(files), 1):
                gates.append({"name": "pre-commit" if number == 1 else f"pre-commit#{number}", "cwd": PROJECT_ROOT,
                              "cmd": ["pre-commit", "run", "--files", *batch], "retry_once": True})
        else:
            gates.append({"name": "pre-commit", "missing": "pre-commit is not installed (pip install pre-commit)"})

    cs_files = [f for f in files if f.endswith((".cs", ".csproj"))]
    built = sorted({p for p in (nearest_csproj(f) for f in cs_files) if p})
    if "dotnet-build" not in skip:
        for proj in built:
            gates.append({"name": f"dotnet-build:{proj.stem}", "cwd": PROJECT_ROOT,
                          "cmd": ["dotnet", "build", str(proj), "-nologo", "-v", "q", "-clp:ErrorsOnly"]})
    if "dotnet-test" not in skip:
        for proj in test_projects_for(built):
            gates.append({"name": f"dotnet-test:{proj.stem}", "cwd": PROJECT_ROOT,
                          "cmd": ["dotnet", "test", str(proj), "-nologo", "-v", "q"]})

    if "ng-test" not in skip:
        for project, specs in angular_specs(files).items():
            # Batched like pre-commit: hundreds of --include flags exceed the Windows command-line limit
            for number, batch in enumerate(batches([f"--include={s}" for s in specs]), 1):
                cmd = ["npx", "ng", "test", "--watch=false", "--browsers=ChromeHeadless", "--progress=false", *batch]
                name = f"ng-test:{project}" if number == 1 else f"ng-test:{project}#{number}"
                gates.append({"name": name, "cwd": PROJECT_ROOT / project, "cmd": cmd})

    if "maven-test" not in skip and GATES_CFG.get("maven"):
        gates += maven_gates(files)
    if GATES_CFG.get("python"):
        gates += python_gates(files, skip)
    return gates


# ---------------------------------------------------------------------- running

def run_gate(gate: dict, log_dir: Path) -> dict:
    if gate.get("missing"):
        return {"name": gate["name"], "status": "SKIPPED", "reason": gate["missing"]}
    log_path = log_dir / (re.sub(r"[^\w.-]", "_", gate["name"]) + ".log")
    attempts = 2 if gate.get("retry_once") else 1
    argv, problem = resolve_cmd(gate["cmd"])
    if problem:
        return {"name": gate["name"], "status": "FAIL", "exit_code": None, "attempts": 0, "log": str(log_path),
                "errors": [problem]}
    env = {k: v for k, v in os.environ.items() if k not in SECRET_ENV}
    for attempt in range(1, attempts + 1):
        try:
            proc = subprocess.run(argv, cwd=gate["cwd"], capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", env=env, timeout=GATE_TIMEOUT)
        except subprocess.TimeoutExpired:
            return {"name": gate["name"], "status": "FAIL", "exit_code": None, "attempts": attempt,
                    "log": str(log_path), "errors": [f"timed out after {GATE_TIMEOUT}s (gate_timeout_seconds)"]}
        output = (proc.stdout or "") + (proc.stderr or "")
        with log_path.open("a", encoding="utf-8") as log:
            log.write(f"$ {' '.join(gate['cmd'])}  (attempt {attempt})\n{output}\n")
        # pre-commit fixers (whitespace, EOF, line endings) fail the run in which they
        # rewrite files; a second run tells us whether anything is still really wrong.
        if proc.returncode == 0:
            break
    errors = [l.strip() for l in output.splitlines() if ERROR_LINE_RE.search(l)][:MAX_ERROR_LINES]
    status = "PASS" if proc.returncode == 0 else ("STOP" if proc.returncode == gate.get("stop_code") else "FAIL")
    if gate["name"] == "db-checks":  # its own ERROR/STOP/WARNING lines are the useful part
        errors = [l.strip() for l in output.splitlines() if l.strip().startswith(("STOP", "ERROR", "WARNING"))][:MAX_ERROR_LINES]
    return {"name": gate["name"], "status": status,
            "exit_code": proc.returncode, "attempts": attempt, "log": str(log_path),
            "errors": [] if status == "PASS" and gate["name"] != "db-checks" else errors}


TICKET_ARG_RE = re.compile(r"(?:[A-Za-z][A-Za-z0-9_]*[-_]|#)?(\d+)")


def ticket_arg(value):
    """Validate a --ticket value ('18474', 'C2MC-18474', '#18474') -> digits; None stays None."""
    if value is None:
        return None
    match = TICKET_ARG_RE.fullmatch(str(value).strip())
    if not match:
        raise ValueError(f"'{value}' is not a ticket number (expected digits, e.g. 18474).")
    return match.group(1)


def guarded(main):
    """Run main(); any uncaught error still ends with one RESULT line (exit 1) instead of a bare traceback."""
    try:
        return main()
    except Exception as exc:  # noqa: BLE001 - the RESULT line is the contract
        print("RESULT: " + json.dumps({"status": "ERROR", "message": f"{type(exc).__name__}: {exc}"}))
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ticket", help="Ticket number - logs go under DEV_TICKETS/{N}/gates/.")
    parser.add_argument("--files", nargs="+", help="Explicit repo-relative files (default: detect from git).")
    parser.add_argument("--skip", nargs="+", default=[],
                        choices=["pre-commit", "dotnet-build", "dotnet-test", "ng-test", "maven-test", "db-checks",
                                 "py-compile", "pytest"])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    args.ticket = ticket_arg(args.ticket)
    if "db-checks" in args.skip and ticket_db_files(args.ticket):
        print("RESULT: " + json.dumps({"status": "ERROR", "message": "db-checks cannot be skipped: this ticket has "
                                       "DB repo changes, and that gate is what stops destructive scripts."}))
        return 1

    base_warning = None if args.files else refresh_base()
    if base_warning:
        print(f"WARNING: {base_warning}; the file list may include newer {BASE} changes.")
    files = [f.replace("\\", "/") for f in args.files] if args.files else changed_files()
    files = [f for f in files if not is_local_only(f)]
    local_only = local_only_changes()
    if local_only:
        print(f"Ignored local-only changes (never part of the ticket): {', '.join(local_only)}")
    record = bool(args.ticket and not args.files and not args.dry_run)  # a hand-picked subset is never recorded
    if not files:
        if record:
            record_gates_in_state(args.ticket, "NO_CHANGES", [])
        print("RESULT: " + json.dumps({"status": "NO_CHANGES", "files": [], "local_only_ignored": local_only}))
        return 0
    print(f"Changed files ({len(files)}):")
    for name in files:
        print(f"  {name}")

    gates = plan_gates(files, set(args.skip), args.ticket)
    if args.dry_run:
        for gate in gates:
            print(f"[plan] {gate['name']}: {gate.get('missing') or ' '.join(gate['cmd'])}")
        print("RESULT: " + json.dumps({"status": "DRY_RUN", "files": files, "gates": [g["name"] for g in gates]}))
        return 0

    log_dir = (ticket_log_dir(args.ticket) if args.ticket else None) or Path(tempfile.mkdtemp(prefix="dev_gates_"))
    log_dir.mkdir(parents=True, exist_ok=True)
    results = []
    review = is_review_ticket(args.ticket)
    before = tracked_changes() if review else set()
    for gate in gates:
        print(f"-> {gate['name']} ...", flush=True)
        outcome = run_gate(gate, log_dir)
        results.append(outcome)
        print(f"   {outcome['status']}" + (f" ({outcome.get('reason')})" if outcome.get("reason") else ""))
        for line in outcome.get("errors", []):
            print(f"     {line}")
    restored = restore_gate_changes(before) if review else []
    if restored:
        print(f"Review checkout: the gates rewrote {len(restored)} file(s) (pre-commit fixers: whitespace, end of file, "
              f"line endings); restored from HEAD, the author's committed code is what is judged.")

    # A gate that didn't run (tool missing, or --skip) is not a pass: INCOMPLETE until it really runs.
    skipped = [r["name"] for r in results if r["status"] == "SKIPPED"] + [f"--skip {s}" for s in args.skip]
    status = ("STOP" if any(r["status"] == "STOP" for r in results)
              else "FAIL" if any(r["status"] == "FAIL" for r in results)
              else "INCOMPLETE" if skipped else "PASS")
    if record:
        record_gates_in_state(args.ticket, status, files)
    if args.ticket:  # D-53 quality metrics
        import metrics
        metrics.record(PROJECT_ROOT, CONFIG["tickets_dir"], args.ticket, "gates", status=status,
                       failed=[r["name"] for r in results if r["status"] in ("FAIL", "STOP")])
    print("RESULT: " + json.dumps({"status": status, "files": files, "gates": results, "log_dir": str(log_dir),
                                   "skipped": skipped, "local_only_ignored": local_only,
                                   **({"restored_after_gates": restored} if restored else {}),
                                   **({"base_warning": base_warning} if base_warning else {})}, ensure_ascii=False))
    return 0 if status == "PASS" else 2


if __name__ == "__main__":
    sys.exit(guarded(main))
