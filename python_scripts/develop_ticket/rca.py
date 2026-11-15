"""RCA / CLCA fields of a defect ticket and the same-flaw sweep line: dt rca."""

import re
from pathlib import Path

from assembla_api import AssemblaApi, bare_number

from develop_ticket import common
from develop_ticket.comments import needs_rca_clca
from develop_ticket.common import CONFIG, inside_ticket_folder, is_db_node, result, ticket_dir


# ---------------------------------------------------------------------- RCA / CLCA (defects)

def rca_clca_fields() -> dict:
    return CONFIG["rca_clca"]["fields"]


def rca_clca_values(ticket: dict) -> dict:
    cf = ticket.get("custom_fields") or {}
    return {key: (cf.get(name) or "").strip() for key, name in rca_clca_fields().items()}


def rca_clca_missing(ticket: dict) -> list:
    """Names of the RCA / CLCA fields that are empty or still a placeholder (`-NA-`, `Pending`, ...)."""
    placeholders = {p.lower() for p in CONFIG["rca_clca"]["placeholders"]}
    return [rca_clca_fields()[key] for key, value in rca_clca_values(ticket).items() if value.lower() in placeholders]


def sweep_check(number: str) -> list:
    """Problems with the CLCA 'same flaw elsewhere' record in BRIEF.md (see sweep.py); empty = OK."""
    import sweep
    brief = ticket_dir(number) / "BRIEF.md"
    return sweep.sweep_problems(brief.read_text(encoding="utf-8") if brief.is_file() else "")


SWEEP_LINE_MARKER = re.compile(r"(?im)^\s*Same flaw elsewhere:")


def sweep_closing_line(number: str, state: dict) -> str:
    """The CLCA's closing line, built from facts (the sweep recorded in BRIEF.md: each follow-up bullet carries its ticket or
    its decline), so the counts and ticket numbers are never typed by hand: '' when no usable sweep is recorded (`dt rca N` reports that as SWEEP_MISSING)."""
    import sweep
    brief = ticket_dir(number) / "BRIEF.md"
    counts = sweep.sweep_counts(brief.read_text(encoding="utf-8") if brief.is_file() else "")
    if counts is None:
        return ""
    if counts.get("na"):
        return "Same flaw elsewhere: not applicable."
    raised = [f"#{n}" for n in counts["tickets"]]
    fixed, follow, declined = counts["fixed"], counts["followup"], counts["declined"]
    parts = [f"{fixed} fixed here"]
    if raised:
        parts.append(f"{len(raised)} follow-up{'s' if len(raised) != 1 else ''} ({', '.join(raised)})")
    if declined:
        parts.append(f"{declined} declined")
    return f"Same flaw elsewhere: {fixed + follow} hit{'s' if fixed + follow != 1 else ''} ({', '.join(parts)})."


def cmd_rca(api: AssemblaApi, args) -> int:
    """Show, or write, the RCA (Root Cause Analysis) and CLCA (Closed-Loop Corrective Action) fields of a main ticket.

    For a defect the CLCA gets a closing 'Same flaw elsewhere: …' line appended (see sweep_closing_line) unless the file
    already has one or `--plain` is given; the composed texts are saved as `rca-final.md` / `clca-final.md` (the source
    files are never changed) and the 1024-character cap is checked on them."""
    number = bare_number(args.ticket)
    t = api.get_ticket(number)
    if is_db_node(t):
        return result("NOT_MAIN_TICKET", 1, ticket=number, message="RCA / CLCA belong to the main ticket, not its DB ticket.")
    state = common.load_state(number)[0] or {}
    names = rca_clca_fields()
    required = needs_rca_clca(t, state)
    new = {}
    for key, file_arg in (("rca", args.rca_file), ("clca", args.clca_file)):
        if not file_arg:
            continue
        if not inside_ticket_folder(number, file_arg):
            return result("FILE_OUTSIDE_TICKET_FOLDER", 1, ticket=number, file=file_arg,
                          message=f"Only files inside {ticket_dir(number)} can be used.")
        text = Path(file_arg).read_text(encoding="utf-8").strip()
        if text.lower() in {p.lower() for p in CONFIG["rca_clca"]["placeholders"]}:
            return result("EMPTY", 1, ticket=number, file=file_arg, message=f"{file_arg} is empty or only a placeholder.")
        line = ""
        if key == "clca" and required and not args.plain and not SWEEP_LINE_MARKER.search(text):
            line = sweep_closing_line(number, state)
            text = f"{text}\n{line}" if line else text
        limit = CONFIG["rca_clca"].get("max_length", 1024)
        if len(text) > limit:
            extra = f" The closing line ({len(line)} characters) is fixed, so trim the prose." if line else ""
            return result("TOO_LONG", 1, ticket=number, file=file_arg, field=names[key], length=len(text), max_length=limit,
                          message=f"{names[key]} is {len(text)} characters; Assembla allows {limit}. "
                                  f"Shorten {file_arg} by {len(text) - limit} and re-run.{extra}")
        (ticket_dir(number) / f"{key}-final.md").write_text(text + "\n", encoding="utf-8")
        new[names[key]] = text
    current = rca_clca_values(t)
    info = dict(ticket=number, required=required, current={names[k]: v for k, v in current.items()})
    if not new:
        missing = rca_clca_missing(t)
        sweep = sweep_check(number) if required else []
        if not missing and sweep:
            return result("SWEEP_MISSING", **info, missing=missing, sweep_problems=sweep)
        return result("MISSING" if missing else "OK", **info, missing=missing, sweep_problems=sweep)
    if not args.yes:
        for name, text in new.items():
            print(f"---- {name} preview (not written) ----\n{text}\n--------------------------------------")
        return result("PREVIEW", **info, will_write=sorted(new), message="Re-run with --yes to write.")
    before = dict(t.get("custom_fields") or {})
    api.update_ticket(number, {"custom_fields": new})
    after = api.get_ticket(number)
    after_cf = after.get("custom_fields") or {}
    wrong = [n for n, v in new.items() if (after_cf.get(n) or "").strip() != v]
    others = [k for k in before if k not in new and (before.get(k) or "") != (after_cf.get(k) or "")]
    if wrong or others:
        return result("FAILED", 1, **info, not_stored=wrong, other_fields_changed=others,
                      message="Re-read does not match: check the ticket in Assembla.")
    return result("UPDATED", ticket=number, written=sorted(new), still_missing=rca_clca_missing(after))
