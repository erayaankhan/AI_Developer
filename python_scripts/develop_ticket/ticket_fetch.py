"""`dt fetch`: the ticket snapshot written to DEV_TICKETS/N/."""

import json
import re
from pathlib import Path

from assembla_api import PLAN_LEVELS, AssemblaApi, bare_number
from state_io import write_state

from develop_ticket.common import CONFIG, STATUS, branch_name, now_iso, result, ticket_dir
from develop_ticket import common


def work_dir_for(number: str, ticket_type: str = None):
    """(type, folder). The type comes from --type/inference, else from an existing state.json."""
    folder = ticket_dir(number)
    if not ticket_type and (folder / "state.json").is_file():
        ticket_type = json.loads((folder / "state.json").read_text(encoding="utf-8")).get("type")
    return (ticket_type, folder) if ticket_type else (None, None)


def infer_type(ticket: dict, tags: list):
    rules = CONFIG["ticket_type_rules"]
    field = rules.get("custom_field")
    if field:
        value = ((ticket.get("custom_fields") or {}).get(field) or "").strip().lower()
        if value in (v.lower() for v in rules["defect_values"]):
            return "DEFECT"
        if value in (v.lower() for v in rules["cr_values"]):
            return "CR"
        # Category present but not a defect value (Task, blank, ...) -> default type
        # (team decision 2026-09-29: treat as CR).
        if field in (ticket.get("custom_fields") or {}) and rules.get("default_type"):
            return rules["default_type"]
    lowered = {t.lower() for t in tags}
    if lowered & {t.lower() for t in rules["defect_tags"]}:
        return "DEFECT"
    if lowered & {t.lower() for t in rules["cr_tags"]}:
        return "CR"
    return rules.get("default_type")


READINESS_PATTERNS = {
    "acceptance_criteria": r"acceptance\s+criteria|\bAC\s*[:#\d]",
    "expected_behaviour": r"expected\s+(behaviou?r|result|outcome)|\bexpected\b\s*:",
    "current_behaviour": r"(current|actual)\s+(behaviou?r|result|outcome)|\bactual\b\s*:",
    "steps_to_reproduce": r"steps\s+to\s+reproduce|\bsteps\b\s*:|\brepro\b",
}


IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")


VIDEO_EXTS = (".mp4", ".mov", ".webm", ".avi", ".mkv")


MAX_IMAGE_BYTES = 10 * 1024 * 1024


def attachment_kind(attachment: dict) -> str:
    name = (attachment.get("name") or attachment.get("filename") or "").lower()
    return "image" if name.endswith(IMAGE_EXTS) else "video" if name.endswith(VIDEO_EXTS) else "other"


def download_images(api, attachments: list, folder: Path) -> None:
    """Save image attachments to <folder>/attachments/ so Claude can view them; sets 'local_path'.
    Videos and other files are only listed (Claude cannot watch a video). Failures never stop the fetch."""
    target = folder / "attachments"
    for a in attachments:
        if attachment_kind(a) != "image" or (a.get("filesize") or 0) > MAX_IMAGE_BYTES:
            continue
        name = re.sub(r"[^\w.\- ]", "_", a.get("name") or a.get("filename") or f"{a.get('id')}.png")
        try:
            target.mkdir(parents=True, exist_ok=True)
            (target / name).write_bytes(api.download_attachment(a))
            a["local_path"] = str(target / name)
        except Exception as exc:  # network, auth or disk: the link in ticket.md still works
            a["download_error"] = str(exc)[:120]


def readiness_hints(ticket: dict, comments: list, attachments: list, links: list, tags: list) -> dict:
    """Mechanical signals only - Claude makes the actual READY / NEEDS_INFO call."""
    text = "\n".join([ticket.get("description") or ""] + [c.get("comment") or "" for c in comments])
    hints = {name: bool(re.search(p, text, re.I)) for name, p in READINESS_PATTERNS.items()}
    hints["description_chars"] = len((ticket.get("description") or "").strip())
    hints["comment_count"] = len(comments)
    hints["attachment_count"] = len(attachments)
    hints["image_attachments"] = sum(1 for a in attachments if attachment_kind(a) == "image")
    hints["video_attachments"] = sum(1 for a in attachments if attachment_kind(a) == "video")
    hints["urls_in_text"] = sorted(set(u.rstrip(".,;)") for u in re.findall(r"https?://[^\s<>\]\"']+", text)))
    # Structured fields that answer "which area?" / "where does it happen?" (c2m-ci space).
    cf_all = ticket.get("custom_fields") or {}
    for key in ("C2M-Module", "C2M-Sub-Module", "Environment", "Severity"):
        value = (cf_all.get(key) or "").strip()
        hints[key] = value if value not in ("", "NA", "-NA-") else None

    hints["deploy_attribute_warnings"] = deploy_attribute_problems(ticket, tags, links)
    return hints


def deploy_attribute_problems(ticket: dict, tags: list, links: list) -> list:
    """Attributes the staging merge/deploy job filters on. Empty list = OK.

    Decision D-12 (2026-09-29): these must all be correct *before* the FB_ branch is
    created - `branch` refuses to run otherwise. The deploy job only ever picks
    Plan level = Story tickets, so a ticket missing them would never reach staging.
    """
    attrs = CONFIG["deploy_attributes"]
    cf = ticket.get("custom_fields") or {}
    problems = []
    if ticket.get("hierarchy_type") != attrs["plan_level"]:
        problems.append(f"Plan level is {PLAN_LEVELS.get(ticket.get('hierarchy_type'), ticket.get('hierarchy_type'))}, "
                        f"expected {PLAN_LEVELS.get(attrs['plan_level'])}")
    if (cf.get(attrs["node_field"]) or "").strip() != attrs["node_value"]:
        problems.append(f"{attrs['node_field']} is {cf.get(attrs['node_field'])!r}, expected {attrs['node_value']!r}")
    if attrs["tag"] and attrs["tag"] not in tags:  # a team without a deploy tag sets tag ""
        problems.append(f"Tag {attrs['tag']!r} missing")
    if attrs["needs_epic_parent"]:
        inactive = set(CONFIG["epics"]["inactive_statuses"])
        epics = [link for link in links if link.get("plan_level") == "Epic"]
        if not epics:
            problems.append("No Epic parent")
        elif not any(link.get("status") not in inactive for link in epics):
            # D-14: a closed/invalid Epic counts as no Epic.
            problems.append("Epic parent is inactive: " + ", ".join(
                f"#{link['ticket']} ({link['status']})" for link in epics))
    return problems


def resolve_links(api: AssemblaApi, ticket: dict) -> list:
    relationship = {0: "parent", 1: "child", 2: "related", 3: "duplicate", 4: "sibling",
                    5: "story", 6: "subtask", 7: "dependent", 8: "block"}
    links = []
    for assoc in api.get_associations(ticket["number"]):
        other_id = assoc.get("ticket2_id") if assoc.get("ticket1_id") == ticket["id"] else assoc.get("ticket1_id")
        other = api.get_ticket_by_id(other_id) if other_id else {}
        if not other:
            continue
        links.append({
            "association_id": assoc.get("id"),
            "ticket_id": other.get("id"),
            "relationship": relationship.get(assoc.get("relationship"), str(assoc.get("relationship"))),
            "ticket": other.get("number"),
            "summary": other.get("summary"),
            "status": other.get("status"),
            "plan_level": PLAN_LEVELS.get(other.get("hierarchy_type"), ""),
        })
    return links


def render_ticket_md(api, t, tags, comments, attachments, links, milestone, hints, ticket_type) -> str:
    cf = t.get("custom_fields") or {}
    lines = [
        f"# Ticket {t.get('number')} — {t.get('summary')}",
        "",
        f"_Snapshot taken {now_iso()} by dev_ticket.py — re-run `fetch` to refresh._",
        "",
        "| Field | Value |", "|---|---|",
        f"| Type (inferred) | {ticket_type or 'UNKNOWN — decide CR vs DEFECT'} |",
        f"| Status | {t.get('status')} |",
        f"| Assignee | {api.user_label(t.get('assigned_to_id'))} |",
        f"| Reporter | {api.user_label(t.get('reporter_id'))} |",
        f"| Priority | {t.get('priority')} |",
        f"| Plan level | {PLAN_LEVELS.get(t.get('hierarchy_type'), t.get('hierarchy_type'))} |",
        f"| Milestone | {milestone.get('title') or t.get('milestone_id')} |",
        f"| Tags | {', '.join(tags) or '-'} |",
    ]
    for key, value in sorted(cf.items()):
        if value not in (None, ""):
            lines.append(f"| {key} | {value} |")
    lines += ["", "## Description", "", (t.get("description") or "_(empty)_").strip(), ""]

    lines += ["## Linked tickets", ""]
    lines += [f"- {l['relationship']}: #{l['ticket']} [{l['plan_level'] or '-'}] {l['summary']} ({l['status']})"
              for l in links] or ["_(none)_"]

    lines += ["", "## Attachments", ""]
    lines += [f"- {a.get('name') or a.get('filename')} [{attachment_kind(a)}] — {a.get('url') or ''}"
              + (f" — saved: `{a['local_path']}` (view it)" if a.get("local_path") else "")
              + (" — video, cannot be viewed: ask for a description if it is the only repro"
                 if attachment_kind(a) == "video" else "")
              for a in attachments] or ["_(none)_"]

    # Long-running tickets can carry 100+ comments; keep the snapshot readable and cheap
    # to load. The full set stays in ticket.json.
    limit = CONFIG.get("max_comments_in_snapshot") or len(comments)
    shown = comments[-limit:]
    lines += ["", f"## Comments (oldest first, latest {len(shown)} of {len(comments)})", ""]
    for c in shown:
        lines += [f"### {api.user_label(c.get('user_id'))} — {c.get('created_on')}", "", c["comment"].strip(), ""]
    if not comments:
        lines.append("_(none)_")

    lines += ["", "## Readiness hints (mechanical — not a verdict)", "", "```json",
              json.dumps(hints, indent=2), "```", ""]
    return "\n".join(lines)


def _is_my_review(api: AssemblaApi, t: dict) -> bool:
    """A ticket in Code Review assigned to me while my role is reviewer: a review (/review-and-deploy), not author work."""
    return (t.get("status") == STATUS["code_review"] and common._remembered_role() == "reviewer"
            and t.get("assigned_to_id") == api.get_current_user().get("id"))


def cmd_takeover(_api, args) -> int:
    """Fix a ticket yourself that has a reviewer's review folder (`kind=review`, e.g. its author was another developer):
    move DEV_TICKETS/N aside as N-review-rK (kept, never deleted) so a fresh `dt fetch N` starts the author work.
    Previews unless --yes."""
    number = bare_number(args.ticket)
    state, state_path = common.load_state(number)
    if state_path is None:
        return result("NO_STATE", 1, ticket=number, message="No work folder for this ticket: run dt fetch.")
    if (state or {}).get("kind") != "review":
        return result("NOT_A_REVIEW_FOLDER", 1, ticket=number,
                      message="This folder is not a reviewer's review: there is nothing to take over.")
    folder = ticket_dir(number)
    k = 1
    while (folder.parent / f"{number}-review-r{k}").exists():
        k += 1
    aside = folder.parent / f"{number}-review-r{k}"
    stage = (state.get("review") or {}).get("stage")
    info = {"ticket": number, "move_from": str(folder), "move_to": str(aside), "review_stage": stage}
    unfinished = " The review is not finished: taking over abandons it (the folder is kept)." if stage not in (None, "done") else ""
    if not args.yes:
        return result("PREVIEW", **info, message=f"Moves the review folder aside; then `dt fetch {number}` starts the author work "
                                                 f"(re-run with --yes).{unfinished}")
    try:
        folder.rename(aside)
    except OSError as exc:
        return result("FAILED", 1, **info, message=f"Could not move the folder (close anything using it): {exc}")
    return result("TAKEN_OVER", **info, message=f"Review folder kept at {aside}. Run `dt fetch {number}` for a fresh work folder.")


def cmd_fetch(api: AssemblaApi, args) -> int:
    number = bare_number(args.ticket)
    if not getattr(args, "from_review", False):
        existing = (common.load_state(number)[0] or {})
        if existing.get("kind") == "review":
            return result("REVIEW_FOLDER_EXISTS", 1, ticket=number, review_stage=(existing.get("review") or {}).get("stage"),
                          message=f"DEV_TICKETS\\{number} is a reviewer's review of this ticket (kind=review). To review it use "
                                  f"/review-and-deploy {number}. To fix the ticket yourself: `dt takeover {number}` (preview), "
                                  f"then `dt takeover {number} --yes` and `dt fetch {number}`.")
    t = api.get_ticket(number)
    if not getattr(args, "from_review", False) and _is_my_review(api, t):
        return result("USE_REVIEW_AND_DEPLOY", 1, ticket=number,
                      message="This ticket is in Code Review, assigned to you as a reviewer: use /review-and-deploy "
                              f"{number} (dt review-pick / dt review-fetch), not /dev-ticket.")
    tags = api.get_tags(number)
    comments = api.get_comments(number)
    attachments = api.get_attachments(number)
    links = resolve_links(api, t)
    milestone = api.get_milestone(t.get("milestone_id"))
    ticket_type = args.type or infer_type(t, tags)
    hints = readiness_hints(t, comments, attachments, links, tags)
    kind, folder = work_dir_for(number, ticket_type)
    if folder is not None:
        download_images(api, attachments, folder)
    md = render_ticket_md(api, t, tags, comments, attachments, links, milestone, hints, ticket_type)

    if folder is None:
        print(md)
        return result("TYPE_UNKNOWN", ticket=number, hints=hints,
                      message="Could not infer CR vs DEFECT - re-run with --type CR|DEFECT to save.")

    folder.mkdir(parents=True, exist_ok=True)
    (folder / "ticket.md").write_text(md, encoding="utf-8")
    (folder / "ticket.json").write_text(json.dumps(
        {"ticket": t, "tags": tags, "comments": comments, "attachments": attachments, "links": links},
        indent=2, ensure_ascii=False), encoding="utf-8")
    state_path = folder / "state.json"
    if not state_path.exists():
        write_state(state_path, {
            "ticket": number, "type": kind, "branch": branch_name(number), "role": common._remembered_role(),
            "stage": "2-check", "risk": None, "repair_attempts": {},
            "history": [{"stage": "1-pick", "result": "PICKED", "at": now_iso()}],
        })
    print(f"Saved {folder / 'ticket.md'}")
    return result("SAVED", ticket=number, type=kind, folder=str(folder), hints=hints)
