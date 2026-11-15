"""Comment helpers: the SUMMARY comment checks and the post-or-edit call."""

import re

from assembla_api import AssemblaApi

from develop_ticket.common import CONFIG, is_db_node


def post_or_edit_comment(api: AssemblaApi, number: str, body: str, edit_id=None) -> tuple:
    """Post a new comment, or edit one of mine in place. -> (RESULT status, fields). Never edits someone else's."""
    me_id = api.get_current_user()["id"]
    if edit_id:
        # Editing replaces the text: only my own comment on THIS ticket, never a reviewer's or a teammate's.
        mine = {str(c.get("id")): c for c in api.get_comments(number)}
        target = mine.get(str(edit_id))
        if target is None:
            return "COMMENT_NOT_FOUND", {"comment_id": edit_id, "message": "No comment with that id on this ticket."}
        if str(target.get("user_id")) != str(me_id):
            return "NOT_YOUR_COMMENT", {"comment_id": edit_id,
                                        "message": "That comment was written by someone else: not edited."}
        api.edit_comment(number, edit_id, body)
        return "EDITED", {"comment_id": edit_id, "chars": len(body)}
    comment_id = api.add_comment(number, body)
    if comment_id is None:  # the POST answered without an id: find it, or the SUMMARY could never be edited
        found = [c for c in api.get_comments(number)
                 if str(c.get("user_id")) == str(me_id) and (c.get("comment") or "").strip() == body]
        comment_id = found[-1].get("id") if found else None
    return "POSTED", {"comment_id": comment_id, "chars": len(body),
                      **({} if comment_id is not None else {"warning": "comment id unknown: it cannot be edited in place"})}


# ---------------------------------------------------------------------- hand-off summary (D-75)

SUMMARY_SECTIONS = ("Impact Analysis Report", "Approach Taken")


def summary_problems(text: str, defect: bool = False, functional_skipped: bool = False) -> tuple:
    """-> (errors, warnings) for a SUMMARY.md against skills/dev-ticket/SUMMARY_FORMAT.md. Deterministic, no judgement.

    The summary is written in Assembla's wiki (Textile-style) markup, because that is what the ticket renders: a title line
    `*Ticket #N: ...*`, then two bold heading lines (`*Impact Analysis Report*`, `*Approach Taken*`, once each, in this
    order), each followed by `* ` bullets, one per line. Markdown does not render there (a leading `#` becomes a numbered
    list), so it is an error."""
    cfg = CONFIG["summary"]
    errors, warnings = [], []
    lines = [ln.rstrip() for ln in text.splitlines()]
    body = [ln for ln in lines if ln.strip()]
    if not body or not re.match(r"^\*Ticket #\d+\b.*\*$", body[0]):
        errors.append("The first line must be the bold title: `*Ticket #N: <summary>*`.")
    bold = [m.group(1).strip() for ln in body[1:] for m in [re.match(r"^\*([^*\s][^*]*)\*$", ln)] if m]
    canon = {n.lower(): n for n in SUMMARY_SECTIONS}
    for h in bold:
        if h.lower() not in canon:
            errors.append(f"Heading '{h}' is not in the template (allowed: {', '.join(SUMMARY_SECTIONS)}).")
    found = [canon[h.lower()] for h in bold if h.lower() in canon]
    errors += [f"Missing heading `*{n}*`." for n in SUMMARY_SECTIONS if n not in found]
    if len(found) != len(set(found)):
        errors.append("A heading appears more than once.")
    elif [SUMMARY_SECTIONS.index(n) for n in found] != sorted(SUMMARY_SECTIONS.index(n) for n in found):
        errors.append("Headings are out of order (use the template order).")
    if any(re.match(r"^\s*#{1,6}(\s|$)", ln) for ln in lines):
        errors.append("A line starts with `#`: in Assembla that is a numbered list, not a heading. Use a bold heading line.")
    if any(re.match(r"^\s*-\s", ln) for ln in lines):
        errors.append("Bullets must start with `* `, not `- `.")
    if "**" in text or "`" in text:
        errors.append("Markdown (`**bold**`, backticks) does not render on Assembla: use `*bold*`, and plain names.")
    if re.search(r"[<>]", text):
        errors.append("`<` and `>` are read as HTML on Assembla and vanish: write the name out (e.g. 'the slug').")
    heading_at = next((i for i, ln in enumerate(body[1:], 1) if re.match(r"^\*([^*\s][^*]*)\*$", ln)), len(body))
    for ln in body[heading_at:]:  # lines between the title and the first heading are the Risk / Branches line
        if not (re.match(r"^\*([^*\s][^*]*)\*$", ln) or ln.startswith("* ")):
            errors.append(f"A loose line: every point must be its own `* ` bullet (no sub-bullets): {ln[:50]}")
            break
    if re.search(r"[A-Za-z]:\\", text):
        errors.append("Local paths (e.g. D:\\...) do not belong in a ticket comment.")
    if re.search(r"\b(TODO|TBD|XXX|lorem)\b", text):
        errors.append("Placeholder text (TODO / TBD) left in the summary.")
    if functional_skipped and not re.search(r"(?i)functional checks:\s*skipped", text):
        errors.append("The developer skipped the functional checks: say it in Approach Taken, e.g. "
                      "'Functional checks: skipped by the developer (reason)'.")
    if defect and not re.search(r"(?i)root cause", text):
        warnings.append("A defect: say the root cause (RCA) in Approach Taken.")
    used = len(body)
    if used > cfg["max_lines_error"]:
        errors.append(f"{used} non-blank lines; the limit is {cfg['max_lines_error']}. Cut process history and file lists.")
    elif used > cfg["max_lines_warn"]:
        warnings.append(f"{used} non-blank lines; aim for at most {cfg['max_lines_warn']}.")
    return errors, warnings


def needs_rca_clca(ticket: dict, state: dict) -> bool:
    """A main ticket whose Category is a defect value, or whose verdict recorded by `fetch` is DEFECT. Never a DB ticket."""
    if is_db_node(ticket):
        return False
    rules = CONFIG["ticket_type_rules"]
    category = ((ticket.get("custom_fields") or {}).get(rules["custom_field"]) or "").strip().lower()
    return category in {v.lower() for v in rules["defect_values"]} or (state or {}).get("type") == "DEFECT"
