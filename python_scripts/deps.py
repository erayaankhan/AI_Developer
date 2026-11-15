"""Fresh dependencies at the start of a ticket (improvements item 12).

`dt deps N` runs once the planned files are known: right before the baseline on a new branch, or before the first
edit on an existing branch / resume. A stale node_modules (Angular 20 installed, 22 required) hid every unit-test
result until someone ran `npm i` in the middle of a ticket; doing it up front, on a clean tree, keeps the baseline
meaningful.

All three active Angular apps keep package-lock.json out of git (.gitignore), so the start is:
    delete the local lockfile (only when it is NOT tracked), then `npm install --no-package-lock`.
--no-package-lock neither writes a lockfile nor rewrites package.json, so the tree is still clean afterwards and the
baseline's clean-tree check needs no special case. A peer-dependency conflict is reported, never worked around:
only the developer's explicit yes allows a retry with --legacy-peer-deps (node_modules only, nothing tracked).
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

NPM_ARGS = ["install", "--no-package-lock", "--no-audit", "--no-fund"]
CONFLICT_RE = re.compile(r"ERESOLVE|Could not resolve dependency|peer dep", re.IGNORECASE)
VERSION_RE = re.compile(r"(\d+)\.(\d+)")


def projects_for_files(files: list, projects: tuple) -> list:
    """The Angular projects the planned files belong to, in first-seen order."""
    found = []
    for name in files:
        top = name.replace("\\", "/").split("/", 1)[0]
        if top in projects and top not in found:
            found.append(top)
    return found


def is_tracked(root: Path, rel: str, run=subprocess.run) -> bool:
    return run(["git", "ls-files", "--error-unmatch", rel], cwd=root, capture_output=True).returncode == 0


def tracked_file_modified(root: Path, rel: str, run=subprocess.run) -> bool:
    proc = run(["git", "status", "--porcelain", "--", rel], cwd=root, capture_output=True, text=True)
    return bool((proc.stdout or "").strip())


def major_minor(version: str):
    match = VERSION_RE.search(version or "")
    return (int(match.group(1)), int(match.group(2))) if match else None


def angular_core_check(app: Path) -> dict:
    """Declared (package.json) vs installed (node_modules) @angular/core: {declared, installed, match}."""
    try:
        declared = json.loads((app / "package.json").read_text(encoding="utf-8"))["dependencies"]["@angular/core"]
        installed = json.loads((app / "node_modules" / "@angular" / "core" / "package.json")
                               .read_text(encoding="utf-8"))["version"]
    except (OSError, KeyError, ValueError):
        return {"declared": None, "installed": None, "match": None}
    return {"declared": declared, "installed": installed, "match": major_minor(declared) == major_minor(installed)}


def tail(text: str, lines: int = 15) -> list:
    return [l for l in (text or "").splitlines() if l.strip()][-lines:]


def prepare(root: Path, project: str, timeout: int = 1800, legacy: bool = False, dry_run: bool = False,
            run=subprocess.run, which=shutil.which) -> dict:
    """Delete the untracked local lockfile and install. Returns {status, ...}:
    DEPS_READY, DEPS_CONFLICT (peer conflict: show it, ask), DEPS_ERROR, DEPS_DIRTY (the install changed
    package.json: never expected) or NO_PACKAGE_JSON."""
    app = root / project
    if not (app / "package.json").is_file():
        return {"status": "NO_PACKAGE_JSON", "project": project, "message": f"{project}/package.json not found"}

    lock = app / "package-lock.json"
    lock_note = "no lockfile"
    if lock.is_file():
        if is_tracked(root, f"{project}/package-lock.json", run):
            lock_note = "lockfile is tracked in git: left alone"
        elif dry_run:
            lock_note = "local lockfile would be deleted (dry run)"
        else:
            lock.unlink()
            lock_note = "local lockfile deleted"

    cmd = [which("npm") or "npm", *NPM_ARGS]
    if legacy:
        cmd.append("--legacy-peer-deps")
    if dry_run:
        cmd.append("--dry-run")
    try:
        proc = run(cmd, cwd=app, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"status": "DEPS_ERROR", "project": project, "lockfile": lock_note,
                "message": f"npm install timed out after {timeout}s"}
    output = (proc.stdout or "") + (proc.stderr or "")
    base = {"project": project, "lockfile": lock_note, "command": " ".join(cmd[1:]), "legacy_peer_deps": legacy,
            "dry_run": dry_run}
    if proc.returncode != 0:
        status = "DEPS_CONFLICT" if CONFLICT_RE.search(output) else "DEPS_ERROR"
        return dict(base, status=status, output=tail(output),
                    message=("Peer-dependency conflict: show it to the developer; only on their yes retry with "
                             "--legacy-peer-deps (node_modules only)." if status == "DEPS_CONFLICT"
                             else "npm install failed: see output."))
    if tracked_file_modified(root, f"{project}/package.json", run):
        return dict(base, status="DEPS_DIRTY",
                    message="The install changed package.json. It should not: show the developer the diff.")
    return dict(base, status="DEPS_READY", angular_core=angular_core_check(app),
                message="Dependencies are fresh." if not dry_run else "Dry run: nothing was installed.")
