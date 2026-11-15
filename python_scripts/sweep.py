"""CLCA 'same flaw elsewhere' check for defects: BRIEF.md must record the sweep and a decision per hit.

BRIEF.md section (DEFECT main tickets only):

    ## Same flaw elsewhere
    - path/File.cs:120 - same unguarded cast - decision: fix here
    - other/Service.ts:44 - same pattern, different feature - decision: follow-up - ticket: #18578
    - third/Page.ts:9 - same pattern, not worth a ticket - decision: follow-up - declined: developer says it is dead code

or, when the root cause is not a code pattern that can recur:

    ## Same flaw elsewhere
    not applicable: <reason>

The developer chooses `fix here` or `follow-up` for each hit. A `follow-up` hit must end in the ticket raised for it
(`dt followup-create --hit`, which writes `ticket: #N` onto the bullet) or in the developer's explicit `declined: <reason>`;
the script checks that the sweep is recorded and that no follow-up hit is left without one of the two.
"""
import re

HEADING = re.compile(r"^##\s+Same flaw elsewhere\s*$(.*?)(?=^##\s|\Z)", re.M | re.S | re.I)
DECISION = re.compile(r"decision:\s*(fix here|follow-up)\b", re.I)
PLACEHOLDER = re.compile(r"\b(TODO|TBD|XXX)\b", re.I)
FOLLOWUP = re.compile(r"decision:\s*follow-up\b", re.I)
TICKET = re.compile(r"ticket:\s*#(\d+)", re.I)
DECLINED = re.compile(r"declined:\s*(\S.*)", re.I)
MIN_REASON = 5


def _outcome_ok(line: str) -> bool:
    """A follow-up hit's outcome: the ticket raised for it, or the developer's reason for not raising one."""
    declined = DECLINED.search(line)
    return bool(TICKET.search(line)) or bool(declined and len(declined.group(1).strip()) >= MIN_REASON)


def _hit_lines(body: str) -> list:
    return [ln for ln in body.splitlines() if re.match(r"^\s*[-*]\s+", ln)]


def sweep_counts(brief_text: str):
    """What the recorded sweep says: {"na": True} for `not applicable`, for hits {"fixed": n, "followup": n (all
    follow-up hits), "tickets": [numbers raised], "declined": n}, or None when there is no usable sweep (see sweep_problems)."""
    if sweep_problems(brief_text):
        return None
    body = HEADING.search(brief_text).group(1)
    if re.search(r"not applicable:", body, re.I):
        return {"na": True}
    decisions = [m.group(1).lower() for m in DECISION.finditer(body)]
    follow = [ln for ln in _hit_lines(body) if FOLLOWUP.search(ln)]
    return {"fixed": decisions.count("fix here"), "followup": decisions.count("follow-up"),
            "tickets": [int(m.group(1)) for ln in follow for m in [TICKET.search(ln)] if m],
            "declined": sum(1 for ln in follow if not TICKET.search(ln) and DECLINED.search(ln))}


def sweep_problems(brief_text: str) -> list:
    """Why the sweep is not recorded (empty list = OK)."""
    match = HEADING.search(brief_text or "")
    if not match:
        return ["BRIEF.md has no `## Same flaw elsewhere` section."]
    body = match.group(1).strip()
    if not body or PLACEHOLDER.search(body):
        return ["`## Same flaw elsewhere` is empty or still a placeholder."]
    na = re.search(r"not applicable:\s*(.+)", body, re.I)
    if na:
        return [] if len(na.group(1).strip()) >= 10 else ["`not applicable:` needs a real reason."]
    hits = _hit_lines(body)
    if not hits:
        return ["List each hit as a bullet with `decision: fix here` or `decision: follow-up`, "
                "or write `not applicable: <reason>`."]
    problems = [f"A hit has no decision (`decision: fix here` | `decision: follow-up`): {ln.strip()[:60]}"
                for ln in hits if not DECISION.search(ln)]
    problems += [f"A follow-up hit has no ticket and no decline: raise it (`dt followup-create --hit \"<path:line>\"`) or "
                 f"write `declined: <the developer's reason>` on it: {ln.strip()[:60]}"
                 for ln in hits if FOLLOWUP.search(ln) and not _outcome_ok(ln)]
    return problems


def find_hit(brief_text: str, hit: str) -> tuple:
    """The one follow-up bullet (not yet ticketed or declined) that contains `hit` (e.g. `path/File.cs:120`), case-insensitive.
    -> ("OK", line) | ("HIT_NOT_FOUND", None) | ("HIT_AMBIGUOUS", [lines])."""
    match = HEADING.search(brief_text or "")
    needle = (hit or "").strip().lower()
    lines = [ln for ln in _hit_lines(match.group(1)) if FOLLOWUP.search(ln) and not TICKET.search(ln)
             and not DECLINED.search(ln) and needle in ln.lower()] if match and needle else []
    if len(lines) == 1:
        return "OK", lines[0]
    return ("HIT_AMBIGUOUS", lines) if lines else ("HIT_NOT_FOUND", None)


def link_ticket(brief_text: str, hit: str, ticket) -> tuple:
    """Write ` - ticket: #N` onto the bullet `find_hit` finds. -> (status, new text or None)."""
    status, line = find_hit(brief_text, hit)
    if status != "OK":
        return status, None
    return "OK", brief_text.replace(line, line.rstrip() + f" - ticket: #{ticket}", 1)
