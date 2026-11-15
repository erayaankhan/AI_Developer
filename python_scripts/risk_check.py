#!/usr/bin/env python3
"""
Risk re-check on the actual diff - step 7b of the dev-ticket workflow.

Claude sets a risk tier in BRIEF.md from its reading of the ticket. Before a LOW ticket
may take the automated path (commit, push, status moves, staging deploy), this script
checks what the diff really touches and returns the *minimum* tier the change allows.
Claude must use max(its tier, min_tier); it can never lower the result.

Rules (config "risk_check" in dev_ticket.config.json):
    HIGH   gateway service, shared libraries (DLL sources), encryption/Vault code,
           auth guards/interceptors/JWT, DDL in .sql files, AFD3 final_json persistence
    MEDIUM appsettings / publish profiles, stored procedures (.sql without DDL),
           more than one app/service touched, more than max_low_changed_lines lines
    CANVAS (D-26) WFD2 diagram / AFD3 canvas code -> MEDIUM unless the change is label/color-only
           (.scss/.css/.html, or code lines that differ only in string literals / hex colors)
    DB     (D-20) any file in the DB repo (C2M_CI_DB) for this ticket: SP/DML -> MEDIUM,
           DDL -> HIGH. DB files come from state.json "db.files" or --db-files.

Changed files = committed on the branch + tracked working-tree changes + new untracked
code files (same detection as local_gates.py).

    python risk_check.py --ticket 18474      # also records the result in state.json
    python risk_check.py --files a.ts b.cs   # explicit list

Ends with: RESULT: {"status": "LOW_OK"|"BUMPED", "min_tier": ..., "reasons": [...], ...}
"""

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from local_gates import (CONFIG, PROJECT_ROOT, BASE, diff_fingerprint, guarded,  # noqa: E402
                         is_local_only, refresh_base, risk_files, ticket_arg)
from state_io import update_state  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

RULES = CONFIG["risk_check"]
TIERS = ["LOW", "MEDIUM", "HIGH"]
DDL_RE = re.compile(r"^\+\s*(CREATE|ALTER|DROP|RENAME)\s+(TABLE|INDEX|VIEW|DATABASE|SCHEMA)\b", re.I | re.M)


def git(*args, check: bool = True) -> str:
    """stdout of a git call. A failed call RAISES (guarded() turns it into RESULT: ERROR): an empty string
    would read as "no diff" and let the risk check fail open."""
    proc = subprocess.run(["git", "-C", str(PROJECT_ROOT), "-c", "core.quotepath=false", *args],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        if check:
            raise RuntimeError(f"git {' '.join(args)} failed: {(proc.stderr or proc.stdout).strip()}")
        return ""
    return proc.stdout


def diff_base() -> str:
    return git("merge-base", f"origin/{BASE}", "HEAD", check=False).strip() or f"origin/{BASE}"


def changed_lines(files: list, base: str) -> int:
    """Added + deleted lines vs the merge base, including uncommitted edits and new files."""
    total, tracked = 0, set()
    for line in git("diff", "--numstat", base, "--", *files).splitlines():
        added, deleted, name = (line.split("\t") + ["", "", ""])[:3]
        tracked.add(name)
        if added.isdigit() and deleted.isdigit():
            total += int(added) + int(deleted)
    for name in files:
        if name not in tracked and (PROJECT_ROOT / name).is_file():
            total += len((PROJECT_ROOT / name).read_text(encoding="utf-8", errors="replace").splitlines())
    return total


LITERAL_RE = re.compile(r"""'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*"|`(?:[^`\\$]|\\.|\$(?!\{))*`|#[0-9a-fA-F]{3,8}\b""")
# A template literal containing ${...} is code, not a label: the backtick branch above deliberately won't
# match it, so a changed expression inside it shows up as a real difference.


def cosmetic_only(name: str, base: str) -> bool:
    """D-26: a canvas change counts as simple when it is label/color-only.

    Style/template files (.scss/.css/.html) always count as cosmetic. For code files, every
    changed line must differ only in string literals or hex colors: after blanking those
    out, the removed and added lines must be identical. A new canvas code file is never
    cosmetic.
    """
    if Path(name).suffix.lower() in RULES["canvas_cosmetic_extensions"]:
        return True
    diff = git("diff", "-U0", base, "--", name)
    if not diff:
        return False  # untracked/new file
    removed, added = [], []
    for line in diff.splitlines():
        if line.startswith(("---", "+++")):
            continue
        if line.startswith("-"):
            removed.append(LITERAL_RE.sub("''", line[1:]).strip())
        elif line.startswith("+"):
            added.append(LITERAL_RE.sub("''", line[1:]).strip())
    return bool(removed or added) and removed == added  # same order: moving statements around is not cosmetic


def untracked_names() -> set:
    return set(git("ls-files", "--others", "--exclude-standard").splitlines())


def app_of(path: str) -> str:
    return path.split("/", 1)[0]


def is_doc_path(path: str) -> bool:
    """A documentation file (*.md, *.txt, or anything under a docs/ folder): never code, so no canvas or size risk.
    Same rule as dev_ticket._is_doc_path."""
    p = path.replace("\\", "/").lower()
    return p.endswith((".md", ".txt")) or "docs" in p.split("/")[:-1]


def assess(files: list, db_files: list = ()) -> dict:
    base = diff_base()
    reasons = assess_db(list(db_files))  # (tier, reason)

    for name in files:
        for tier, key in (("HIGH", "high_path_patterns"), ("MEDIUM", "medium_path_patterns")):
            for pattern in RULES[key]:
                if re.search(pattern, name, re.I):
                    reasons.append((tier, f"{name} matches {pattern!r}"))
                    break

        if name.lower().endswith(".sql"):
            diff_text = git("diff", base, "--", name)
            if not diff_text and (PROJECT_ROOT / name).is_file():  # new untracked file
                diff_text = "\n".join("+" + l for l in (PROJECT_ROOT / name).read_text(
                    encoding="utf-8", errors="replace").splitlines())
            if DDL_RE.search(diff_text):
                reasons.append(("HIGH", f"{name} contains DDL"))
            else:
                reasons.append(("MEDIUM", f"{name} is a SQL/SP change"))

    persistence = [n for n in files if re.search(RULES["afd3_persistence_path"], n)]
    if persistence:
        new_text = "".join((PROJECT_ROOT / n).read_text(encoding="utf-8", errors="replace")
                           for n in persistence if n in untracked_names())  # a diff can't show untracked files
        if "final_json" in git("diff", base, "--", *persistence) + new_text:
            reasons.append(("HIGH", "AFD3 final_json persistence code changed"))

    for name in files:
        if is_doc_path(name):
            continue
        if any(re.search(p, name) for p in RULES["canvas_path_patterns"]) and not cosmetic_only(name, base):
            reasons.append(("MEDIUM", f"{name}: canvas code change that isn't label/color-only"))

    apps = sorted({app_of(n) for n in files if "/" in n})
    if len(apps) > 1:
        reasons.append(("MEDIUM", f"more than one app/service touched: {', '.join(apps)}"))

    lines = changed_lines([n for n in files if not is_doc_path(n)], base)
    if lines > RULES["max_low_changed_lines"]:
        reasons.append(("MEDIUM", f"{lines} changed lines > {RULES['max_low_changed_lines']}"))

    return _finish(reasons, list(files) + [f"DB:{n}" for n in db_files], lines, apps)


def assess_db(db_files: list) -> list:
    """D-20: any SP/DML change in the DB repo -> at least MEDIUM; DDL -> HIGH."""
    repo = (Path(__file__).resolve().parent / CONFIG["db"]["repo_path"]).resolve()
    reasons = []
    for name in db_files:
        path = repo / name
        text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
        diff = subprocess.run(["git", "-C", str(repo), "diff", "HEAD", "--", name], capture_output=True,
                              text=True, encoding="utf-8", errors="replace").stdout
        added = diff or "\n".join("+" + l for l in text.splitlines())  # new/untracked file: all lines added
        if DDL_RE.search(added):
            reasons.append(("HIGH", f"DB: {name} contains DDL"))
        else:
            reasons.append(("MEDIUM", f"DB: {name} is an SP/DML change"))
    return reasons


def _finish(reasons: list, files: list, lines: int, apps: list) -> dict:
    min_tier = max((t for t, _ in reasons), key=TIERS.index, default="LOW")
    return {"status": "LOW_OK" if min_tier == "LOW" else "BUMPED", "min_tier": min_tier,
            "reasons": [f"{t}: {r}" for t, r in reasons], "files": len(files),
            "changed_lines": lines, "apps": apps}


def state_db_files(ticket: str) -> list:
    """DB repo files recorded for this ticket in step 4a (state.json "db": {"files": [...]})."""
    state_path = PROJECT_ROOT / CONFIG["tickets_dir"] / ticket / "state.json"
    if state_path.is_file():
        return (json.loads(state_path.read_text(encoding="utf-8")).get("db") or {}).get("files") or []
    return []


def record_in_state(ticket: str, outcome: dict, files: list, auto_detected: bool) -> None:
    state_path = PROJECT_ROOT / CONFIG["tickets_dir"] / ticket / "state.json"  # D-31
    if state_path.is_file():
        # fingerprint: lets the LOW-path guard refuse a verdict the diff has outgrown (dev_ticket.low_path_problems)
        record = dict(outcome, at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                      fingerprint=diff_fingerprint(files), fp_files=files, fp_auto=auto_detected)
        update_state(state_path, lambda state: state.update(risk_check=record))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ticket")
    parser.add_argument("--files", nargs="+")
    parser.add_argument("--db-files", nargs="+", help="DB repo files (default: state.json db.files).")
    args = parser.parse_args()
    args.ticket = ticket_arg(args.ticket)

    base_warning = None if args.files else refresh_base()  # fresh origin/master first: merge base and line counts
    if base_warning:
        print(f"WARNING: {base_warning}; the file list and line count may include newer {BASE} changes.")
    files = [f.replace("\\", "/") for f in args.files] if args.files else risk_files()
    files = [f for f in files if not is_local_only(f)]  # D-27: environment.ts etc. never count
    db_files = [f.replace("\\", "/") for f in args.db_files] if args.db_files else (
        state_db_files(args.ticket) if args.ticket else [])
    if args.ticket and not args.db_files:  # DB files changed but never listed in state still count
        import db_checks
        detected, _problem = db_checks.detect_db_files(db_checks.load_ticket_db(args.ticket))
        db_files = sorted(set(db_files) | set(detected))
    if not files and not db_files:
        print("RESULT: " + json.dumps({"status": "NO_CHANGES"}))
        return 0
    outcome = assess(files, db_files)
    if base_warning:
        outcome["base_warning"] = base_warning
    for reason in outcome["reasons"]:
        print(f"  {reason}")
    if args.ticket and args.files:
        # A verdict on a hand-picked subset must never gate the LOW path: report it, but don't record it.
        outcome["recorded"] = False
        print("  (--files given: result NOT recorded in state.json; re-run without --files to record)")
    elif args.ticket:
        record_in_state(args.ticket, outcome, files, auto_detected=True)
        import metrics  # D-53
        metrics.record(PROJECT_ROOT, CONFIG["tickets_dir"], args.ticket, "risk_check",
                       min_tier=outcome["min_tier"], bumped=outcome["status"] == "BUMPED",
                       reasons=len(outcome["reasons"]))
    print("RESULT: " + json.dumps(outcome, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(guarded(main))
