"""Review aid commands: dt review-brief, dt review-check."""

import re
import subprocess
import sys
from pathlib import Path

from assembla_api import bare_number

from develop_ticket import common
from develop_ticket.common import CONFIG, result, ticket_dir


REVIEWER_INPUTS_OVERRIDE = """## Inputs override (a reviewer's review of a developer's ticket)

There is no BRIEF.md or PLAN.md from the author. Read instead: `DEV_TICKETS\\{N}\\ticket.md`; `hand-off\\summary.md`;
`hand-off\\handoff.md` (the author's evidence, not authority); `hand-off\\author-review.md` (the author's own pre-review: a second
opinion, do not rely on it); `PLAN.md` (the reviewer's functional checks only) and `reviews\\functional-checks.md` if present;
`attachments\\`. The WF diff is `git diff origin/master...HEAD`; the checkout is clean, expect no uncommitted changes. Derive the
acceptance criteria from the ticket and the summary and list them in your coverage table. Where REVIEW.md names BRIEF.md: take the
README impact from the HANDOFF (`readme_impact`) and, for a defect, grep the root-cause pattern yourself (no sweep record exists).
You are not the reviewer of record; a human reviewer decides. Write only `reviews\\{out}`; change no other file and run no git write.
"""


def _db_check_lines(number: str) -> str:
    """STOP / ERROR / WARN lines db_checks prints for this ticket's DB files; says so when it could not run."""
    proc = subprocess.run([sys.executable, str(common.SCRIPT_DIR / "db_checks.py"), "--ticket", number],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    lines = [ln.strip() for ln in proc.stdout.splitlines() if re.match(r"\s*(STOP|ERROR|WARN)\b", ln)]
    if not lines and proc.returncode not in (0, 1):
        return f"db_checks could not run (exit {proc.returncode}); run it yourself: `python db_checks.py --ticket {number}`."
    return "\n".join(f"- {ln}" for ln in lines) or "none"


def cmd_review_brief(_api, args) -> int:
    """Step 7e / R4: write the filled-in REVIEW.md for the review sub-agent to `reviews\\REVIEW_BRIEF[-roundN].md`.

    Claude passes the sub-agent the path, never the text, so the brief cannot be cut off or mistyped. Values come from
    state.json and ticket.md; `--reviewer` adds the reviewer's Inputs override (no author BRIEF/PLAN)."""
    number = bare_number(args.ticket)
    state, _path = common.load_state(number)
    if _path is None:
        return result("NO_STATE", 1, ticket=number, message="Run fetch first.")
    folder = ticket_dir(number)
    template = common.SCRIPT_DIR.parent / "skills" / "dev-ticket" / "REVIEW.md"
    first = (folder / "ticket.md").read_text(encoding="utf-8").splitlines()[0] if (folder / "ticket.md").is_file() else ""
    summary = re.sub(r"^#\s*Ticket\s+\d+\s*[—-]\s*", "", first).strip()
    tier = (state.get("review") or {}).get("tier") if args.reviewer else None
    tier = tier or state.get("risk") or (state.get("risk_check") or {}).get("min_tier")
    files = (state.get("gates") or {}).get("files") or []
    missing = [name for name, value in (("summary (ticket.md)", summary), ("tier", tier), ("gates.files", files)) if not value]
    if missing or not template.is_file():
        return result("BRIEF_INPUTS_MISSING", 1, ticket=number, missing=missing, template_found=template.is_file(),
                      message="Fetch the ticket, confirm the tier and run `local_gates.py --ticket N` first.")
    db = state.get("db") or {}
    db_files = db.get("files") or []
    values = {"{N}": number, "{summary}": summary, "{tier}": str(tier), "{files}": ", ".join(files),
              "{D}": str(db.get("ticket") or "none"), "{db_files}": ", ".join(db_files) or "none"}
    text = template.read_text(encoding="utf-8")
    if not db.get("ticket"):
        text = text.replace("`FB_{D}`", "no DB ticket")
    for token, value in values.items():  # only these six: {model} and {n} are for the reviewer to fill in
        text = text.replace(token, value)
    out_name = "REVIEW_BRIEF.md" if args.round == 1 else f"REVIEW_BRIEF-round{args.round}.md"
    result_name = "code-review.md" if args.round == 1 else f"code-review-round{args.round}.md"
    if args.reviewer:
        text += "\n" + REVIEWER_INPUTS_OVERRIDE.replace("{N}", number).replace("{out}", result_name)
    if db_files:
        text += f"\n## DB-checks output (`db_checks.py --ticket {number}`)\n\n{_db_check_lines(number)}\n"
    out = folder / "reviews" / out_name
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    return result("BRIEF_WRITTEN", ticket=number, brief=str(out), review_file=str(folder / "reviews" / result_name),
                  tier=tier, files=len(files), db_files=len(db_files), reviewer=bool(args.reviewer),
                  message=f"Give the sub-agent only this: \"Read {out} and follow it.\" Do not paste its text.")


def cmd_review_check(_api, args) -> int:
    """Mechanical evidence check of the independent code review (backlog A-10).

    Every High / Medium / security finding must cite `file:line` and carry the verbatim code line in the Evidence
    column; this checks that the file exists, the line is in range and the excerpt really appears there. It does not
    judge the reasoning, and it drops nothing: a finding that fails stays in the review, flagged, and counts toward
    the verdict until the developer rules it out."""
    number = bare_number(args.ticket)
    folder = ticket_dir(number)
    path = Path(args.file) if args.file else folder / "reviews" / "code-review.md"
    if not path.is_absolute() and not path.is_file():
        path = folder / path
    if not path.is_file():
        return result("NO_FILE", 1, ticket=number, file=str(path), message="No such review file.")
    import review_check
    db_root = (common.SCRIPT_DIR / CONFIG["db"]["repo_path"]).resolve()
    outcome = review_check.check_review(path, common.PROJECT_ROOT, db_root)
    if not outcome["findings"]:
        return result("NO_FINDINGS_TABLE", 1, ticket=number, file=str(path),
                      message="No findings table (| # | Severity | … | File:line | Evidence | … |) found in the review.")
    bad = outcome["unverified"]
    return result("REVIEW_CHECK_UNVERIFIED" if bad else "REVIEW_CHECK_OK", 2 if bad else 0, ticket=number,
                  file=str(path), findings=outcome["findings"], checked=outcome["checked"], ok=outcome["ok"],
                  unverified=bad, not_checkable=outcome["not_checkable"],
                  message=(f"{len(bad)} finding(s) could not be verified: open each cited line yourself. If the finding "
                           "holds, correct its citation and evidence in the review file; if it does not, show the "
                           "developer and only on their yes move it under '## Dismissed' with the reason. Never drop "
                           "it silently." if bad else "Every required finding's evidence was found at its citation."))
