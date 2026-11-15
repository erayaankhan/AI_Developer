"""Pause and resume: dt handover (push the work folder to the branch) and dt restore."""

import re
from datetime import datetime
from pathlib import Path

from assembla_api import AssemblaApi, bare_number
from state_io import write_state

from develop_ticket import common
from develop_ticket.common import STATUS, inside_ticket_folder, now_iso, record_metric, result, ticket_dir


# ---------------------------------------------------------------------- pause / resume (D-32..D-40)

EMBEDDED = ("BRIEF.md", "PLAN.md", "state.json")


def cmd_handover(api: AssemblaApi, args) -> int:
    """Pause: build HANDOVER.md, then (with --yes) attach it, comment, Dev In Process -> Dev Queue."""
    number = bare_number(args.ticket)
    folder = ticket_dir(number)
    state, state_path = common.load_state(number)
    if state_path is None:
        return result("NO_STATE", 1, ticket=number, message=f"No {folder / 'state.json'}.")
    if not inside_ticket_folder(number, args.notes):
        return result("FILE_OUTSIDE_TICKET_FOLDER", 1, ticket=number, file=args.notes,
                      message=f"The notes file must be inside {folder}.")
    notes_path = Path(args.notes)
    if not notes_path.is_file():
        return result("NO_NOTES", 1, ticket=number, message=f"Notes file {notes_path} not found.")

    # The code must be safe on origin before anything is handed over (D-32, D-36, D-40).
    wf_push = common.push_state(number)
    db = state.get("db") or {}
    db_push = common.push_state(str(db["ticket"]), common.DB_REPO) if db.get("files") else None
    not_pushed = [p for p in (wf_push, db_push) if p and p["status"] != "PUSHED"]
    if not_pushed:
        return result("NOT_PUSHED", ticket=number, repos=[wf_push] + ([db_push] if db_push else []),
                      message="Commit and push the WIP first (WF branch, and DB branch if any).")

    ticket = api.get_ticket(number)
    me = api.get_current_user().get("login")
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    stage = state.get("stage")
    header = [
        f"# Hand-over — #{number} {ticket.get('summary')}",
        f"Paused: {now_iso()} by {me} · Stage: {stage} · Risk: {state.get('risk')} ({state.get('path')}) · "
        f"Branch: FB_{number} @ {wf_push['head']} (pushed)",
        "DB: " + (f"ticket #{db['ticket']}, branch FB_{db['ticket']} @ {db_push['head']}, files: {', '.join(db['files'])}"
                  if db_push else "none"),
        "",
        notes_path.read_text(encoding="utf-8").strip(),
        "",
        "## How to resume",
        f"`/dev-ticket {number}` as the assigned developer. Everything below is restored by "
        f"`dev_ticket.py restore {number}`.",
        "",
    ]
    for name in EMBEDDED:
        path = folder / name
        header += [f"<!-- BEGIN {name} -->", path.read_text(encoding="utf-8").rstrip() if path.is_file() else "",
                   f"<!-- END {name} -->", ""]
    local_copy = folder / "HANDOVER.md"
    local_copy.write_text("\n".join(header), encoding="utf-8")
    upload_name = folder / f"HANDOVER-{number}-{stamp}.md"
    upload_name.write_text("\n".join(header), encoding="utf-8")

    moves_status = ticket.get("status") == STATUS["in_process"]
    planned = [f"attach {upload_name.name}", "post a one-line '[dev-ticket pause]' comment"]
    planned += [f"status {STATUS['in_process']} -> {STATUS['queue']}"] if moves_status else []
    if not args.yes:
        return result("PREVIEW", ticket=number, handover=str(local_copy), planned=planned,
                      message="Re-run with --yes to post.")

    api.upload_attachment(ticket["id"], upload_name)
    api.add_comment(number, f"[dev-ticket pause] Paused at stage {stage} by {me}. Hand-over: "
                            f"{upload_name.name} (attached). Resume with /dev-ticket {number}.")
    companion = None
    if moves_status:
        api.update_status(number, STATUS["queue"])
        companion = common.move_db_companion(api, state, STATUS["in_process"], STATUS["queue"])
    state.update(paused_from=stage, stage="paused", paused_at=now_iso(), paused_by=me)
    state.setdefault("history", []).append({"stage": "paused", "result": "HANDOVER_POSTED", "at": now_iso()})
    write_state(state_path, state)
    record_metric(number, "pause", paused_from=stage)
    return result("PAUSED", ticket=number, attached=upload_name.name, status_moved=moves_status, paused_from=stage,
                  **({"db_companion": companion} if companion else {}))


def cmd_restore(api: AssemblaApi, args) -> int:
    """Resume on any PC: rebuild DEV_TICKETS/N from the latest attached HANDOVER-N-*.md."""
    number = bare_number(args.ticket)
    folder = ticket_dir(number)
    pattern = re.compile(rf"^HANDOVER-{number}-\d{{8}}-\d{{4}}\.md$")
    handovers = sorted((a for a in api.get_attachments(number) if pattern.match(a.get("name") or "")),
                       key=lambda a: a.get("created_at") or "")
    if not handovers:
        return result("NO_HANDOVER", ticket=number, message="No HANDOVER attachment on the ticket.")
    latest = handovers[-1]
    local_state, _ = common.load_state(number)
    if local_state and not args.force:
        return result("LOCAL_EXISTS", ticket=number, local_stage=local_state.get("stage"),
                      local_paused_at=local_state.get("paused_at"), attachment=latest["name"],
                      attachment_at=latest.get("created_at"),
                      message="A local work folder exists. Use --force to overwrite it from the attachment.")
    text = api.download_attachment(latest).decode("utf-8", errors="replace")
    folder.mkdir(parents=True, exist_ok=True)
    restored = []
    for name in EMBEDDED:
        match = re.search(rf"<!-- BEGIN {re.escape(name)} -->\n(.*?)\n?<!-- END {re.escape(name)} -->", text, re.S)
        if match and match.group(1).strip():
            (folder / name).write_text(match.group(1).rstrip() + "\n", encoding="utf-8")
            restored.append(name)
    (folder / "HANDOVER.md").write_text(text, encoding="utf-8")
    if "state.json" not in restored:
        return result("INCOMPLETE", 1, ticket=number, restored=restored, message="state.json missing from hand-over.")
    state, state_path = common.load_state(number)
    state.setdefault("history", []).append({"stage": "restored", "result": latest["name"], "at": now_iso()})
    write_state(state_path, state)
    record_metric(number, "resume", attachment=latest["name"])
    return result("RESTORED", ticket=number, attachment=latest["name"], restored=restored,
                  resume_stage=state.get("paused_from") or state.get("stage"), paused_by=state.get("paused_by"))
