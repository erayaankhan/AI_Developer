"""Automated gate-7a fix rounds (improvements item 3).

When the unit-test gate fails, `dt gate-round N` reads the failures from the last gates run, says which of them
already fail on the ticket's start commit (so they are not the ticket's fault), groups them by cause, counts the
rounds already used from metrics.jsonl and logs the next `fix_round` itself, so the cap of MAX_ROUNDS is enforced by
a script and not by memory of the chat.

What Claude may then edit without asking (boundary A, see SKILL.md 7a): test files only, setup patterns only.
Anything that changes an expected value or an assertion, touches production code, or looks like a product question
stops the loop and goes to the developer.

Pure functions here are unit-tested in test_gate_rounds.py; `start_check` is the only part that touches git and npm.
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

MAX_ROUNDS = 3
STEP = "7a"

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# "Chrome Headless 153.0.0.0 (Windows 10) <suite> <test> FAILED" (the "Executed n of m (k FAILED)" lines don't match)
FAILED_RE = re.compile(r"\(Windows[^)]*\)\s+(?P<name>.+?)\s+FAILED\s*$")

NG0100 = "NG0100"


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def parse_karma_failures(text: str) -> list:
    """[{test, error}] in first-seen order. Karma repeats each failure in its progress and summary output, so
    they are de-duplicated by test name; the error is the first non-empty line after the FAILED line."""
    lines = strip_ansi(text).splitlines()
    failures, seen = [], set()
    for index, line in enumerate(lines):
        match = FAILED_RE.search(line)
        if not match or "Executed" in line:
            continue
        name = match.group("name").strip()
        if name in seen:
            continue
        seen.add(name)
        error = ""
        for follow in lines[index + 1:index + 6]:
            if follow.strip() and not FAILED_RE.search(follow):
                error = follow.strip()
                break
        failures.append({"test": name, "error": error})
    return failures


def classify(error: str) -> str:
    """NG0100 (the dev-mode change-detection check: a test setup problem), ASSERTION (an expectation failed),
    RUNTIME (a TypeError / missing function in the code under test or its mocks) or OTHER."""
    if NG0100 in error or "ExpressionChangedAfterItHasBeenChecked" in error:
        return "NG0100"
    if error.startswith("Expected") or "to have been called" in error:
        return "ASSERTION"
    if "TypeError" in error or "is not a function" in error or "Cannot read properties" in error:
        return "RUNTIME"
    return "OTHER"


def rounds_used(events: list, step: str = STEP) -> int:
    return sum(1 for e in events if e.get("event") == "fix_round" and str(e.get("step")) == step)


def plan_round(failures: list, used: int, cap: int = MAX_ROUNDS) -> dict:
    """Decide what the next round is. failures carry `category` and, when verified, `pre_existing` (True/False/None)."""
    if not failures:
        return {"status": "NO_FAILURES", "used": used, "cap": cap}
    if used >= cap:
        return {"status": "ROUND_CAP", "used": used, "cap": cap}
    pre = [f for f in failures if f.get("pre_existing") is True]
    new = [f for f in failures if f.get("pre_existing") is False]
    unknown = [f for f in failures if f.get("pre_existing") is None]
    return {"status": "ROUND_READY", "used": used, "cap": cap, "attempt": used + 1,
            # Boundary A: only a pre-existing NG0100 test-setup failure may be edited without asking.
            "auto_fixable": [f for f in pre if f["category"] == "NG0100"],
            "needs_decision": [f for f in pre if f["category"] != "NG0100"],
            "ticket_failures": new,
            "unverified": unknown}


# ---------------------------------------------------------------------- the start-commit check

def remove_link(path: Path) -> None:
    """Remove a directory link (junction or symlink) WITHOUT following it. os.rmdir removes only the link;
    shutil.rmtree / `git worktree remove` on a path that still holds a junction could delete the target."""
    if path.is_symlink() and os.name != "nt":
        os.unlink(path)  # POSIX: a symlink is removed with unlink (rmdir fails on it); unlink never follows the link
    elif path.is_symlink() or (path.exists() and _is_junction(path)):
        os.rmdir(path)  # Windows: a directory symlink or junction is removed with rmdir


def _is_junction(path: Path) -> bool:
    check = getattr(os.path, "isjunction", None)
    return bool(check and check(path))


def make_junction(link: Path, target: Path) -> None:
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)
    else:
        link.symlink_to(target, target_is_directory=True)


def start_check(root: Path, start_head: str, project: str, specs: list, work_dir: Path, timeout: int,
                run=subprocess.run) -> dict:
    """Run `specs` of `project` on a temporary checkout of `start_head` and return
    {"ok": bool, "failures": [{test, error}], "message": str}. node_modules is linked from the real project, the
    checkout is removed afterwards (link first), and nothing in the working tree is touched."""
    if not specs:
        return {"ok": False, "failures": [], "message": "no specs to check"}
    worktree = work_dir / "_startcheck"
    link = worktree / project / "node_modules"
    source = root / project / "node_modules"
    if not source.is_dir():
        return {"ok": False, "failures": [], "message": f"{project}/node_modules is missing: run npm install first"}
    try:
        run(["git", "worktree", "add", "--detach", str(worktree), start_head], cwd=root, check=True,
            capture_output=True, text=True)
        make_junction(link, source)
        npx = shutil.which("npx") or "npx"
        cmd = [npx, "ng", "test", "--watch=false", "--browsers=ChromeHeadless", "--progress=false"]
        cmd += [f"--include={s}" for s in specs]
        proc = run(cmd, cwd=worktree / project, capture_output=True, text=True, encoding="utf-8", errors="replace",
                   timeout=timeout)
        output = (proc.stdout or "") + (proc.stderr or "")
        return {"ok": True, "failures": parse_karma_failures(output), "exit_code": proc.returncode,
                "message": "start-commit run finished"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "failures": [], "message": f"start-commit run timed out after {timeout}s"}
    except subprocess.CalledProcessError as exc:
        return {"ok": False, "failures": [], "message": f"{exc.cmd[:3]} failed: {(exc.stderr or '')[:200]}"}
    finally:
        remove_link(link)  # first, so nothing below can reach the real node_modules
        if worktree.exists():
            run(["git", "worktree", "remove", "--force", str(worktree)], cwd=root, capture_output=True, text=True)
            run(["git", "worktree", "prune"], cwd=root, capture_output=True, text=True)


def mark_pre_existing(failures: list, start_failures: list, checked: bool) -> list:
    """Annotate each failure: True when the same test also fails on the start commit, False when it does not,
    None when the start-commit check could not run."""
    names = {f["test"] for f in start_failures}
    out = []
    for failure in failures:
        item = dict(failure, category=classify(failure["error"]))
        item["pre_existing"] = (failure["test"] in names) if checked else None
        out.append(item)
    return out
