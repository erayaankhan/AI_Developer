"""The author side of the review: `dt handoff` and `dt assign`."""

import json

import teams_notify
from assembla_api import AssemblaApi, bare_number

from develop_ticket.comments import needs_rca_clca, post_or_edit_comment, summary_problems
from develop_ticket.common import CONFIG, STATUS, _write_state, now_iso, record_metric, result, ticket_dir
from develop_ticket.protocol import _brief_section, handoff_payload, latest_verdict, render_handoff
from develop_ticket import common, review


def cmd_handoff(api: AssemblaApi, args) -> int:
    """Developer hand-off: build HANDOFF.md from the ticket's state and, with --yes, post it as one comment (edited in
    place on a later round). Refuses unless the code is pushed, the gates pass on exactly these files, the functional
    checks are decided and SUMMARY.md is valid: the reviewer must get evidence that is complete and current."""
    number = bare_number(args.ticket)
    state, state_path = common.load_state(number)
    if state_path is None:
        return result("NO_STATE", 1, ticket=number, message="Run fetch first.")

    wf_push = common.push_state(number)
    if wf_push["status"] != "PUSHED":
        return result(wf_push["status"], ticket=number, repo="wf", branch=wf_push.get("branch"),
                      message="Commit and push everything first (dt pushed, dt sync-branch).")
    db = state.get("db") or {}
    if db.get("files"):
        db_push = common.push_state(str(db["ticket"]), common.DB_REPO)
        if db_push["status"] != "PUSHED":
            return result(db_push["status"], ticket=number, repo="db", branch=db_push.get("branch"),
                          message="The DB branch must be committed and pushed too (dt pushed D --db).")

    import local_gates  # lazy: it reconfigures stdout on import
    last = state.get("gates") or {}
    if last.get("status") not in ("PASS", "NO_CHANGES"):
        return result("GATES_REQUIRED", ticket=number, gates=last.get("status"),
                      message="Run local_gates.py --ticket N after the coding; it must pass before hand-off.")
    if local_gates.diff_fingerprint(local_gates.changed_files()) != last.get("fingerprint"):
        return result("GATES_STALE", ticket=number, gates=last.get("status"),
                      message="The files changed since the last gates run: re-run local_gates.py --ticket N.")
    import functional
    from develop_ticket.functional_cmds import auto_restore_env
    auto_restore_env(number, state)
    problem = functional.gate_problem(state, lambda: local_gates.diff_fingerprint(local_gates.changed_files()))
    if problem:
        return result(problem[0], ticket=number, mode=state.get("functional_mode"), message=problem[1])
    from develop_ticket.quality_cmds import quality_problem
    problem = quality_problem(state, local_gates.diff_fingerprint(local_gates.changed_files()))
    if problem:
        return result(problem[0], ticket=number, quality=(state.get("quality") or {}).get("status"), message=problem[1])

    summary = ticket_dir(number) / "SUMMARY.md"
    if not summary.is_file():
        return result("NOT_FOUND", 1, ticket=number, file=str(summary), message="Write SUMMARY.md first (SUMMARY_FORMAT.md).")
    errors, warnings = summary_problems(summary.read_text(encoding="utf-8"),
                                        needs_rca_clca(api.get_ticket(number), state),
                                        state.get("functional_mode") == "skip")
    if errors:
        return result("SUMMARY_CHECK_FAILED", 1, ticket=number, errors=errors, warnings=warnings,
                      message="Fix SUMMARY.md (see SUMMARY_FORMAT.md) before the hand-off.")

    db_head = common.git("rev-parse", "HEAD", repo=common.DB_REPO).stdout.strip() if db.get("files") else None
    payload = handoff_payload(state, number, api.get_current_user()["login"], common.git("rev-parse", "HEAD").stdout.strip(),
                              db_head, _brief_section(number, "README impact"))
    body = render_handoff(payload)
    (ticket_dir(number) / "HANDOFF.md").write_text(body, encoding="utf-8")
    if not args.yes:
        print("---- hand-off comment preview (not posted) ----\n" + body + "-----------------------------------------------")
        return result("PREVIEW", ticket=number, head=payload["head"][:12], round=payload["round"],
                      confirm=common.confirm_token("handoff", {"ticket": number, "body": body,
                                                               "edit": (state.get("handoff") or {}).get("comment_id")}),
                      message="Re-run with --yes --confirm <confirm> to post it (edited in place if already posted).")

    previous = (state.get("handoff") or {}).get("comment_id")
    refused = common.confirm_refusal("handoff", number, args, {"ticket": number, "body": body, "edit": previous})
    if refused is not None:
        return refused
    status, fields = post_or_edit_comment(api, number, body.strip(), previous)
    if status in ("POSTED", "EDITED") and fields.get("comment_id") is not None:
        state["handoff"] = {"comment_id": fields["comment_id"], "head": payload["head"], "round": payload["round"],
                            "author": payload["author"], "risk": payload.get("risk"), "at": now_iso()}
        _write_state(state_path, state, f"handoff {status.lower()} round {payload['round']}")
        record_metric(number, "handoff", round=payload["round"], risk=payload["risk"])
    return result(status, 1 if status in ("COMMENT_NOT_FOUND", "NOT_YOUR_COMMENT") else 0, ticket=number,
                  head=payload["head"][:12], round=payload["round"], **fields)


def _review_request(number: str, ticket: dict, state: dict, login: str, user: dict) -> dict:
    """The Teams message for the reviewer `login` (config `teams`)."""
    handoff = state.get("handoff") or {}
    return teams_notify.review_request(
        number=number, title=ticket.get("summary") or "", url=teams_notify.ticket_url(CONFIG, number), reviewer=login,
        reviewer_name=user.get("name") or login, email=teams_notify.reviewer_email(CONFIG, login, user),
        author=handoff.get("author") or "", round_no=handoff.get("round"),
        risk=handoff.get("risk") or state.get("risk_floor") or (state.get("risk_check") or {}).get("min_tier"))


def _teams_preview(number, ticket, state, login, user) -> dict:
    """What the hand-off would post on Teams (shown in the preview, so the developer's yes covers it)."""
    if not teams_notify.enabled(CONFIG):
        return {"status": "OFF"}
    message = _review_request(number, ticket, state, login, user)
    problem = teams_notify.webhook_problem(CONFIG)
    return {"status": problem[0] if problem else "WILL_POST", "message": message["text"],
            "mentions": bool(teams_notify.reviewer_email(CONFIG, login, user)),
            **({"note": problem[1]} if problem else {})}


def _teams_send(number, ticket, state, state_path, login, user) -> dict:
    """Post the review request and record it in state.handoff.teams. A failure never undoes the assignment."""
    message = _review_request(number, ticket, state, login, user)
    sent = teams_notify.post(CONFIG, message["payload"])
    handoff = state.get("handoff") or {}
    state["handoff"] = dict(handoff, teams={"status": sent["status"], "reviewer": login, "round": handoff.get("round"),
                                            "at": now_iso()})
    _write_state(state_path, state, f"teams {sent['status'].lower()}")
    record_metric(number, "teams_notify", status=sent["status"], reviewer=login)
    return sent


def cmd_teams_notify(api: AssemblaApi, args) -> int:
    """Post (or re-post) the Teams review request for the reviewer the ticket was assigned to with `dt assign`."""
    number = bare_number(args.ticket)
    if not teams_notify.enabled(CONFIG):
        return result("TEAMS_OFF", ticket=number, message="Teams messages are not enabled for this team (config `teams`).")
    state, state_path = common.load_state(number)
    if state_path is None:
        return result("NO_STATE", 1, ticket=number, message="No local work folder for this ticket.")
    handoff = state.get("handoff") or {}
    login = handoff.get("reviewer")
    if not login:
        return result("NOT_ASSIGNED", ticket=number, message="Assign a reviewer first (dt assign N --to LOGIN).")
    ticket = api.get_ticket(number)
    if ticket.get("status") != STATUS["code_review"]:
        return result("WRONG_STATUS", ticket=number, ticket_status=ticket.get("status"),
                      message=f"The ticket is no longer in '{STATUS['code_review']}': nothing to ask for.")
    me = api.get_current_user()
    if login.lower() == str(me.get("login", "")).lower():
        return result("SELF_REVIEW", ticket=number, message="You are the reviewer: no Teams message is needed.")
    user = {str(u.get("login", "")).lower(): u for u in api.get_space_users()}.get(login.lower()) or {}
    before = handoff.get("teams") or {}
    if before.get("status") == "SENT" and before.get("round") == handoff.get("round") and not args.again:
        return result("ALREADY_SENT", ticket=number, reviewer=login, at=before.get("at"),
                      message="Already posted for this round: add --again to post a reminder.")
    preview = _teams_preview(number, ticket, state, login, user)
    if preview["status"] != "WILL_POST":
        return result(preview["status"], ticket=number, **{k: v for k, v in preview.items() if k != "status"})
    if not args.yes:
        return result("PREVIEW", ticket=number, reviewer=login, teams=preview,
                      message="Re-run with --yes to post this on the team's Teams channel.")
    sent = _teams_send(number, ticket, state, state_path, login, user)
    return result(sent["status"], 1 if sent["status"] != "SENT" else 0, ticket=number, reviewer=login,
                  **{k: v for k, v in sent.items() if k != "status"})


def cmd_assign(api: AssemblaApi, args) -> int:
    """Developer hand-off, last step: assign the Code Review ticket to a reviewer from the configured `reviewers` list.
    `--list` shows that list; `--to LOGIN` previews, and with --yes assigns and re-reads the ticket to verify.
    Refuses unless the HANDOFF comment is posted and still matches the pushed head."""
    number = bare_number(args.ticket)
    reviewers = [str(r).strip() for r in CONFIG.get("reviewers") or [] if str(r).strip()]
    if not args.list and not args.to:
        return result("BAD_ARG", 1, ticket=number, message="Use --list to see the reviewers, or --to LOGIN.")
    if not reviewers:
        return result("REVIEWERS_NOT_CONFIGURED", ticket=number,
                      message="No reviewers are configured: the lead adds their Assembla logins to `reviewers` in "
                              "dev_ticket.config.json (or the local config).")
    users = {str(u.get("login", "")).lower(): u for u in api.get_space_users()}
    if args.list:
        rows = [{"login": r, "name": users[r.lower()].get("name")} if r.lower() in users
                else {"login": r, "name": None, "note": "not found in the space"} for r in reviewers]
        return result("REVIEWERS", ticket=number, reviewers=rows)

    state, state_path = common.load_state(number)
    if state_path is None:
        return result("NO_STATE", 1, ticket=number, message="Run fetch first.")
    ticket = api.get_ticket(number)
    if ticket.get("status") != STATUS["code_review"]:
        return result("WRONG_STATUS", ticket=number, ticket_status=ticket.get("status"),
                      message=f"Assign only after the ticket is in '{STATUS['code_review']}'.")
    login = args.to.strip()
    chosen = next((r for r in reviewers if r.lower() == login.lower()), None)
    if chosen is None:
        return result("NOT_A_REVIEWER", ticket=number, login=login, reviewers=reviewers,
                      message="Pick one of the configured reviewers (dt assign N --list).")
    me = api.get_current_user()
    if chosen.lower() == str(me.get("login", "")).lower() and not common.self_review_allowed():
        return result("SELF_ASSIGN", ticket=number, message="A ticket is handed to someone else, not to yourself.")

    handoff = state.get("handoff") or {}
    posted = {str(c.get("id")) for c in api.get_comments(number)}
    if not handoff.get("comment_id") or str(handoff["comment_id"]) not in posted:
        return result("NO_HANDOFF", ticket=number, message="Post the hand-off first: dt handoff N --yes.")
    wf_push = common.push_state(number)
    if wf_push["status"] != "PUSHED":
        return result(wf_push["status"], ticket=number, branch=wf_push.get("branch"),
                      message="Commit and push everything first (dt pushed, dt sync-branch).")
    head = common.git("rev-parse", "HEAD").stdout.strip()
    if head != handoff.get("head") and not review.docs_only_delta(handoff.get("head"), head):  # documentation or a master merge keeps it
        return result("STALE_HANDOFF", ticket=number, handoff_head=str(handoff.get("head"))[:12],
                      message="The code changed after the hand-off comment: run dt handoff N --yes again.")

    user = users.get(chosen.lower())
    if user is None or not user.get("id"):
        return result("REVIEWER_NOT_FOUND", ticket=number, login=chosen,
                      message="That login is not a user of the Assembla space: tell the lead.")
    is_me = chosen.lower() == str(me.get("login", "")).lower()
    if str(ticket.get("assigned_to_id")) == str(user["id"]) and not (is_me and handoff.get("reviewer") != chosen):
        return result("ALREADY_ASSIGNED", ticket=number, reviewer=chosen)
    teams = None if is_me else _teams_preview(number, ticket, state, chosen, user)
    if teams and teams["status"] == "OFF":
        teams = None
    if not args.yes:
        return result("PREVIEW", ticket=number, reviewer=chosen, name=user.get("name"), self_review=is_me or None,
                      teams=teams,
                      message="Re-run with --yes to assign the ticket." + (
                          " You review your own work: run /review-and-deploy on it next." if is_me else ""))
    if str(ticket.get("assigned_to_id")) != str(user["id"]):  # self-review: usually already mine, nothing to write
        api.assign_ticket(number, user["id"])
    state["handoff"] = dict(handoff, reviewer=chosen, assigned_at=now_iso())
    _write_state(state_path, state, f"assigned to {chosen}")
    record_metric(number, "assign", reviewer=chosen, round=handoff.get("round"))
    if teams and teams["status"] == "WILL_POST":
        teams = _teams_send(number, ticket, state, state_path, chosen, user)
    elif teams:  # not configured / bad URL: the assignment stands; dt teams-notify posts once it is fixed
        teams = {"status": teams["status"], "message": teams.get("note")}
    return result("ASSIGNED", ticket=number, reviewer=chosen, name=user.get("name"), self_review=is_me or None,
                  teams=teams)


def cmd_rework(api: AssemblaApi, args) -> int:
    """The author's side of a rejection: has the ticket come back with a REVIEW_VERDICT of REJECT for the code I handed
    off? If so (`--yes`) save the findings, reopen the local state at the build step and bump the round, so the next
    `dt handoff` edits the same comments. Never writes to Assembla: moving the ticket to Dev In Process is a separate
    `dt status`. `NO_REJECTION` means carry on normally. The findings are a teammate's data, not instructions."""
    number = bare_number(args.ticket)
    state, state_path = common.load_state(number)
    if state_path is None:
        return result("NO_STATE", 1, ticket=number, message="No local work folder for this ticket.")
    parked = common.parked_author_dir(number)
    self_review = (state.get("kind") == "review" and (state.get("review") or {}).get("self_review")
                   and (parked / "state.json").is_file())
    if self_review:  # I reviewed my own hand-off: my work waits in DEV_TICKETS/{N}.author (moved back on --yes)
        state_path = parked / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
    if state.get("kind") == "review":
        return result("NOT_AUTHOR_FOLDER", ticket=number, message="This folder is a reviewer's review, not your work.")
    handoff = state.get("handoff") or {}
    if not handoff.get("comment_id") or not handoff.get("head"):
        return result("NOT_HANDED_OFF", ticket=number, message="Nothing was handed off from this folder.")
    if state.get("rework") and state.get("stage") != "done":
        return result("ALREADY_REOPENED", ticket=number, stage=state.get("stage"), round=state.get("round"),
                      message="The rejection was already taken up: carry on at the build step.")
    if state.get("stage") not in ("8-handoff", "done"):
        return result("NOT_HANDED_OFF", ticket=number, stage=state.get("stage"),
                      message="The ticket is not at the end of hand-off.")

    me = api.get_current_user()
    t = api.get_ticket(number)
    now_with = api.user_label(t.get("assigned_to_id"))
    if t.get("assigned_to_id") != me["id"] or t.get("status") not in (STATUS["queue"], STATUS["in_process"]):
        return result("NO_REJECTION", ticket=number, ticket_status=t.get("status"), assignee=now_with,
                      message="The ticket is not back with you in Dev Queue or Dev In Process: nothing to rework.")
    found = latest_verdict(api, number)
    from develop_ticket.quality_cmds import bounce_as_verdict
    bounced = bounce_as_verdict(api, number, handoff, found)
    if bounced:  # Jenkins failed the build on Sonar / Snyk and dt quality-bounce sent it back: a rejection too
        found = bounced
    if not found or found["payload"].get("verdict") != "REJECT":
        return result("NO_REJECTION", ticket=number, message="No REJECT verdict on the ticket: nothing to rework.")
    p = found["payload"]
    if p.get("round") != handoff.get("round") or (p.get("head") != handoff.get("head")
                                                  and not review.docs_only_delta(handoff.get("head"), p.get("head"))):
        return result("VERDICT_NOT_FOR_THIS_HANDOFF", ticket=number, verdict_round=p.get("round"),
                      handoff_round=handoff.get("round"), verdict_head=str(p.get("head"))[:12],
                      handoff_head=str(handoff.get("head"))[:12],
                      message="The verdict is about other code than the one you handed off: ask the reviewer.")
    writer = found["writer_login"].lower()
    expected = str(handoff.get("reviewer") or "").lower()
    configured = {str(r).strip().lower() for r in CONFIG.get("reviewers") or []}
    if found.get("bounce"):  # whoever ran dt quality-bounce, as long as they are a configured reviewer
        expected = writer if writer in configured else "(a configured reviewer)"
    if writer != str(p.get("reviewer", "")).lower() or (expected and writer != expected) or (not expected and writer not in configured):
        return result("REVIEW_AUTHOR_MISMATCH", ticket=number, writer=found["writer_login"], claimed=p.get("reviewer"),
                      assigned_reviewer=handoff.get("reviewer"),
                      message="The verdict was not written by the reviewer the ticket was assigned to: not trusted.")

    rnd = p.get("round") or 1
    findings = p.get("findings") or []
    summary = {"round": rnd, "next_round": rnd + 1, "reviewer": found["writer_login"], "summary": p.get("summary"),
               "findings": findings, "fix_list": p.get("fix_list") or [], "notes": p.get("notes") or []}
    saved = ticket_dir(number) / "reviews" / f"review-feedback-round{rnd}.md"
    steps = ([f'dt status {number} --to "{STATUS["in_process"]}" --yes'] if t.get("status") == STATUS["queue"] else []) + [
        "settle the plan with the developer", f"dt branch {number}", f"dt sync-branch {number} --yes", f"dt deps {number}"]
    if not args.yes:
        return result("PREVIEW", ticket=number, will_save=str(saved), steps=steps, **summary,
                      message="Re-run with --yes to save the findings and reopen the ticket at the build step.")
    kept_review = None
    if self_review:
        try:
            kept_review = common.restore_author_folder(number, rnd)
        except OSError as exc:
            return result("RESTORE_FAILED", 1, ticket=number, message=f"Could not put the author folder back: {exc}")
        state_path = ticket_dir(number) / "state.json"
    saved.parent.mkdir(parents=True, exist_ok=True)
    saved.write_text(found["text"], encoding="utf-8")
    for key in ("reviewer", "assigned_at"):
        handoff.pop(key, None)  # the next dt assign picks a reviewer again; comment_id stays, so dt handoff edits in place
    state.update(stage="6-build", round=rnd + 1, handoff=handoff,
                 rework={"round": rnd, "head": p.get("head"), "reviewer": found["writer_login"],
                         "comment_id": found["comment_id"], "findings": len(findings), "at": now_iso()})
    _write_state(state_path, state, f"rework: round {rnd} rejected by {found['writer_login']}")
    record_metric(number, "rework", round=rnd, findings=len(findings), reviewer=found["writer_login"])
    return result("REWORK_READY", ticket=number, saved=str(saved), steps=steps,
                  review_folder_kept=str(kept_review) if kept_review else None, **summary)
