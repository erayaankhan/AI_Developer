#!/usr/bin/env python3
"""
dev-ticket helper - the deterministic half of the dev-ticket workflow.

Claude Code runs these subcommands from the terminal; every subcommand ends with a
single machine-readable line

    RESULT: {"status": "...", ...}

so the workflow reads one JSON object instead of scraping log text. Exit code is 0
for any *decided* outcome (including "stop and ask" outcomes such as MULTIPLE) and
non-zero only for errors (bad credentials, network, git failure).

No Jenkins, no MCP server: Assembla is called directly (assembla_api.py) and the
feature branch is created with plain git.

Subcommands are listed below; `<subcommand> -h` shows the options of one.

Config: dev_ticket.config.json next to this script, overridden per developer by dev_ticket.config.local.json.
"""

import argparse
import subprocess
import sys
import urllib.parse as urllib_parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from assembla_api import PLAN_LEVELS, AssemblaApi, AssemblaError, bare_number  # noqa: E402
import metrics  # noqa: E402
from state_io import write_state  # noqa: E402
from develop_ticket import common, ticket_fetch  # noqa: E402,F401
from develop_ticket.common import (  # noqa: E402,F401
    BASE,
    CONFIG,
    DB_REPO,
    PROJECT_ROOT,
    RELATED,
    SCRIPT_DIR,
    STATUS,
    TIER_ORDER,
    _remembered_role,
    _role_env,
    _write_state,
    branch_name,
    git,
    inside_ticket_folder,
    is_db_companion,
    is_db_node,
    is_local_only_path,
    load_state,
    master_sync_info,
    move_db_companion,
    now_iso,
    push_state,
    record_metric,
    result,
    start_dirty_files,
    ticket_dir,
    ticket_row,
    uncommitted_tracked)
from develop_ticket.ticket_fetch import (  # noqa: E402,F401
    IMAGE_EXTS,
    MAX_IMAGE_BYTES,
    READINESS_PATTERNS,
    VIDEO_EXTS,
    _is_my_review,
    attachment_kind,
    cmd_fetch,
    cmd_takeover,
    deploy_attribute_problems,
    download_images,
    infer_type,
    readiness_hints,
    render_ticket_md,
    resolve_links,
    work_dir_for)
from develop_ticket.comments import (  # noqa: E402,F401
    SUMMARY_SECTIONS,
    needs_rca_clca,
    post_or_edit_comment,
    summary_problems)
from develop_ticket.protocol import (  # noqa: E402,F401
    HANDOFF_BEGIN,
    HANDOFF_END,
    HANDOFF_MAX_FILES,
    REVIEW_SEVERITIES,
    VERDICT_BEGIN,
    VERDICT_END,
    VERDICT_MAX_FINDINGS,
    VERDICT_TEXT_MAX,
    _brief_section,
    _verdict_text_errors,
    handoff_payload,
    latest_handoff,
    latest_verdict,
    parse_handoff,
    parse_verdict,
    render_handoff,
    render_verdict,
    verdict_payload,
    verdict_problems)
from develop_ticket.quality_cmds import cmd_quality, cmd_quality_bounce  # noqa: E402,F401
from develop_ticket.author import (  # noqa: E402,F401
    cmd_assign,
    cmd_handoff,
    cmd_rework,
    cmd_teams_notify)
from develop_ticket.review import (  # noqa: E402,F401
    _is_doc_path,
    _is_fetch_stub,
    _own_code_diff,
    _review_checks,
    _review_row,
    _review_verdict_act,
    _reviewer_problem,
    _save_handoff_files,
    _wiki_safe,
    ai_review_summary,
    checkout_remote_branch,
    cmd_review_compare,
    cmd_review_deploy,
    cmd_review_fetch,
    cmd_review_pick,
    cmd_review_verdict,
    cmd_review_withdraw,
    compare_handoff,
    dirty_files_in,
    docs_only_delta,
    fetch_origin_branch)
from develop_ticket.state_status import (  # noqa: E402,F401
    STATE_FIELDS,
    _set_nested,
    cmd_state,
    cmd_status,
    low_path_problems,
    status_guard)
from develop_ticket.guards import (  # noqa: E402,F401
    developer_problem,
    my_in_process)
from develop_ticket.pause import (  # noqa: E402,F401
    EMBEDDED,
    cmd_handover,
    cmd_restore)
from develop_ticket.rca import (  # noqa: E402,F401
    SWEEP_LINE_MARKER,
    cmd_rca,
    rca_clca_fields,
    rca_clca_missing,
    rca_clca_values,
    sweep_check,
    sweep_closing_line)
from develop_ticket.functional_cmds import (  # noqa: E402,F401
    _local_env,
    _port_open,
    _profile_name,
    cmd_env_apply,
    cmd_env_restore,
    cmd_env_check,
    cmd_func_mode,
    cmd_func_prepare,
    cmd_func_record,
    cmd_host_check,
    cmd_profile,
    cmd_service_start,
    load_env_path)
from develop_ticket.db import (  # noqa: E402,F401
    cmd_db_branch,
    cmd_db_create,
    cmd_db_files,
    cmd_db_ticket,
    cmd_followup_create,
    find_db_ticket)
from develop_ticket.git_sync import (  # noqa: E402,F401
    cmd_merge_master,
    cmd_pushed,
    cmd_sync_branch,
    cmd_sync_check,
    predict_conflicts,
    pull_merge,
    unmerged_files)

from develop_ticket.gates import (  # noqa: E402,F401
    PLAN_PATH_RE,
    cmd_baseline,
    cmd_deps,
    cmd_gate_round,
    latest_gates_dir,
    planned_files)
from develop_ticket.review_aid import (  # noqa: E402,F401
    REVIEWER_INPUTS_OVERRIDE,
    _db_check_lines,
    cmd_review_brief,
    cmd_review_check)
from develop_ticket.epics import (  # noqa: E402,F401
    STOPWORDS,
    _area,
    _tokens,
    active_tickets_cached,
    active_wf_epics,
    cmd_epics,
    rank_epics,
    sibling_epic_counts)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)


def cmd_event(_api, args) -> int:
    """Record an event only Claude can know about (stop, review, ui_checks, fallback, fix_round, questions)."""
    number = bare_number(args.ticket)
    fields, error = metrics.parse_claude_event(args.type, args.fields or [])
    if error:
        return result("BAD_EVENT", 1, ticket=number, message=error)
    if not metrics.record(common.PROJECT_ROOT, CONFIG["tickets_dir"], number, args.type, **fields):
        return result("NO_WORK_FOLDER", 1, ticket=number, message="Run fetch first.")
    return result("RECORDED", ticket=number, event=args.type, fields=fields)


def _listed_role(login) -> str:
    """The role the config lists give an Assembla login: 'reviewer' | 'developer' | 'both' (a config error) | None."""
    def names(key):
        return {str(n).strip().lower() for n in CONFIG.get(key) or [] if str(n).strip()}
    login = str(login or "").strip().lower()
    reviewer, developer = login in names("reviewers"), login in names("developers")
    return "both" if reviewer and developer else "reviewer" if reviewer else "developer" if developer else None


def cmd_role(api, args) -> int:
    """The role (developer or reviewer) of whoever runs the workflow. **The config lists decide it**: a login on
    `reviewers` is a reviewer, one on `developers` a developer, and the answer is kept in the local env file (never
    opened by Claude: use this). Someone on neither list is asked once (`--set`); a reviewer must be on `reviewers`."""
    import functional
    path, key, allowed = _role_env()
    login = api.get_current_user().get("login")
    listed = _listed_role(login)
    if listed == "both":
        return result("ROLE_CONFLICT", 1, login=login,
                      message="This login is on both `reviewers` and `developers`: the lead fixes dev_ticket.config.json.")
    cached = functional.read_role(path, key, allowed)
    if not args.set:
        if listed:
            if cached != listed:  # the lists win over whatever was remembered
                functional.set_env_key(path, key, listed)
            return result("ROLE", role=listed, source="lists", login=login, updated=cached != listed)
        if cached == "developer":
            return result("ROLE", role=cached, source="remembered", login=login, updated=False)
        return result("ROLE_UNSET", role=None, login=login,
                      message="Not on `reviewers` or `developers`. Ask once: are you a " + " or a ".join(allowed) +
                              "? (a reviewer must be on `reviewers`: the lead adds them.) Then: dt role --set <role> --yes")
    role = args.set.strip().lower()
    if role not in allowed:
        return result("BAD_ROLE", 1, role=role, allowed=allowed)
    if listed and role != listed:
        return result("ROLE_FIXED_BY_LIST", 1, login=login, listed_as=listed, requested=role,
                      message=f"This login is listed as {listed}: the role comes from dev_ticket.config.json, not from --set.")
    if not listed and role == "reviewer":
        return result("NOT_ON_REVIEWERS", 1, login=login,
                      message="A reviewer must be on `reviewers` in dev_ticket.config.json: ask the lead to add this login.")
    if not args.yes:
        return result("PREVIEW", role=role, message="Re-run with --yes to remember it.")
    functional.set_env_key(path, key, role)
    return result("ROLE_SET", role=role)


# ---------------------------------------------------------------------- whoami / pick

def cmd_creds(_api, _args) -> int:
    """Where each credential comes from (file / env / missing) - never the values."""
    import credentials
    for key, source in credentials.SOURCES.items():
        print(f"  {key:<20} {source}")
    required = ("ASSEMBLA_API_KEY", "ASSEMBLA_API_SECRET")
    status = "OK" if all(credentials.SOURCES[k] != "missing" for k in required) else "MISSING"
    return result(status, file=str(credentials.credentials_path()),
                  file_exists=credentials.credentials_path().is_file(), sources=credentials.SOURCES)


def cmd_config_check(_api, _args) -> int:
    """Read-only: which team profile and code repo this run uses, and whether the repos it needs are where the config
    says. Never calls Assembla. `CONFIG_PROBLEM` lists what to fix; `OK` also tells Claude which profile notes to read."""
    import json as _json
    problems, repos = [], {}
    root = common.PROJECT_ROOT
    profile = CONFIG.get("profile") or ""
    if not (root / ".git").exists():
        problems.append(f"project root {root} is not a git repo")
    if profile and (CONFIG.get("repos") or {}) and not CONFIG.get("repo_key"):
        problems.append(f"'{root.name}' is not one of the profile's code repos ({', '.join(CONFIG['repos'])}): "
                        "open Claude in one of them")
    for key, repo in (CONFIG.get("repos") or {}).items():
        path = (common.SCRIPT_DIR / repo["folder"]).resolve() if repo.get("folder") else None
        repos[key] = {"path": str(path) if path else None, "exists": bool(path and (path / ".git").exists()),
                      "node": (repo.get("config") or {}).get("repo_node"), "current": key == CONFIG.get("repo_key")}
        if path and not repos[key]["exists"]:
            problems.append(f"repo '{key}' not found at {path} (set repos.{key}.folder in {LOCAL_CONFIG_HINT})")
    db_repo = common.DB_REPO
    db_on = (CONFIG.get("db") or {}).get("enabled", True)  # a team without a DB repo sets db.enabled false
    db_ok = db_on and (db_repo / ".git").exists()
    if profile and db_on and not db_ok:
        problems.append(f"DB repo not found at {db_repo} (db.repo_path)")
    notes = (common.SCRIPT_DIR.parent / "skills" / "dev-ticket" / f"PROFILE_{profile.upper()}.md") if profile else None
    repo_key = CONFIG.get("repo_key")  # a profile serving several repos keeps each repo's own notes next to its own
    repo_notes = (notes.with_name(f"PROFILE_{profile.upper()}_{repo_key.upper()}.md") if notes and repo_key else None)
    info = {"profile": profile or None, "repo_key": CONFIG.get("repo_key") or None, "project_root": str(root),
            "repo_node": CONFIG.get("repo_node"), "assembla_space": CONFIG.get("assembla_space"),
            "current_milestones": CONFIG.get("current_milestones"),
            "db_repo": str(db_repo) if db_on else None, "db_repo_ok": db_ok,
            "repos": repos, "staging_deploy": (CONFIG.get("staging_deploy") or {}).get("enabled", True),
            "profile_notes": str(notes) if notes and notes.is_file() else None,
            "repo_notes": str(repo_notes) if repo_notes and repo_notes.is_file() else None,
            "additional_directories": sorted({r["path"] for r in repos.values() if r["path"] and not r["current"]}
                                             | ({str(db_repo)} if db_ok else set()))}
    print(_json.dumps(info, indent=2, ensure_ascii=False))
    return result("CONFIG_PROBLEM" if problems else "OK", problems=problems, profile=info["profile"],
                  repo_key=info["repo_key"], profile_notes=info["profile_notes"], repo_notes=info["repo_notes"])


LOCAL_CONFIG_HINT = "python_scripts/dev_ticket.config.local.json"


def cmd_whoami(api: AssemblaApi, _args) -> int:
    me = api.get_current_user()
    print(f"You are {me.get('login')} ({me.get('name')}), id={me.get('id')}, space={api.space}")
    return result("OK", id=me.get("id"), login=me.get("login"), name=me.get("name"))


def cmd_pick(api: AssemblaApi, args) -> int:
    me = api.get_current_user()
    me_id, me_login = me["id"], me.get("login")

    current = api.get_current_milestones()
    if not current:
        return result("NO_CURRENT_MILESTONE", developer=me_login,
                      message="No milestone has '_CURRENT' in its title - development can't start "
                              "until the release milestone is marked _CURRENT.")
    current_ids = {m["id"] for m in current}
    current_titles = [m.get("title") for m in current]

    in_process, queue, elsewhere, source = my_in_process(api, me_id, current_ids)
    print(f"Developer: {me_login} | source: {source} | current milestone(s): {current_titles} | "
          f"'{STATUS['in_process']}' tickets: {[t.get('number') for t in in_process]}"
          + (f" | outside _CURRENT: {[t.get('number') for t in elsewhere]}" if elsewhere else ""))
    elsewhere_rows = [ticket_row(api, t) for t in elsewhere]

    if args.ticket:
        try:
            t = api.get_ticket(args.ticket)
        except AssemblaError as exc:
            return result("NOT_FOUND", ticket=bare_number(args.ticket), message=str(exc))
        row = ticket_row(api, t)
        others = [ticket_row(api, o) for o in in_process if o.get("number") != t.get("number")]
        if t.get("assigned_to_id") != me_id:
            return result("NOT_ASSIGNED", developer=me_login, **row,
                          message=f"Ticket is assigned to {row['assignee']}, not {me_login}.")
        wrong_repo = common.repo_node_problem(t)
        if wrong_repo:
            return result(wrong_repo[0], developer=me_login, **row, message=wrong_repo[1])
        from_queue = bool(CONFIG.get("pick_from_queue")) and t.get("status") == STATUS["queue"]
        if from_queue and others:  # a team that starts tickets from the queue: one ticket in progress per repo
            return result("OTHER_IN_PROCESS", developer=me_login, others_in_process=others, **row,
                          message=f"#{others[0]['ticket']} is already in '{STATUS['in_process']}'. Pause it first "
                                  f"(/dev-ticket pause {others[0]['ticket']}) or finish it, then start #{row['ticket']}.")
        if t.get("status") != STATUS["in_process"] and not from_queue:
            return result("WRONG_STATUS", developer=me_login, **row,
                          message=f"Ticket is '{t.get('status')}', expected '{STATUS['in_process']}'."
                                  + (f" It is a review for you: use /review-and-deploy {row['ticket']}."
                                     if _is_my_review(api, t) else ""))
        if t.get("milestone_id") not in current_ids:
            milestone = api.get_milestone(t.get("milestone_id")).get("title") or "(none)"
            return result("WRONG_MILESTONE", developer=me_login, milestone=milestone,
                          current_milestones=current_titles, **row,
                          message=f"Ticket is in milestone '{milestone}', not a _CURRENT milestone.")
        return result("PICKED", developer=me_login, others_in_process=others, **row,
                      **(_start_fields() if from_queue else {}))

    if not in_process and CONFIG.get("pick_from_queue") and queue:
        rows = [ticket_row(api, t) for t in sorted(queue, key=lambda x: x.get("number") or 0)]
        if len(rows) == 1:
            return result("PICKED", developer=me_login, others_in_process=[], **rows[0], **_start_fields())
        return result("CHOOSE_FROM_QUEUE", developer=me_login, candidates=rows, current_milestones=current_titles,
                      message=f"No ticket is in '{STATUS['in_process']}' yet; {len(rows)} are in '{STATUS['queue']}'. "
                              "Which one do you start? Then: dt pick --ticket N.")
    if not in_process:
        return result("NONE", developer=me_login, current_milestones=current_titles,
                      dev_queue=[ticket_row(api, t) for t in queue],
                      in_process_outside_current=elsewhere_rows,
                      message=f"No '{STATUS['in_process']}' ticket assigned to {me_login} "
                              f"in the _CURRENT milestone.")
    if len(in_process) > 1:
        return result("MULTIPLE", developer=me_login,
                      candidates=[ticket_row(api, t) for t in in_process],
                      message=f"{len(in_process)} tickets in '{STATUS['in_process']}' - only one is allowed.")
    return result("PICKED", developer=me_login, others_in_process=[], **ticket_row(api, in_process[0]))


def _start_fields() -> dict:
    """pick_from_queue: the picked ticket is still in the queue; it moves to in-process when work starts."""
    return {"start_needed": True, "start_move": f"{STATUS['queue']} -> {STATUS['in_process']}",
            "start_note": f"Moved to '{STATUS['in_process']}' right after the branch exists (step 3), on the developer's "
                          f"yes: dt status N --to \"{STATUS['in_process']}\"."}


def cmd_branch(api: AssemblaApi, args) -> int:
    number = bare_number(args.ticket)
    branch = branch_name(number)

    # D-12: staging attributes are a hard gate before any branch work.
    ticket = api.get_ticket(number)
    problems = deploy_attribute_problems(ticket, api.get_tags(number), resolve_links(api, ticket))
    if problems:
        return result("ATTRIBUTES_MISSING", ticket=number, branch=branch, problems=problems,
                      message="Fix these ticket attributes in Assembla before the branch is created.")
    try:
        current = common.git("branch", "--show-current").stdout.strip()
        state, state_path = common.load_state(number)
        stages = CONFIG["stages"]
        stage = state.get("stage") or stages[0]
        # A ticket starts from a CLEAN tree, whether this switches branches or not: tracked changes and untracked
        # files alike must be committed (elsewhere), stashed (git stash -u) or deleted by the developer first.
        # Only a fresh start is checked; a resumed ticket past step 3 has its own work in the tree.
        fresh_start = (not state.get("start_head") and stage in stages
                       and stages.index(stage) <= stages.index("3-branch"))  # a paused ticket is a resume, not a start
        if fresh_start:
            unclean = common.start_dirty_files()
            if unclean:
                tracked = [l for l in unclean if not l.startswith("??")]
                return result("DIRTY", ticket=number, branch=branch, current_branch=current,
                              tracked=len(tracked), untracked=len(unclean) - len(tracked), total=len(unclean),
                              files=unclean[:50],
                              message="The working tree must be clean before the ticket starts. The developer commits "
                                      "(on another branch), stashes (git stash -u) or deletes these files, then "
                                      "re-run. Never do it for them.")
        # Local-only files (D-27, e.g. environment.ts) are always modified on a developer's PC and
        # are carried across the switch by git; they never count as "dirty".
        dirty = [l for l in common.git("status", "--porcelain", "--untracked-files=no").stdout.splitlines()
                 if l.strip() and not is_local_only_path(l[3:].strip())]
        if dirty and current != branch:
            return result("DIRTY", ticket=number, branch=branch, current_branch=current, files=dirty[:50],
                          message="Tracked files have uncommitted changes - stash/commit them first.")

        common.git("fetch", "--quiet", "origin", BASE)
        remote_exists = bool(common.git("ls-remote", "--heads", "origin", branch).stdout.strip())
        local_exists = common.git("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False).returncode == 0

        if current == branch:
            action = "ALREADY_ON_BRANCH"
        elif local_exists:
            common.git("switch", branch)
            action = "CHECKED_OUT"
        elif remote_exists:
            common.git("fetch", "--quiet", "origin", f"refs/heads/{branch}:refs/remotes/origin/{branch}")
            common.git("switch", "--track", f"origin/{branch}")
            action = "CHECKED_OUT_REMOTE"
        else:
            common.git("switch", "--no-track", "-c", branch, f"origin/{BASE}")
            action = "CREATED_LOCAL"

        pushed = remote_exists
        if not remote_exists and args.push:
            common.git("push", "-u", "origin", branch, timeout=300)
            pushed = True
            action = "CREATED_PUSHED" if action == "CREATED_LOCAL" else action

        ahead, behind, conflicts = common.master_sync_info()
        if fresh_start and state_path is not None:  # remember the clean start the ticket began from
            state["start_head"] = common.git("rev-parse", "--short", "HEAD").stdout.strip()
            # A branch created from master must pass the baseline tests (dt baseline) before the ticket moves on.
            state["branch_origin"] = "created" if action.startswith("CREATED") else "existing"
            write_state(state_path, state)
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        return result("FAILED", 1, ticket=number, branch=branch, message=str(exc))

    print(f"{branch}: {action} | pushed={pushed} | ahead={ahead} behind={behind} | conflicts={len(conflicts)}")
    return result(action, ticket=number, branch=branch, pushed=pushed, ahead=ahead,
                  behind=behind, merge_conflicts=conflicts,
                  baseline_required=bool(fresh_start and action.startswith("CREATED")))



def cmd_fix_attributes(api: AssemblaApi, args) -> int:
    """D-13: the workflow fixes the staging attributes itself (preview unless --yes)."""
    number = bare_number(args.ticket)
    ticket = api.get_ticket(number)
    tags = api.get_tags(number)
    links = resolve_links(api, ticket)
    attrs = CONFIG["deploy_attributes"]
    inactive = set(CONFIG["epics"]["inactive_statuses"])

    fields, fixes = {}, []
    if ticket.get("hierarchy_type") != attrs["plan_level"]:
        fields["hierarchy_type"] = attrs["plan_level"]
        fixes.append(f"Plan level -> {PLAN_LEVELS[attrs['plan_level']]}")
    if ((ticket.get("custom_fields") or {}).get(attrs["node_field"]) or "").strip() != attrs["node_value"]:
        fields["custom_fields"] = {attrs["node_field"]: attrs["node_value"]}
        fixes.append(f"{attrs['node_field']} -> {attrs['node_value']}")
    if attrs["tag"] and attrs["tag"] not in tags:  # a team without a deploy tag sets tag ""
        fields["tags"] = sorted(set(tags) | {attrs["tag"]})
        fixes.append(f"+ tag {attrs['tag']}")

    epic_links = [l for l in links if l.get("plan_level") == "Epic"]
    needs_epic = attrs["needs_epic_parent"] and not any(l["status"] not in inactive for l in epic_links)
    new_epic, unlink = None, []
    if needs_epic:
        if not args.epic:
            return result("NEEDS_EPIC", ticket=number, planned_fixes=fixes,
                          inactive_epics=[f"#{l['ticket']} ({l['status']})" for l in epic_links],
                          message=f"Pick an Epic first: dev_ticket.py epics {number}")
        new_epic = api.get_ticket(args.epic)
        epic_node = ((new_epic.get("custom_fields") or {}).get(attrs["node_field"]) or "").strip()
        if new_epic.get("hierarchy_type") != 3 or new_epic.get("status") in inactive or epic_node != CONFIG["epics"]["node_value"]:
            return result("BAD_EPIC", 1, ticket=number, epic=new_epic.get("number"),
                          message=f"#{new_epic.get('number')} is not an active Epic with "
                                  f"{attrs['node_field']}={CONFIG['epics']['node_value']}.")
        unlink = [l for l in epic_links if l["status"] in inactive]
        fixes += [f"unlink inactive Epic #{l['ticket']} ({l['status']})" for l in unlink]
        fixes.append(f"parent Epic -> #{new_epic.get('number')} {new_epic.get('summary')}")

    if not fixes:
        return result("NOTHING_TO_FIX", ticket=number)
    if not args.yes:
        for f in fixes:
            print(f"  would fix: {f}")
        return result("PREVIEW", ticket=number, planned_fixes=fixes, message="Re-run with --yes to apply.")

    # Parent first, then fields - same order as create_feature_branch_v1.py.
    for l in unlink:
        api.delete_association(number, l["association_id"])
    if new_epic:
        api.add_parent(number, ticket["id"], new_epic["id"])
    if fields:
        api.update_ticket(number, fields)

    # Verify from a fresh read - never trust the write responses alone.
    ticket = api.get_ticket(number)
    remaining = deploy_attribute_problems(ticket, api.get_tags(number), resolve_links(api, ticket))
    if remaining:
        return result("FAILED", 1, ticket=number, applied=fixes, remaining=remaining)
    api.add_comment(number, "Auto-fixed by dev-ticket before branch creation: " + "; ".join(fixes))
    record_metric(number, "attr_fix", fixes=fixes)
    return result("FIXED", ticket=number, applied=fixes)


def cmd_comment(api: AssemblaApi, args) -> int:
    number = bare_number(args.ticket)
    if not inside_ticket_folder(number, args.file):
        return result("FILE_OUTSIDE_TICKET_FOLDER", 1, ticket=number, file=args.file,
                      message=f"Only files inside {ticket_dir(number)} can be posted as a comment.")
    body = Path(args.file).read_text(encoding="utf-8").strip()
    if not body:
        return result("EMPTY", 1, ticket=number, message=f"{args.file} is empty.")
    if Path(args.file).name == "SUMMARY.md":  # the hand-off summary must follow SUMMARY_FORMAT.md (D-75)
        state = common.load_state(number)[0] or {}
        defect = needs_rca_clca(api.get_ticket(number), state)
        errors, warnings = summary_problems(body, defect, state.get("functional_mode") == "skip")
        if errors:
            return result("SUMMARY_CHECK_FAILED", 1, ticket=number, errors=errors, warnings=warnings,
                          message="Fix SUMMARY.md (see SUMMARY_FORMAT.md), then post it.")
    if not args.yes:
        print("---- comment preview (not posted) ----\n" + body + "\n--------------------------------------")
        return result("PREVIEW", ticket=number, chars=len(body),
                      confirm=common.confirm_token("comment", {"ticket": number, "body": body, "edit": args.edit_id}),
                      message="Re-run with --yes --confirm <confirm> to post.")
    refused = common.confirm_refusal("comment", number, args, {"ticket": number, "body": body, "edit": args.edit_id})
    if refused is not None:
        return refused
    status, fields = post_or_edit_comment(api, number, body, args.edit_id)
    return result(status, 1 if status in ("COMMENT_NOT_FOUND", "NOT_YOUR_COMMENT") else 0, ticket=number, **fields)


def cmd_summary_check(api: AssemblaApi, args) -> int:
    number = bare_number(args.ticket)
    file_arg = args.file or str(ticket_dir(number) / "SUMMARY.md")
    if not inside_ticket_folder(number, file_arg):
        return result("FILE_OUTSIDE_TICKET_FOLDER", 1, ticket=number, file=file_arg)
    if not Path(file_arg).is_file():
        return result("NOT_FOUND", 1, ticket=number, file=file_arg, message="Write SUMMARY.md first (SUMMARY_FORMAT.md).")
    state = common.load_state(number)[0] or {}
    defect = needs_rca_clca(api.get_ticket(number), state)
    errors, warnings = summary_problems(Path(file_arg).read_text(encoding="utf-8"), defect,
                                        state.get("functional_mode") == "skip")
    return result("SUMMARY_CHECK_FAILED" if errors else "OK", 1 if errors else 0, ticket=number, defect=defect,
                  errors=errors, warnings=warnings)


# ---------------------------------------------------------------------- main

COMMAND_HELP = {
    "whoami": "Developer identity from their own API key.",
    "creds": "Where each credential comes from (file / env / missing); never the values.",
    "config-check": "Which team profile, code repo, DB repo and milestones this run uses (read-only, no Assembla).",
    "role": "The role of whoever runs the workflow (developer or reviewer); --set remembers it (preview unless --yes).",
    "pick": "Find the developer's single Dev In Process ticket.",
    "fetch": "Save the ticket, comments, links and readiness hints to ticket.md.",
    "branch": "Check the staging attributes, then create or check out FB_N from origin/master (no commits).",
    "epics": "Active Node=WF epics from the whole space, ranked by closeness to the ticket.",
    "fix-attributes": "Fix Plan level / Node / WF tag / Epic parent (preview unless --yes).",
    "host-check": "UI-check pre-flight: host project on its dev branch, host and remote ports serving.",
    "profile": "Which environment the functional checks run against, decided from the changed files.",
    "env-check": "Is the chosen profile usable? Key names and booleans only, never a value.",
    "env-apply": "Write the profile's URLs into every environment.ts and its appsettings overlays into the services' appsettings.json (snapshotted first, never staged).",
    "env-restore": "Put every file patched by env-apply back from its snapshot (func-record, hand-off and review-compare do it automatically).",
    "service-start": "Start one local service detached with the test DB connection (local profile only).",
    "func-mode": "The developer's choice for the functional checks: run, skip or later.",
    "func-prepare": "Write FUNCTIONAL_CHECKS.md from PLAN.md for the developer to run in Claude Desktop.",
    "func-record": "Record the functional-check run (or a developer-confirmed 'no UI path'); a PASS also puts the patched run files back.",
    "followup-create": "Create one follow-up ticket (a CLCA-sweep hit or a review finding) linked Related (preview unless --yes).",
    "review-brief": "Write the filled-in REVIEW.md for the review sub-agent (--reviewer adds the reviewer's override).",
    "review-check": "Mechanical evidence check of the independent code review.",
    "baseline": "Run the unit tests of the planned projects on the untouched start commit.",
    "deps": "Fresh dependencies for the Angular app(s) the ticket changes.",
    "gate-round": "Start the next automated unit-test fix round (max 3) and write gates/round-K.md.",
    "event": "Record a quality event only Claude can know about, in metrics.jsonl.",
    "state": "Read or change state.json through validated keys.",
    "handover": "Pause: HANDOVER.md, attach and comment, Dev In Process to Dev Queue (preview unless --yes).",
    "restore": "Resume on any PC: rebuild DEV_TICKETS/N from the latest HANDOVER.",
    "db-ticket": "The shared C2M-DB ticket of the ticket's current milestone.",
    "db-create": "Create the DB ticket for a WF ticket and link it Related (preview unless --yes).",
    "db-files": "Set state.db.files from what the DB repo says belongs to this ticket.",
    "db-branch": "Check out FB_D in the DB repo, creating it from master if missing; never commits.",
    "sync-check": "Is the branch behind master, and would merging it conflict? (git only)",
    "sync-branch": "Pull origin/FB_N, push, pull origin/master, push (preview unless --yes).",
    "merge-master": "Pull origin/master into FB_N (preview unless --yes); aborts cleanly on a conflict.",
    "pushed": "Is everything committed and HEAD on origin? (--db checks the DB ticket's branch)",
    "summary-check": "Check SUMMARY.md against the required sections before the hand-off.",
    "rca": "Show or write the RCA and CLCA fields of a main ticket.",
    "handoff": "Developer hand-off: build HANDOFF.md and post it as one comment (preview unless --yes).",
    "assign": "Developer hand-off, last step: list the reviewers or assign the Code Review ticket to one.",
    "teams-notify": "Post (or with --again re-post) the Teams review request for the assigned reviewer.",
    "quality": "Last step before hand-off: Sonar (own dev project) and Snyk on the apps this ticket changed.",
    "quality-bounce": "Build Failed tickets that failed on Sonar / Snyk: list them, and with --yes send them back to Dev Queue.",
    "rework": "Did the reviewer send the ticket back? With --yes save the findings and reopen the build step.",
    "review-pick": "Reviewer R1 (read-only): my Code Review tickets that carry a HANDOFF.",
    "review-fetch": "Reviewer R2: save the ticket and hand-off and check out the developer's branch read-only.",
    "review-compare": "Reviewer R3: compare the HANDOFF with the reviewer's own gates, risk check and functional checks.",
    "review-verdict": "Reviewer R5: post the REVIEW_VERDICT, then reject or approve (preview unless --yes).",
    "review-deploy": "Reviewer R6: Dev Done to DevOps Staging while origin still equals the approved heads.",
    "review-withdraw": "Reviewer: withdraw an approval before any deploy (preview unless --yes).",
    "takeover": "Fix a ticket yourself that has a reviewer's review folder: move that folder aside (preview unless --yes), then fetch.",
    "comment": "Post a comment (preview unless --yes).",
    "status": "Move the status along an allowed transition (preview unless --yes).",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True, metavar="subcommand")
    sub.add_parser("whoami", help=COMMAND_HELP["whoami"], description=COMMAND_HELP["whoami"])
    sub.add_parser("creds", help=COMMAND_HELP["creds"], description=COMMAND_HELP["creds"])
    sub.add_parser("config-check", help=COMMAND_HELP["config-check"], description=COMMAND_HELP["config-check"])
    p = sub.add_parser("role", help=COMMAND_HELP["role"], description=COMMAND_HELP["role"])
    p.add_argument("--set", help="Remember this role (developer|reviewer); previews unless --yes.")
    p.add_argument("--yes", action="store_true")
    p = sub.add_parser("pick", help=COMMAND_HELP["pick"], description=COMMAND_HELP["pick"])
    p.add_argument("--ticket")
    p = sub.add_parser("fetch", help=COMMAND_HELP["fetch"], description=COMMAND_HELP["fetch"])
    p.add_argument("ticket")
    p.add_argument("--type", choices=CONFIG["ticket_types"])
    p = sub.add_parser("takeover", help=COMMAND_HELP["takeover"], description=COMMAND_HELP["takeover"])
    p.add_argument("ticket")
    p.add_argument("--yes", action="store_true")
    p = sub.add_parser("branch", help=COMMAND_HELP["branch"], description=COMMAND_HELP["branch"])
    p.add_argument("ticket")
    p.add_argument("--push", action="store_true", help="Push a newly created branch to origin.")
    sub.add_parser("host-check", help=COMMAND_HELP["host-check"], description=COMMAND_HELP["host-check"])
    p = sub.add_parser("profile", help=COMMAND_HELP["profile"], description=COMMAND_HELP["profile"])
    p.add_argument("ticket")
    p.add_argument("--force", choices=["cloud_staging", "cloud_preprod", "local"],
                   help="Only on the developer's say-so (e.g. staging is down for an Angular-only ticket).")
    p.add_argument("--choose", choices=["cloud_staging", "cloud_preprod"],
                   help="The developer's pick for an Angular-only ticket (the answer to `choose_between`).")
    for name in ("env-check", "env-apply"):
        p = sub.add_parser(name, help=COMMAND_HELP[name], description=COMMAND_HELP[name])
        p.add_argument("ticket")
        p.add_argument("--profile", choices=["cloud_staging", "cloud_preprod", "local"])
    p = sub.add_parser("env-restore", help=COMMAND_HELP["env-restore"], description=COMMAND_HELP["env-restore"])
    p.add_argument("ticket")
    p = sub.add_parser("service-start", help=COMMAND_HELP["service-start"], description=COMMAND_HELP["service-start"])
    p.add_argument("ticket")
    p.add_argument("service")
    p = sub.add_parser("func-record", help=COMMAND_HELP["func-record"], description=COMMAND_HELP["func-record"])
    p.add_argument("ticket")
    p.add_argument("--passed", type=int, default=0)
    p.add_argument("--failed", type=int, default=0)
    p.add_argument("--from", dest="from_file", help="Results file from the Claude Desktop run (reads its `passed X failed Y` line).")
    p = sub.add_parser("func-prepare", help=COMMAND_HELP["func-prepare"], description=COMMAND_HELP["func-prepare"])
    p.add_argument("ticket")
    p = sub.add_parser("followup-create", help=COMMAND_HELP["followup-create"], description=COMMAND_HELP["followup-create"])
    p.add_argument("ticket")
    p.add_argument("--title", required=True)
    p.add_argument("--description-file", required=True, help="File inside the ticket folder with the description.")
    p.add_argument("--category", help="Category custom field (default: the main ticket's).")
    p.add_argument("--allow-similar", action="store_true",
                   help="Create it although an open Related ticket has a near-identical title (the developer said it is a different one).")
    p.add_argument("--hit", help="The CLCA-sweep bullet this ticket raises (part of its `path:line`): it gets `ticket: #N` in BRIEF.md.")
    p.add_argument("--yes", action="store_true", help="Create it (without this it only previews).")
    p.add_argument("--confirm", metavar="HASH", help="with --yes: the `confirm` hash the preview returned")
    p = sub.add_parser("review-brief", help=COMMAND_HELP["review-brief"], description=COMMAND_HELP["review-brief"])
    p.add_argument("ticket")
    p.add_argument("--round", type=int, default=1, help="Review round (round 2+ writes REVIEW_BRIEF-roundN.md).")
    p.add_argument("--reviewer", action="store_true", help="Append the reviewer's Inputs override (no author BRIEF/PLAN).")
    p = sub.add_parser("func-mode", help=COMMAND_HELP["func-mode"], description=COMMAND_HELP["func-mode"])
    p.add_argument("ticket")
    p.add_argument("mode", choices=["run", "skip", "later"])
    p.add_argument("--reason", help="Required for skip (e.g. 'no screen reaches the change').")
    p.add_argument("--confirmed", action="store_true", help="The developer said yes to the skip.")
    p = sub.add_parser("review-check", help=COMMAND_HELP["review-check"], description=COMMAND_HELP["review-check"])
    p.add_argument("ticket")
    p.add_argument("--file", help="Review file (default: reviews/code-review.md in the ticket folder).")
    p = sub.add_parser("baseline", help=COMMAND_HELP["baseline"], description=COMMAND_HELP["baseline"])
    p.add_argument("ticket")
    p.add_argument("--files", nargs="+", help="Planned source files (default: the backticked paths in PLAN.md).")
    p = sub.add_parser("deps", help=COMMAND_HELP["deps"], description=COMMAND_HELP["deps"])
    p.add_argument("ticket")
    p.add_argument("--files", nargs="+", help="Planned source files (default: the backticked paths in PLAN.md).")
    p.add_argument("--project", help="Force one app (c2mwf_admin, C2MWFAng or c2mwf_runtime).")
    p.add_argument("--legacy-peer-deps", action="store_true",
                   help="Only after a DEPS_CONFLICT and the developer's yes: installs node_modules only.")
    p.add_argument("--dry-run", action="store_true", help="npm install --dry-run: install nothing, keep the lockfile.")
    p = sub.add_parser("gate-round", help=COMMAND_HELP["gate-round"], description=COMMAND_HELP["gate-round"])
    p.add_argument("ticket")
    p.add_argument("--no-verify", action="store_true",
                   help="Skip the start-commit check (which failing tests already fail before the ticket).")
    p = sub.add_parser("event", help=COMMAND_HELP["event"], description=COMMAND_HELP["event"])
    p.add_argument("ticket")
    p.add_argument("type", choices=sorted(metrics.EVENT_FIELDS))
    p.add_argument("fields", nargs="*", metavar="KEY=VALUE")
    p = sub.add_parser("state", help=COMMAND_HELP["state"], description=COMMAND_HELP["state"])
    p.add_argument("ticket")
    p.add_argument("--set", nargs="+", metavar="KEY=VALUE")
    p = sub.add_parser("handover", help=COMMAND_HELP["handover"], description=COMMAND_HELP["handover"])
    p.add_argument("ticket")
    p.add_argument("--notes", required=True, help="Claude-written 'Where I stopped / Decisions / Open points' markdown.")
    p.add_argument("--yes", action="store_true")
    p = sub.add_parser("restore", help=COMMAND_HELP["restore"], description=COMMAND_HELP["restore"])
    p.add_argument("ticket")
    p.add_argument("--force", action="store_true", help="Overwrite an existing local work folder.")
    sub.add_parser("db-ticket", help=COMMAND_HELP["db-ticket"], description=COMMAND_HELP["db-ticket"]).add_argument("ticket")
    p = sub.add_parser("db-create", help=COMMAND_HELP["db-create"], description=COMMAND_HELP["db-create"])
    p.add_argument("ticket")
    p.add_argument("--title", required=True, help="DB | #{N} <what changes in the database>")
    p.add_argument("--description-file", help="A file in the ticket folder with the description.")
    p.add_argument("--yes", action="store_true", help="Create it (without this: preview only).")
    p.add_argument("--confirm", metavar="HASH", help="with --yes: the `confirm` hash the preview returned")
    sub.add_parser("db-files", help=COMMAND_HELP["db-files"], description=COMMAND_HELP["db-files"]).add_argument("ticket")
    p = sub.add_parser("db-branch", help=COMMAND_HELP["db-branch"], description=COMMAND_HELP["db-branch"])
    p.add_argument("db_ticket")
    p.add_argument("--ticket", help="The WF ticket: the first call checks the DB tree is clean and records db.ticket/db.base.")
    p.add_argument("--push", action="store_true", help="Push a newly created DB branch to origin.")
    sub.add_parser("sync-check", help=COMMAND_HELP["sync-check"], description=COMMAND_HELP["sync-check"]).add_argument("ticket")
    p = sub.add_parser("sync-branch", help=COMMAND_HELP["sync-branch"], description=COMMAND_HELP["sync-branch"])
    p.add_argument("ticket")
    p.add_argument("--yes", action="store_true")
    p = sub.add_parser("merge-master", help=COMMAND_HELP["merge-master"], description=COMMAND_HELP["merge-master"])
    p.add_argument("ticket")
    p.add_argument("--yes", action="store_true")
    p = sub.add_parser("pushed", help=COMMAND_HELP["pushed"], description=COMMAND_HELP["pushed"])
    p.add_argument("ticket")
    p.add_argument("--db", action="store_true", help="Check the DB ticket's branch in C2M_CI_DB instead.")
    p = sub.add_parser("epics", help=COMMAND_HELP["epics"], description=COMMAND_HELP["epics"])
    p.add_argument("ticket")
    p.add_argument("--limit", type=int)
    p.add_argument("--refresh", action="store_true", help="Ignore the Epic cache.")
    p = sub.add_parser("fix-attributes", help=COMMAND_HELP["fix-attributes"], description=COMMAND_HELP["fix-attributes"])
    p.add_argument("ticket")
    p.add_argument("--epic", help="Epic ticket number chosen by the developer.")
    p.add_argument("--yes", action="store_true")
    p = sub.add_parser("summary-check", help=COMMAND_HELP["summary-check"], description=COMMAND_HELP["summary-check"])
    p.add_argument("ticket")
    p.add_argument("--file")
    p = sub.add_parser("rca", help=COMMAND_HELP["rca"], description=COMMAND_HELP["rca"])
    p.add_argument("ticket")
    p.add_argument("--rca-file")
    p.add_argument("--clca-file")
    p.add_argument("--plain", action="store_true", help="Write the CLCA exactly as in the file (no closing sweep line).")
    p.add_argument("--yes", action="store_true")
    p = sub.add_parser("handoff", help=COMMAND_HELP["handoff"], description=COMMAND_HELP["handoff"])
    p.add_argument("ticket")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--confirm", metavar="HASH", help="with --yes: the `confirm` hash the preview returned")
    p = sub.add_parser("review-pick", help=COMMAND_HELP["review-pick"], description=COMMAND_HELP["review-pick"])
    p.add_argument("--ticket")
    p = sub.add_parser("review-fetch", help=COMMAND_HELP["review-fetch"], description=COMMAND_HELP["review-fetch"])
    p.add_argument("ticket")
    p = sub.add_parser("review-deploy", help=COMMAND_HELP["review-deploy"], description=COMMAND_HELP["review-deploy"])
    p.add_argument("ticket")
    p.add_argument("--yes", action="store_true")
    p = sub.add_parser("review-withdraw", help=COMMAND_HELP["review-withdraw"], description=COMMAND_HELP["review-withdraw"])
    p.add_argument("ticket")
    p.add_argument("--yes", action="store_true")
    p = sub.add_parser("quality", help=COMMAND_HELP["quality"], description=COMMAND_HELP["quality"])
    p.add_argument("ticket")
    p.add_argument("--skip", action="append", choices=["sonar", "snyk"], help="Skip a tool (needs --reason).")
    p.add_argument("--reason", help="Why the skipped tool cannot run (it goes in the hand-off).")
    p = sub.add_parser("quality-bounce", help=COMMAND_HELP["quality-bounce"], description=COMMAND_HELP["quality-bounce"])
    p.add_argument("--ticket", help="Only this Build Failed ticket.")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--confirm", help="The hash the preview returned.")
    p = sub.add_parser("teams-notify", help=COMMAND_HELP["teams-notify"], description=COMMAND_HELP["teams-notify"])
    p.add_argument("ticket")
    p.add_argument("--again", action="store_true", help="Post a reminder although this round was already posted.")
    p.add_argument("--yes", action="store_true")
    p = sub.add_parser("rework", help=COMMAND_HELP["rework"], description=COMMAND_HELP["rework"])
    p.add_argument("ticket")
    p.add_argument("--yes", action="store_true")
    p = sub.add_parser("review-verdict", help=COMMAND_HELP["review-verdict"], description=COMMAND_HELP["review-verdict"])
    p.add_argument("ticket")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--reject", action="store_true", help="Send the ticket back to its author with the findings.")
    mode.add_argument("--approve", action="store_true", help="Approve: Dev Done, bound to the reviewed head.")
    p.add_argument("--file", required=True, help="verdict.json inside DEV_TICKETS/N (written from the reviewer's decisions).")
    p.add_argument("--yes", action="store_true")
    p = sub.add_parser("review-compare", help=COMMAND_HELP["review-compare"], description=COMMAND_HELP["review-compare"])
    p.add_argument("ticket")
    p.add_argument("--early", action="store_true", help="Leave the functional checks out (they usually come last).")
    p = sub.add_parser("assign", help=COMMAND_HELP["assign"], description=COMMAND_HELP["assign"])
    p.add_argument("ticket")
    p.add_argument("--list", action="store_true", help="Show the reviewers the ticket can be handed to.")
    p.add_argument("--to", help="Assembla login of the reviewer (must be in `reviewers`).")
    p.add_argument("--yes", action="store_true")
    p = sub.add_parser("comment", help=COMMAND_HELP["comment"], description=COMMAND_HELP["comment"])
    p.add_argument("ticket")
    p.add_argument("--file", required=True)
    p.add_argument("--edit-id", help="Edit this existing comment id in place instead of posting a new one.")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--confirm", metavar="HASH", help="with --yes: the `confirm` hash the preview returned")
    p = sub.add_parser("status", help=COMMAND_HELP["status"], description=COMMAND_HELP["status"])
    p.add_argument("ticket")
    p.add_argument("--to", required=True)
    p.add_argument("--yes", action="store_true")
    p.add_argument("--confirm", metavar="HASH", help="with --yes: the `confirm` hash the preview returned")
    p.add_argument("--requested", action="store_true",
                   help="developer explicitly asked to deploy: allows Code Review -> DevOps Staging on any risk tier")
    return parser


HANDLERS = {"whoami": cmd_whoami, "pick": cmd_pick, "fetch": ticket_fetch.cmd_fetch, "takeover": cmd_takeover,
            "branch": cmd_branch, "epics": cmd_epics, "fix-attributes": cmd_fix_attributes,
            "comment": cmd_comment, "status": cmd_status, "rca": cmd_rca, "summary-check": cmd_summary_check,
            "sync-check": cmd_sync_check, "pushed": cmd_pushed, "creds": cmd_creds, "config-check": cmd_config_check,
            "db-ticket": cmd_db_ticket, "db-create": cmd_db_create, "db-branch": cmd_db_branch, "host-check": cmd_host_check,
            "handover": cmd_handover, "restore": cmd_restore, "merge-master": cmd_merge_master, "sync-branch": cmd_sync_branch,
            "state": cmd_state, "event": cmd_event, "baseline": cmd_baseline,
            "review-check": cmd_review_check, "db-files": cmd_db_files, "gate-round": cmd_gate_round,
            "deps": cmd_deps, "profile": cmd_profile, "env-check": cmd_env_check, "env-apply": cmd_env_apply,
            "env-restore": cmd_env_restore, "service-start": cmd_service_start, "func-record": cmd_func_record, "func-mode": cmd_func_mode,
            "func-prepare": cmd_func_prepare, "review-brief": cmd_review_brief,
            "followup-create": cmd_followup_create,
            "role": cmd_role, "handoff": cmd_handoff, "assign": cmd_assign, "review-pick": cmd_review_pick,
            "review-fetch": cmd_review_fetch, "review-compare": cmd_review_compare,
            "review-verdict": cmd_review_verdict, "review-deploy": cmd_review_deploy,
            "review-withdraw": cmd_review_withdraw, "rework": cmd_rework, "teams-notify": cmd_teams_notify,
            "quality": cmd_quality, "quality-bounce": cmd_quality_bounce}


NO_API = ("takeover", "sync-check", "pushed", "creds", "config-check", "review-compare", "db-branch", "host-check", "merge-master", "sync-branch",
          "state", "event", "baseline", "review-check", "db-files", "gate-round", "deps", "profile", "env-check",
          "env-apply", "env-restore", "service-start", "func-record", "func-mode", "func-prepare", "review-brief")  # subcommands that never call Assembla


def main() -> int:
    args = build_parser().parse_args()
    try:
        api = None if args.cmd in NO_API else AssemblaApi()
        return HANDLERS[args.cmd](api, args)
    except AssemblaError as exc:
        return result("ERROR", 1, message=str(exc))
    except Exception as exc:  # noqa: BLE001 - every run must end with a RESULT line, never a bare traceback
        return result("ERROR", 1, message=f"{type(exc).__name__}: {exc}",
                      note="Unexpected error. If this was a write (comment, status, upload), check Assembla "
                           "before retrying: it may have gone through.")


if __name__ == "__main__":
    sys.exit(main())
