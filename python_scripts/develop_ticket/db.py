"""Database companion tickets: db-ticket, db-create, db-files, db-branch, and the follow-up ticket creation."""

import re
import subprocess
from difflib import SequenceMatcher
from pathlib import Path

from assembla_api import PLAN_LEVELS, AssemblaApi, AssemblaError, bare_number
from state_io import write_state

from develop_ticket import common
from develop_ticket.common import (CONFIG, RELATED, branch_name, inside_ticket_folder, is_db_node, record_metric, result,
                                    ticket_dir)


def find_db_ticket(api: AssemblaApi, number: str) -> dict:
    """D-71: the DB ticket related to WF ticket N. The workflow creates one per main ticket (`dt db-create`) and links
    it with relationship Related; an active Story with Node = C2M-DB (or titled `DB | #N`) on that link is the one.

    DB changes (SP/DML/DDL) are never made on FB_N in the DB repo; they go onto the branch of this DB ticket."""
    ticket = api.get_ticket(number)
    inactive = set(CONFIG["epics"]["inactive_statuses"])

    def related(t: dict) -> list:
        found = []
        for assoc in api.get_associations(t.get("number")):
            if assoc.get("relationship") != RELATED:
                continue
            other = assoc.get("ticket2_id") if assoc.get("ticket1_id") == t.get("id") else assoc.get("ticket1_id")
            found.append(api.get_ticket_by_id(other))
        return found

    def row(t: dict, via=None) -> dict:
        return {"ticket": t.get("number"), "summary": t.get("summary"), "status": t.get("status"),
                "plan_level": PLAN_LEVELS.get(t.get("hierarchy_type"), t.get("hierarchy_type")),
                "assignee": api.user_label(t.get("assigned_to_id")), **({"via_requirement": via} if via else {})}

    neighbours = related(ticket)
    rows = [row(t) for t in neighbours  # already linked Related to N, so the Node is enough
            if is_db_node(t) and t.get("status") not in inactive]
    if not rows and CONFIG["db"].get("find_via_requirement"):
        # A team that relates every Node ticket (code, DB) to one requirement ticket, not to each other: the DB ticket is
        # a Related ticket of N's requirement ticket (a Related ticket of N whose Node is not a repo Node, e.g. -NA-).
        repo_nodes = {(r.get("config") or {}).get("repo_node") for r in (CONFIG.get("repos") or {}).values()}
        repo_nodes |= {CONFIG["db"]["node_value"]}
        for hub in neighbours:
            if common.ticket_node(hub) in repo_nodes:
                continue
            rows += [row(t, via=hub.get("number")) for t in related(hub)
                     if t.get("id") != ticket.get("id") and is_db_node(t) and t.get("status") not in inactive]
    info = {"candidates": rows}
    if not rows:
        return dict(info, status="NO_DB_TICKET")
    if len(rows) > 1:
        return dict(info, status="MULTIPLE_DB_TICKETS")
    return dict(info, status="FOUND", db_ticket=rows[0]["ticket"])


def cmd_db_ticket(api: AssemblaApi, args) -> int:
    info = find_db_ticket(api, bare_number(args.ticket))
    return result(info.pop("status"), ticket=bare_number(args.ticket), **info)


def cmd_db_create(api: AssemblaApi, args) -> int:
    """Create the DB ticket for WF ticket N in Assembla and link it as Related (D-71).

    Attributes: Plan level Story, Node C2M-DB, tag WF, the main ticket's milestone / category / priority / status,
    assigned to the developer. Title: `DB | #{N} <what changes in the database>`. Idempotent: a related DB ticket (or
    state.db.ticket) means nothing is created. Without --yes it only previews."""
    number = bare_number(args.ticket)
    state, path = common.load_state(number)
    if path is None:
        return result("NO_STATE", 1, ticket=number, message="Run fetch first.")
    known = (state.get("db") or {}).get("ticket")
    if known:
        return result("ALREADY_EXISTS", ticket=number, db_ticket=known,
                      message=f"DB ticket #{known} is already recorded for this ticket; nothing created.")
    main = api.get_ticket(number)
    if main.get("milestone_id") not in {m["id"] for m in api.get_current_milestones()}:
        return result("NOT_CURRENT_MILESTONE", 1, ticket=number, message="The ticket is not in a _CURRENT milestone.")
    info = find_db_ticket(api, number)
    if info["status"] == "FOUND":
        state.setdefault("db", {})["ticket"] = int(info["db_ticket"])
        write_state(path, state)
        return result("ALREADY_EXISTS", ticket=number, db_ticket=info["db_ticket"], candidates=info["candidates"],
                      message="A related DB ticket exists and is now recorded; nothing created.")
    if info["status"] == "MULTIPLE_DB_TICKETS":
        return result("MULTIPLE_DB_TICKETS", 1, ticket=number, candidates=info["candidates"],
                      message="Several DB tickets are related to this ticket: which one?")
    title = (args.title or "").strip()
    # db.title_template: the team's title convention, {number} = the main ticket (default `DB | #{number} <what>`).
    template = CONFIG["db"].get("title_template") or "DB | #{number} "
    prefix = template.format(number=number)
    if not title.startswith(prefix) or not title[len(prefix):].strip() or len(title) > 250:
        return result("BAD_TITLE", 1, ticket=number, title=title,
                      message=f"The title must read `{prefix}<what changes in the database>` (max 250 characters).")
    description = f"DB changes for #{number}: {main.get('summary')}\n\nRelated to #{number}."
    if args.description_file:
        if not inside_ticket_folder(number, args.description_file):
            return result("FILE_OUTSIDE_TICKET_FOLDER", 1, ticket=number, file=args.description_file,
                          message=f"Only files inside {ticket_dir(number)} can be used.")
        description = Path(args.description_file).read_text(encoding="utf-8").strip() or description
    attrs = CONFIG["deploy_attributes"]
    me = api.get_current_user()
    category = ((main.get("custom_fields") or {}).get("Category") or "").strip()
    custom = {attrs["node_field"]: CONFIG["db"]["node_value"], **({"Category": category} if category else {})}
    plan = {"summary": title, "plan_level": PLAN_LEVELS.get(attrs["plan_level"], attrs["plan_level"]),
            **custom, "tag": attrs["tag"], "milestone_id": main.get("milestone_id"), "priority": main.get("priority"),
            "status": main.get("status"), "assigned_to": me.get("login"),
            "relation": f"Related -> #{number}", "description": description[:300]}
    confirm_payload = {"ticket": number, "title": title, "description": description, "milestone_id": main.get("milestone_id"),
                       "priority": main.get("priority"), "status": main.get("status"), "assignee": me.get("id"), "custom": custom}
    if not args.yes:
        return result("PREVIEW", ticket=number, create=plan, confirm=common.confirm_token("db-create", confirm_payload),
                      message="Re-run with --yes --confirm <confirm> to create it.")
    refused = common.confirm_refusal("db-create", number, args, confirm_payload)
    if refused is not None:
        return refused

    # Assembla refuses a create without the required custom fields (Category, Node: HTTP 422), so they go with it.
    created = api.create_ticket({"summary": title, "description": description, "milestone_id": main.get("milestone_id"),
                                 "assigned_to_id": me["id"], "priority": main.get("priority"), "custom_fields": custom})
    db_number = created["number"]
    # The ticket exists from here on: remember it first, so a failure below can never lead to a second one.
    state.setdefault("db", {})["ticket"] = int(db_number)
    write_state(path, state)
    problems = []
    try:
        api.update_ticket(db_number, {"hierarchy_type": attrs["plan_level"], "custom_fields": custom,
                                      "tags": [attrs["tag"]] if attrs["tag"] else []})
        if api.get_ticket(db_number).get("status") != main.get("status"):
            api.update_status(db_number, main.get("status"))
        api.add_association(number, main["id"], created["id"], RELATED)
        if CONFIG["db"].get("find_via_requirement"):  # the team's convention: every Node ticket is Related to the requirement
            repo_nodes = {(r.get("config") or {}).get("repo_node") for r in (CONFIG.get("repos") or {}).values()}
            repo_nodes |= {CONFIG["db"]["node_value"]}
            for assoc in api.get_associations(number):
                other = assoc.get("ticket2_id") if assoc.get("ticket1_id") == main["id"] else assoc.get("ticket1_id")
                hub = api.get_ticket_by_id(other) if assoc.get("relationship") == RELATED else {}
                if hub and hub.get("id") != created["id"] and common.ticket_node(hub) not in repo_nodes:
                    api.add_association(hub.get("number"), hub["id"], created["id"], RELATED)
    except AssemblaError as exc:
        problems.append(str(exc))
    t = api.get_ticket(db_number)
    cf = t.get("custom_fields") or {}
    if t.get("hierarchy_type") != attrs["plan_level"]:
        problems.append("Plan level is not Story")
    if (cf.get(attrs["node_field"]) or "").strip() != CONFIG["db"]["node_value"]:
        problems.append(f"{attrs['node_field']} is not {CONFIG['db']['node_value']}")
    if attrs["tag"] and attrs["tag"] not in api.get_tags(db_number):
        problems.append(f"tag {attrs['tag']} missing")
    if t.get("milestone_id") != main.get("milestone_id") or t.get("assigned_to_id") != me["id"]:
        problems.append("milestone or assignee differs from the plan")
    if t.get("status") != main.get("status"):
        problems.append(f"status is {t.get('status')!r}, expected {main.get('status')!r}")
    linked = any(a.get("relationship") == RELATED and created["id"] in (a.get("ticket1_id"), a.get("ticket2_id"))
                 for a in api.get_associations(number))
    if not linked:
        problems.append("the Related link to the main ticket is missing")
    record_metric(number, "db_ticket", db_ticket=db_number, problems=len(problems))
    return result("CREATED" if not problems else "CREATED_WITH_PROBLEMS", 0 if not problems else 2, ticket=number,
                  db_ticket=db_number, summary=t.get("summary"), problems=problems,
                  message=("DB ticket created and linked." if not problems else
                           "The ticket exists (it is recorded; do NOT create another) but these checks failed: fix "
                           "them in Assembla by hand or ask the developer."))


def _tag_sweep_hit(number: str, hit: str, ticket) -> bool:
    """Write `ticket: #N` onto the CLCA-sweep bullet `hit` names in BRIEF.md. False when there is no such untagged bullet."""
    import sweep
    brief = ticket_dir(number) / "BRIEF.md"
    if not brief.is_file():
        return False
    status, text = sweep.link_ticket(brief.read_text(encoding="utf-8"), hit, ticket)
    if status != "OK":
        return False
    brief.write_text(text, encoding="utf-8")
    return True


SIMILAR_RATIO = 0.85  # normalised-title similarity at which an open Related ticket counts as the same follow-up


def _title_key(title) -> str:
    """A title reduced to what identifies it: no `Bug |` style prefix, lower case, letters and digits only."""
    text = re.sub(r"^[^|]*\|", "", str(title or "")).lower()
    return " ".join(re.findall(r"[a-z0-9]+", text))


def similar_open_tickets(api: AssemblaApi, main: dict, title: str) -> list:
    """Open tickets Related to the main ticket whose title is near-identical to `title`, most similar first. Asks Assembla,
    so it also finds a follow-up created on another machine or after the work folder was deleted (state.followups cannot)."""
    inactive = set(CONFIG["epics"]["inactive_statuses"])
    mine, found = _title_key(title), []
    for assoc in api.get_associations(main["number"]):
        if assoc.get("relationship") != RELATED:
            continue
        other_id = assoc.get("ticket2_id") if assoc.get("ticket1_id") == main.get("id") else assoc.get("ticket1_id")
        other = api.get_ticket_by_id(other_id) if other_id else {}
        if not other or other.get("status") in inactive:
            continue
        ratio = SequenceMatcher(None, mine, _title_key(other.get("summary"))).ratio()
        if ratio >= SIMILAR_RATIO:
            found.append({"ticket": other.get("number"), "summary": other.get("summary"), "status": other.get("status"),
                          "similarity": round(ratio, 2)})
    return sorted(found, key=lambda f: -f["similarity"])


def cmd_followup_create(api: AssemblaApi, args) -> int:
    """Create ONE follow-up ticket for WF ticket N (a CLCA-sweep hit or a review finding marked follow-up) and link it Related.

    Attributes: Plan level Story, Node WF, tag WF, the main ticket's milestone and C2M-Module, Category (`--category`, default
    the main ticket's), status New, assigned to its creator (the current user), no priority. The description comes from a file in the ticket folder.
    Idempotent per title (state.followups), and refuses (SIMILAR_EXISTS) when an open ticket Related to N already has a
    near-identical title in Assembla (`--allow-similar` overrides). Without --yes it only previews. `--hit "<path:line>"` ties it to the CLCA-sweep
    bullet it raises: that bullet in BRIEF.md must exist (checked before anything is created) and gets ` - ticket: #N`."""
    number = bare_number(args.ticket)
    state, path = common.load_state(number)
    if path is None:
        return result("NO_STATE", 1, ticket=number, message="Run fetch first.")
    title = (args.title or "").strip()
    if not title or len(title) > 250:
        return result("BAD_TITLE", 1, ticket=number, title=title, message="A title of 1 to 250 characters is needed.")
    known = next((f for f in state.get("followups") or [] if f.get("title") == title), None)
    hit = (getattr(args, "hit", None) or "").strip()
    if known:
        updated = _tag_sweep_hit(number, hit, known["number"]) if hit and args.yes else False  # a preview never writes
        return result("ALREADY_EXISTS", ticket=number, followup=known["number"], brief_updated=updated,
                      message=f"#{known['number']} was already created with this title; nothing created.")
    if not inside_ticket_folder(number, args.description_file) or not Path(args.description_file).is_file():
        return result("FILE_OUTSIDE_TICKET_FOLDER", 1, ticket=number, file=args.description_file,
                      message=f"--description-file needs an existing file inside {ticket_dir(number)}.")
    description = Path(args.description_file).read_text(encoding="utf-8").strip()
    if not description:
        return result("EMPTY", 1, ticket=number, file=args.description_file, message="The description file is empty.")
    bullet = None
    if hit:  # the sweep bullet this ticket is for must exist before anything is created: no ticket without its record
        import sweep
        brief = ticket_dir(number) / "BRIEF.md"
        found, detail = sweep.find_hit(brief.read_text(encoding="utf-8") if brief.is_file() else "", hit)
        if found != "OK":
            return result(found, 1, ticket=number, hit=hit, **({"candidates": [d.strip()[:100] for d in detail]} if detail else {}),
                          message="--hit must match exactly one `decision: follow-up` bullet of `## Same flaw elsewhere` in "
                                  "BRIEF.md that has no ticket or decline yet (a part of the `path:line` is enough).")
        bullet = detail.strip()
    main = api.get_ticket(number)
    similar = similar_open_tickets(api, main, title)
    if similar and not getattr(args, "allow_similar", False):
        return result("SIMILAR_EXISTS", ticket=number, similar=similar,
                      message="An open ticket Related to this one has a near-identical title: it is probably this follow-up already "
                              "(raised on another machine, or the work folder was lost). Show it to the developer. If it is the "
                              "same flaw, write ` - ticket: #N` onto the sweep bullet by hand and create nothing; only if it is "
                              "a different one, re-run with --allow-similar.")
    mcf = main.get("custom_fields") or {}
    attrs, fu = CONFIG["deploy_attributes"], CONFIG["followup"]
    category = (args.category or mcf.get("Category") or "").strip()
    custom = {attrs["node_field"]: attrs["node_value"], fu["module_field"]: (mcf.get(fu["module_field"]) or "").strip(),
              **({"Category": category} if category else {})}
    custom = {k: v for k, v in custom.items() if v}
    milestone = (api.get_milestone(main.get("milestone_id")) or {}).get("title") or main.get("milestone_id")
    me = api.get_current_user()
    plan = {"summary": title, "plan_level": PLAN_LEVELS.get(attrs["plan_level"], attrs["plan_level"]), **custom,
            "tag": attrs["tag"], "milestone": milestone, "status": fu["status"], "assignee": me.get("login"),
            "priority": "(unset)", "relation": f"Related -> #{number}", "description": description,
            **({"brief_bullet": bullet[:160]} if bullet else {}), **({"similar_ignored": similar} if similar else {})}
    confirm_payload = {"ticket": number, "title": title, "description": description, "milestone_id": main.get("milestone_id"),
                       "status": fu["status"], "assignee": me.get("id"), "custom": custom, "hit": bullet}
    if not args.yes:
        return result("PREVIEW", ticket=number, create=plan, confirm=common.confirm_token("followup-create", confirm_payload),
                      message="Show this to the developer; re-run with --yes --confirm <confirm> only on their yes.")
    refused = common.confirm_refusal("followup-create", number, args, confirm_payload)
    if refused is not None:
        return refused

    # Assembla refuses a create without the required custom fields (Category, Node: HTTP 422), so they go with it.
    created = api.create_ticket({"summary": title, "description": description, "milestone_id": main.get("milestone_id"),
                                 "assigned_to_id": me["id"], "custom_fields": custom})
    new = created["number"]
    # The ticket exists from here on: remember it first, so a failure below can never lead to a second one.
    state.setdefault("followups", []).append({"title": title, "number": int(new)})
    write_state(path, state)
    brief_updated = _tag_sweep_hit(number, hit, new) if hit else False
    problems = []
    try:
        api.update_ticket(new, {"hierarchy_type": attrs["plan_level"], "custom_fields": custom, "tags": [attrs["tag"]] if attrs["tag"] else []})
        if api.get_ticket(new).get("status") != fu["status"]:
            api.update_status(new, fu["status"])
        api.add_association(number, main["id"], created["id"], RELATED)
    except AssemblaError as exc:
        problems.append(str(exc))
    t = api.get_ticket(new)
    cf = t.get("custom_fields") or {}
    if t.get("hierarchy_type") != attrs["plan_level"]:
        problems.append("Plan level is not Story")
    for name, value in custom.items():
        if (cf.get(name) or "").strip() != value:
            problems.append(f"{name} is not {value}")
    if attrs["tag"] and attrs["tag"] not in api.get_tags(new):
        problems.append(f"tag {attrs['tag']} missing")
    if t.get("milestone_id") != main.get("milestone_id"):
        problems.append("milestone differs from the main ticket's")
    if t.get("assigned_to_id") != me["id"]:
        problems.append("the ticket is not assigned to its creator")
    if t.get("status") != fu["status"]:
        problems.append(f"status is {t.get('status')!r}, expected {fu['status']!r}")
    if not any(a.get("relationship") == RELATED and created["id"] in (a.get("ticket1_id"), a.get("ticket2_id"))
               for a in api.get_associations(number)):
        problems.append("the Related link to the main ticket is missing")
    record_metric(number, "followup_ticket", followup=new, problems=len(problems))
    return result("CREATED" if not problems else "CREATED_WITH_PROBLEMS", 0 if not problems else 2, ticket=number,
                  followup=new, summary=t.get("summary"), problems=problems, brief_updated=brief_updated,
                  message=("Follow-up ticket created and linked." if not problems else
                           "The ticket exists (it is recorded; do NOT create another) but these checks failed: fix "
                           "them in Assembla by hand or ask the developer."))


def cmd_db_branch(_api, args) -> int:
    """Check out FB_<db ticket> in the DB repo; create it from the DB repo's master if it
    doesn't exist anywhere yet (D-21). Never commits. Pushes a new branch only with --push."""
    db_number = bare_number(args.db_ticket)
    branch = branch_name(db_number)
    repo = common.DB_REPO
    base = CONFIG["db"].get("base_branch", "master")
    if not (repo / ".git").exists():
        return result("NO_DB_REPO", 1, repo=str(repo), message="DB repo not found (config db.repo_path).")
    # With --ticket N the command also owns db.ticket / db.base. The FIRST time for a ticket (no db.base yet) the DB
    # working tree must be clean, on or off the branch, exactly like the WF repo (A-3): from then on every change in
    # it is this ticket's own. Later calls (resume, pause) are not re-checked.
    wf = bare_number(args.ticket) if getattr(args, "ticket", None) else None
    state, state_path = common.load_state(wf) if wf else ({}, None)
    if wf and state_path is None:
        return result("NO_STATE", 1, ticket=wf, message="Run fetch first.")
    fresh = bool(wf) and not (state.get("db") or {}).get("base")
    try:
        if fresh:
            unclean = [l for l in common.git("status", "--porcelain", "--untracked-files=all", repo=repo).stdout.splitlines()
                       if l.strip()]
            if unclean:
                tracked = [l for l in unclean if not l.startswith("??")]
                return result("DIRTY", repo=str(repo), branch=branch, tracked=len(tracked),
                              untracked=len(unclean) - len(tracked), total=len(unclean), files=unclean[:50],
                              message="The DB working tree must be clean before the ticket starts. The developer "
                                      "commits (elsewhere), stashes (git stash -u), deletes or excludes "
                                      "(.git/info/exclude) these files, then re-run. Never do it for them.")
        current = common.git("branch", "--show-current", repo=repo).stdout.strip()
        dirty = [l for l in common.git("status", "--porcelain", "--untracked-files=no", repo=repo).stdout.splitlines() if l.strip()]
        if dirty and current != branch:
            return result("DIRTY", repo=str(repo), branch=branch, current_branch=current, files=dirty[:50])
        remote_exists = bool(common.git("ls-remote", "--heads", "origin", branch, repo=repo).stdout.strip())
        local_exists = common.git("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}",
                           check=False, repo=repo).returncode == 0
        if remote_exists:
            common.git("fetch", "--quiet", "origin", f"refs/heads/{branch}:refs/remotes/origin/{branch}", repo=repo)
        if current == branch:
            action = "ALREADY_ON_BRANCH"
        elif local_exists:
            common.git("switch", branch, repo=repo)
            action = "CHECKED_OUT"
        elif remote_exists:
            common.git("switch", "--track", f"origin/{branch}", repo=repo)
            action = "CHECKED_OUT_REMOTE"
        else:
            common.git("fetch", "--quiet", "origin", base, repo=repo)
            common.git("switch", "--no-track", "-c", branch, f"origin/{base}", repo=repo)
            action = "CREATED_LOCAL"
        pushed = remote_exists
        if not remote_exists and args.push:
            common.git("push", "-u", "origin", branch, timeout=300, repo=repo)
            pushed = True
        behind = int(common.git("rev-list", "--count", f"HEAD..origin/{branch}", repo=repo).stdout.strip() or 0) if pushed else 0
        head = common.git("rev-parse", "HEAD", repo=repo).stdout.strip()
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        return result("FAILED", 1, repo=str(repo), branch=branch, message=str(exc))
    # `head` is recorded once per ticket as state db.base, so db_checks judges only this ticket's lines.
    if fresh:
        state.setdefault("db", {}).update(ticket=int(db_number), base=head)
        write_state(state_path, state)
    return result(action, repo=str(repo), branch=branch, pushed=pushed, behind_origin=behind, head=head,
                  db_base_recorded=fresh,
                  sql_root=str(repo / CONFIG["db"]["sql_root"]),
                  folders="Database/<schema>/(SP|Functions|Tables|DDL_DML)/")


def cmd_db_files(_api, args) -> int:
    """Set state.db.files from what the DB repo says belongs to this WF ticket (backlog A-3), not from a typed list.

    Files of the commits since db.base whose message starts `re#{D} - {N} - `, plus the uncommitted work in a DB
    tree that started clean. Other tickets' commits on the shared branch are left out and listed; commits that don't
    follow the convention are listed too, so an unattributed commit of this ticket is not lost silently. Run it after
    writing DB files, before the gates, and again before staging."""
    number = bare_number(args.ticket)
    state, path = common.load_state(number)
    if path is None:
        return result("NO_STATE", 1, ticket=number, message="Run fetch first.")
    db = state.get("db") or {}
    if not db.get("ticket") or not db.get("base"):
        return result("NO_DB_WORK", 1, ticket=number,
                      message="No DB work started: run `dt db-branch {D} --ticket N` first (it records db.ticket and db.base).")
    import db_checks  # lazy: it imports local_gates
    derived = db_checks.derive_db_files(db_checks.load_ticket_db(number))
    old = db.get("files") or []
    state["db"]["files"] = derived["files"]
    write_state(path, state)
    note = ""
    if derived["unconventional_commits"]:
        note = (" Commits without the `re#{D} - {N} - ` message were found since db.base: check whether any is this "
                "ticket's (then its files are missing from the list).")
    return result("DB_FILES", ticket=number, db_ticket=db["ticket"], files=derived["files"],
                  added=sorted(set(derived["files"]) - set(old)), removed=sorted(set(old) - set(derived["files"])),
                  commits=derived["commits"], other_commits=derived["other_commits"],
                  unconventional_commits=derived["unconventional_commits"],
                  message=f"{len(derived['files'])} DB file(s) belong to this ticket.{note}")
