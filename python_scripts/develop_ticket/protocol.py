"""The HANDOFF (author -> reviewer) and VERDICT (reviewer -> author) comments: payloads, render, parse."""

import json
import re

from assembla_api import AssemblaApi

from develop_ticket.common import branch_name, ticket_dir


# ---------------------------------------------------------------------- developer hand-off to a reviewer (HANDOFF comment)

HANDOFF_BEGIN, HANDOFF_END = "HANDOFF-JSON-BEGIN", "HANDOFF-JSON-END"


HANDOFF_MAX_FILES = 100


def _brief_section(number: str, heading: str) -> str:
    """First non-blank line under `## <heading>` in BRIEF.md, or ''."""
    brief = ticket_dir(number) / "BRIEF.md"
    if not brief.is_file():
        return ""
    match = re.search(rf"^##\s+{re.escape(heading)}\s*$(.*?)(?=^##\s|\Z)", brief.read_text(encoding="utf-8"),
                      re.M | re.S | re.I)
    lines = [ln.strip() for ln in (match.group(1) if match else "").splitlines() if ln.strip()]
    return lines[0][:200] if lines else ""


def handoff_payload(state: dict, ticket: str, author: str, head: str, db_head, readme_impact: str) -> dict:
    """The facts the reviewer gets (HANDOFF comment), copied from state.json, never from memory. Evidence, not authority:
    the reviewer re-runs gates and the risk check on the pushed head."""
    gates, risk_check = state.get("gates") or {}, state.get("risk_check") or {}
    func, skip = state.get("functional") or {}, state.get("functional_skip") or {}
    db = state.get("db") or {}
    base = state.get("baseline") or {}
    mode = state.get("functional_mode")
    return {
        "ticket": ticket, "author": author, "branch": branch_name(ticket), "head": head,
        "round": state.get("round") or 1,
        "risk": state.get("risk"), "path": state.get("path"),
        "risk_check": {"min_tier": risk_check.get("min_tier"), "reasons": risk_check.get("reasons") or []},
        "gates": {"result": gates.get("status"), "at": gates.get("at"),
                  "files_count": len(gates.get("files") or []), "files": (gates.get("files") or [])[:HANDOFF_MAX_FILES]},
        "functional": {"mode": mode, "profile": func.get("profile"), "passed": func.get("passed"),
                       "failed": func.get("failed"), "skip_reason": skip.get("reason") if mode == "skip" else None},
        "db": {"ticket": db["ticket"], "branch": branch_name(db["ticket"]), "head": db_head,
               "files": db.get("files") or []} if db.get("files") else None,
        "baseline": "accepted" if base.get("accepted") else (base.get("status") or "n/a"),
        "readme_impact": readme_impact or "none",
        "quality": _quality_summary(state.get("quality")),
    }


def _quality_summary(rec):
    """The hand-off's view of `dt quality`: per tool, what blocked was fixed; what is left (Low Sonar, unfixable Snyk)."""
    if not rec:
        return None
    return {"result": rec.get("status"), "at": rec.get("at"), "apps": rec.get("apps") or [],
            "skipped": rec.get("skipped") or {},
            "sonar_low": sum(int(r.get("low_count") or len(r.get("low") or [])) for r in rec.get("sonar") or []),
            "snyk_unfixable": sum(int(r.get("unfixable_count") or len(r.get("unfixable") or []))
                                  for r in rec.get("snyk") or [])}


def _quality_line(q: dict) -> str:
    if q["result"] == "NO_APPS":
        return "* Sonar / Snyk: no app with a Sonar or Snyk check changed"
    parts = [f"* Sonar / Snyk: {q['result']} at {q['at']} ({', '.join(q['apps'])})"]
    if q.get("sonar_low"):
        parts.append(f"{q['sonar_low']} Low Sonar issue(s) left on changed lines (the Jenkins gate counts them)")
    if q.get("snyk_unfixable"):
        parts.append(f"{q['snyk_unfixable']} Snyk vulnerability(ies) with no fix available")
    for tool, reason in (q.get("skipped") or {}).items():
        parts.append(f"{tool} skipped: {reason}")
    return "; ".join(parts)


def render_handoff(p: dict) -> str:
    """The HANDOFF comment body: Assembla wiki markup for people, then one JSON block between markers for the
    reviewer's tools. `<` and `>` are written as \\u003c / \\u003e (still valid JSON) because Assembla reads them as HTML."""
    func = p["functional"]
    if func["mode"] == "skip":
        func_line = f"skipped by the developer ({func['skip_reason']})"
    else:
        func_line = f"run, {func['passed']} passed, {func['failed']} failed (profile {func['profile']})"
    db = p["db"]
    lines = [f"*Hand-off #{p['ticket']} (round {p['round']}): ready for reviewer review*",
             f"* Branch {p['branch']} at head {p['head'][:12]}, pushed",
             f"* Risk: {p['risk']} (path {p['path']}); risk check minimum tier {p['risk_check']['min_tier']}",
             f"* Gates: {p['gates']['result']} at {p['gates']['at']}, {p['gates']['files_count']} files",
             f"* Functional checks: {func_line}",
             f"* Database: ticket {db['ticket']}, branch {db['branch']} at head {str(db['head'])[:12]}, "
             f"{len(db['files'])} files" if db else "* Database: no change",
             f"* Baseline: {p['baseline']}",
             f"* README impact: {p['readme_impact']}",
             *([_quality_line(p["quality"])] if p.get("quality") else []),
             "", HANDOFF_BEGIN,
             json.dumps(p, indent=2, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e"),
             HANDOFF_END]
    return "\n".join(lines) + "\n"


def parse_handoff(text: str):
    """The payload dict of a HANDOFF comment, or None when there is no (valid) JSON block."""
    match = re.search(rf"{HANDOFF_BEGIN}\s*(.*?)\s*{HANDOFF_END}", text or "", re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def latest_handoff(api: AssemblaApi, number: str):
    """The newest valid HANDOFF comment of a ticket, or None. -> {payload, comment_id, writer_login, at, authentic,
    ambiguous}. `authentic`: the payload's `author` is the Assembla login of the comment's real writer (a claim that
    names someone else is not trusted). `ambiguous`: valid HANDOFF comments of the current round were written by more
    than one person, so the caller must ask which is the developer's instead of picking the newest. A round ends at a
    REJECT verdict or a QUALITY-BOUNCE: hand-offs last written before the latest of those are superseded (kept as the
    record, e.g. when another developer took the ticket over) and do not count. Hand-offs and verdicts are edited in
    place for later rounds, so "last written" is the comment's `updated_at` (else `created_on`), not its position."""
    def written(c):
        return str(c.get("updated_at") or c.get("created_on") or "")

    found, closed = [], ""
    for comment in api.get_comments(number):  # oldest first (by creation)
        text = comment.get("comment")
        verdict = parse_verdict(text)
        if parse_bounce(text) is not None or (verdict is not None and verdict.get("verdict") == "REJECT"):
            closed = max(closed, written(comment))
            continue
        data = parse_handoff(text)
        if data is not None:
            found.append((comment, data))
    found = sorted([(c, d) for c, d in found if written(c) > closed], key=lambda cd: written(cd[0]))  # stable
    if not found:
        return None
    comment, data = found[-1]
    writers = {str(c.get("user_id")) for c, _ in found}
    login = api.get_user(comment.get("user_id")).get("login") or ""
    return {"payload": data, "comment_id": comment.get("id"), "writer_login": login, "at": comment.get("created_on"),
            "authentic": bool(login) and str(data.get("author", "")).lower() == login.lower(),
            "ambiguous": len(writers) > 1}


# ---------------------------------------------------------------------- quality bounce (QUALITY-BOUNCE comment, parse)

BOUNCE_BEGIN, BOUNCE_END = "QUALITY-BOUNCE-JSON-BEGIN", "QUALITY-BOUNCE-JSON-END"


def parse_bounce(text: str):
    match = re.search(rf"{BOUNCE_BEGIN}\s*(.*?)\s*{BOUNCE_END}", text or "", re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


# ---------------------------------------------------------------------- reviewer review: the verdict comment (format)

VERDICT_BEGIN, VERDICT_END = "VERDICT-JSON-BEGIN", "VERDICT-JSON-END"


REVIEW_SEVERITIES = ("High", "Medium", "Low")


VERDICT_TEXT_MAX = 600


VERDICT_MAX_FINDINGS = 30


def _verdict_text_errors(name: str, value, required: bool = True) -> list:
    """Why a verdict text field cannot be posted to Assembla (wiki markup, one line, no local paths or placeholders)."""
    if not isinstance(value, str) or not value.strip():
        return [f"`{name}` is required."] if required else []
    errors = []
    if "\n" in value or "\r" in value:
        errors.append(f"`{name}` must be one line (each point is its own bullet).")
    if len(value) > VERDICT_TEXT_MAX:
        errors.append(f"`{name}` is {len(value)} characters; the limit is {VERDICT_TEXT_MAX}.")
    if "<" in value or ">" in value:
        errors.append(f"`{name}`: `<` and `>` are read as HTML on Assembla and vanish: write the words out.")
    if "`" in value or "**" in value:
        errors.append(f"`{name}`: Markdown (backticks, **bold**) does not render on Assembla: use plain text.")
    if re.search(r"[A-Za-z]:\\", value):
        errors.append(f"`{name}`: local paths do not belong in a ticket comment.")
    if re.search(r"\b(TODO|TBD|XXX|lorem)\b", value):
        errors.append(f"`{name}`: placeholder text left in.")
    return errors


def verdict_problems(v, ai_open: bool = False) -> list:
    """Errors in a verdict.json (empty = fine). `ai_open`: the AI review still has an open High or Medium finding, which an
    approval may only override with a recorded reason."""
    if not isinstance(v, dict):
        return ["verdict.json must be a JSON object."]
    errors = []
    kind = v.get("verdict")
    if kind not in ("APPROVE", "REJECT"):
        errors.append("`verdict` must be APPROVE or REJECT.")
    errors += _verdict_text_errors("summary", v.get("summary"))
    findings = v.get("findings") or []
    if not isinstance(findings, list):
        return errors + ["`findings` must be a list."]
    if len(findings) > VERDICT_MAX_FINDINGS:
        errors.append(f"`findings` has {len(findings)} entries; the limit is {VERDICT_MAX_FINDINGS}.")
    if kind == "REJECT" and not findings:
        errors.append("A reject needs at least one finding.")
    for i, f in enumerate(findings, 1):
        if not isinstance(f, dict):
            errors.append(f"`findings[{i}]` must be an object.")
            continue
        if f.get("severity") not in REVIEW_SEVERITIES:
            errors.append(f"`findings[{i}].severity` must be one of {', '.join(REVIEW_SEVERITIES)}.")
        elif kind == "APPROVE" and f["severity"] in ("High", "Medium"):
            errors.append(f"`findings[{i}]`: an approval cannot carry High or Medium findings: reject, or drop the finding.")
        location = f.get("file_line")
        if not isinstance(location, str) or not re.fullmatch(r"general|[^\s:<>`]+:\d+(-\d+)?", location):
            errors.append(f"`findings[{i}].file_line` must be `path:line` (or `path:line-line`), or `general`.")
        errors += _verdict_text_errors(f"findings[{i}].finding", f.get("finding"))
        errors += _verdict_text_errors(f"findings[{i}].fix", f.get("fix"), required=kind == "REJECT")
    for key in ("fix_list", "notes"):
        items = v.get(key) or []
        if not isinstance(items, list):
            errors.append(f"`{key}` must be a list of strings.")
            continue
        for i, item in enumerate(items, 1):
            errors += _verdict_text_errors(f"{key}[{i}]", item)
    if v.get("override_reason") is not None:
        errors += _verdict_text_errors("override_reason", v.get("override_reason"))
    if kind == "APPROVE" and ai_open and not str(v.get("override_reason") or "").strip():
        errors.append("The AI review has an open High or Medium finding: approving needs an `override_reason`.")
    return errors


def verdict_payload(v: dict, *, ticket: str, reviewer: str, author: str, head: str, round: int, tier: str,
                    checks: dict) -> dict:
    """The facts of a verdict comment: the reviewer's validated verdict.json plus what the scripts know (head, round, tier,
    the checks that were reproduced). Findings are numbered; a reject without an explicit fix list gets one from the findings."""
    findings = [{"n": i, "severity": f["severity"], "file_line": f["file_line"], "finding": f["finding"].strip(),
                 "fix": (f.get("fix") or "").strip()} for i, f in enumerate(v.get("findings") or [], 1)]
    fix_list = list(v.get("fix_list") or [])
    if v["verdict"] == "REJECT" and not fix_list:
        fix_list = [f["fix"] for f in findings if f["fix"]]
    return {"ticket": ticket, "verdict": v["verdict"], "reviewer": reviewer, "author": author, "head": head,
            "round": round, "tier": tier, "summary": v["summary"].strip(), "findings": findings,
            "fix_list": fix_list if v["verdict"] == "REJECT" else [], "notes": list(v.get("notes") or []),
            "override_reason": (v.get("override_reason") or "").strip() or None, "checks": checks}


def render_verdict(p: dict) -> str:
    """The REVIEW_VERDICT comment: Assembla wiki markup for people, then one JSON block between markers that the developer's
    tools read (`<` and `>` escaped as \\u003c / \\u003e, still valid JSON)."""
    ai = (p.get("checks") or {}).get("ai_review") or {}
    c = p.get("checks") or {}
    title = "APPROVED" if p["verdict"] == "APPROVE" else "CHANGES REQUIRED"
    lines = [f"*Review of #{p['ticket']} (round {p['round']}): {title}*",
             f"* Reviewer: {p['reviewer']}, reviewed head {p['head'][:12]}, tier {p['tier']}",
             f"* Summary: {p['summary']}",
             f"* Checks: gates {c.get('gates')}, risk {c.get('risk')}, functional {c.get('functional')}, "
             f"compare {c.get('compare')}; AI review {ai.get('verdict')} ({ai.get('high')} High, "
             f"{ai.get('medium')} Medium, {ai.get('low')} Low)"]
    if p.get("override_reason"):
        lines.append(f"* Override: {p['override_reason']}")
    if p["findings"]:
        lines += ["", "*Findings*"]
        lines += [f"* {f['n']} ({f['severity']}) {f['file_line']} - {f['finding']}" + (f" Fix: {f['fix']}" if f["fix"] else "")
                  for f in p["findings"]]
    if p["fix_list"]:
        lines += ["", "*Fix before resubmitting*"] + [f"* {item}" for item in p["fix_list"]]
    if p["notes"]:
        lines += ["", "*Notes*"] + [f"* {item}" for item in p["notes"]]
    lines += ["", VERDICT_BEGIN, json.dumps(p, indent=2, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e"),
              VERDICT_END]
    return "\n".join(lines) + "\n"


def parse_verdict(text: str):
    """The payload dict of a REVIEW_VERDICT comment, or None when there is no (valid) JSON block."""
    match = re.search(rf"{VERDICT_BEGIN}\s*(.*?)\s*{VERDICT_END}", text or "", re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


# ---------------------------------------------------------------------- developer: rework after a rejection

def latest_verdict(api: AssemblaApi, number: str):
    """The newest valid REVIEW_VERDICT comment of a ticket, or None. -> {payload, text, comment_id, writer_login, at}.
    `writer_login` is the Assembla login of the person who really wrote it (a claimed reviewer is not trusted)."""
    found = [(c, parse_verdict(c.get("comment"))) for c in api.get_comments(number)]
    found = [(c, data) for c, data in found if data is not None]
    if not found:
        return None
    comment, data = found[-1]
    return {"payload": data, "text": comment.get("comment") or "", "comment_id": comment.get("id"),
            "writer_login": api.get_user(comment.get("user_id")).get("login") or "", "at": comment.get("created_on")}
