"""`dt quality N` (the developer's Sonar / Snyk step before hand-off) and `dt quality-bounce` (send tickets whose
Jenkins build failed on Sonar or Snyk back to Dev Queue). See quality.py for what is checked and how."""
import json
import os
import re
from datetime import datetime, timezone

import quality
from assembla_api import AssemblaApi, AssemblaError, bare_number
from develop_ticket import common
from develop_ticket.common import CONFIG, STATUS, _write_state, now_iso, record_metric, result, ticket_dir
from develop_ticket.protocol import BOUNCE_BEGIN, BOUNCE_END, latest_handoff, parse_bounce
TOOLS = ("sonar", "snyk")
ERROR_STATUSES = {"SCAN_FAILED", "TOOL_MISSING", "NOT_AUTHENTICATED", "NO_TOKEN"}
BUILD_FAILED_RE = re.compile(r"Build for (\S+) is failed")
FAILED_APPS_RE = re.compile(r"Failed application:\s*\[([^\]]*)\]")
MAX_LISTED = 40


def _ts(value) -> datetime:
    """An Assembla or workflow timestamp ("2026-11-11T00:47:33.000Z" / "2026-11-11T00:47:33Z") as UTC."""
    text = str(value or "").strip()
    for fmt, size in (("%Y-%m-%dT%H:%M:%S", 19), ("%Y-%m-%d", 10)):
        try:
            return datetime.strptime(text[:size], fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return datetime.min.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------- dt quality N

def quality_problem(state: dict, fingerprint: str):
    """None when the recorded quality run passes for exactly these files, else (status, message). Hand-off uses it."""
    if not quality.enabled(CONFIG):
        return None
    rec = state.get("quality") or {}
    if not rec:
        return "QUALITY_REQUIRED", "Run dt quality N (Sonar and Snyk) as the last step before hand-off."
    if rec.get("fingerprint") != fingerprint:
        return "QUALITY_STALE", "The files changed since the last dt quality run: run it again."
    if rec.get("status") not in ("PASS", "NO_APPS"):
        return "QUALITY_FAILED", (f"dt quality is {rec.get('status')}: fix the blocking issues (or skip a tool with "
                                  "--skip TOOL --reason), then run it again.")
    return None


def _overall(sonar: list, snyk: list, skips: dict) -> str:
    """PASS / FAIL / ERROR across both tools (a skipped tool counts as passed, its reason is recorded)."""
    statuses = []
    for tool, rows in (("sonar", sonar), ("snyk", snyk)):
        if tool in skips:
            continue
        statuses += [r.get("status") for r in rows]
    if any(s == "FAIL" for s in statuses):
        return "FAIL"
    if any(s in ERROR_STATUSES for s in statuses):
        return "ERROR"
    return "PASS"


def _report(number: str, rec: dict) -> str:
    """DEV_TICKETS/N/quality/quality-report.md: what to fix, file by file (Claude reads it; never posted)."""
    lines = [f"# Quality check #{number} ({rec['at']})", "", f"Overall: **{rec['status']}**", ""]
    for row in rec["sonar"]:
        lines.append(f"## Sonar — {row['app']} ({row.get('status')})")
        if row.get("message"):
            lines.append(f"- {row['message']}")
        if row.get("dashboard"):
            lines.append(f"- Dev project: {row['dashboard']}")
        for title, key in (("Blocking (High / Medium) — fix these", "blocking"),
                           ("Low — listed only (the Jenkins gate still counts them)", "low")):
            items = row.get(key) or []
            if items:
                lines += ["", f"### {title}"]
                lines += [f"- [{i['severity']}] {i['file']}:{i.get('line') or '-'} `{i['rule']}` {i['message']}" for i in items]
        lines.append("")
    for row in rec["snyk"]:
        lines.append(f"## Snyk — {row['app']} ({row.get('status')}), file {row.get('file')}")
        if row.get("message"):
            lines.append(f"- {row['message']}")
        for title, key in (("Fixable — upgrade or patch these", "fixable"), ("Unfixable — listed only", "unfixable")):
            items = row.get(key) or []
            if items:
                lines += ["", f"### {title}"]
                lines += [f"- [{v['severity']}] {v['package']} {v['title']} ({v['id']})"
                          + (f" → upgrade to {v['upgrade_to']}" if v.get("upgrade_to") else "")
                          + (f"; fixed in {', '.join(v['fixed_in'])}" if v.get("fixed_in") else "")
                          + (f"; via {v['path']}" if v.get("path") else "") for v in items]
        lines.append("")
    for tool, reason in (rec.get("skipped") or {}).items():
        lines.append(f"- {tool} skipped: {reason}")
    return "\n".join(lines).rstrip() + "\n"


def _trim(rows: list) -> list:
    out = []
    for row in rows:
        row = dict(row)
        for key in ("blocking", "low", "fixable", "unfixable"):
            if key in row:
                row[key + "_count"] = len(row[key])
                row[key] = row[key][:MAX_LISTED]
        out.append(row)
    return out


def cmd_quality(api: AssemblaApi, args) -> int:
    """Run Sonar (into the developer's own project) and Snyk for the apps this ticket changed; record the result."""
    number = bare_number(args.ticket)
    if not quality.enabled(CONFIG):
        return result("QUALITY_OFF", ticket=number, message="Sonar / Snyk checks are not enabled for this repo (config `quality`).")
    state, state_path = common.load_state(number)
    if state_path is None:
        return result("NO_STATE", 1, ticket=number, message="Run fetch first.")
    skips = {}
    for tool in args.skip or []:
        if tool not in TOOLS:
            return result("BAD_ARG", 1, ticket=number, message=f"--skip takes {' or '.join(TOOLS)}.")
        if not (args.reason or "").strip():
            return result("REASON_REQUIRED", ticket=number, message="A skipped tool needs --reason (it goes in the hand-off).")
        skips[tool] = args.reason.strip()

    import local_gates  # lazy: it reconfigures stdout on import
    local_gates.refresh_base()
    files = local_gates.changed_files()
    fingerprint = local_gates.diff_fingerprint(files)
    touched = quality.touched_apps(CONFIG, files)
    repo = common.PROJECT_ROOT
    rec = {"at": now_iso(), "fingerprint": fingerprint, "apps": touched, "skipped": skips, "sonar": [], "snyk": []}
    if not touched:
        rec["status"] = "NO_APPS"
    else:
        lines = quality.changed_lines(repo, local_gates.BASE, files)
        cfg = quality.settings(CONFIG)
        login = str(api.get_current_user().get("login") or "")
        sonar_token, snyk_token = os.environ.get("SONAR_TOKEN", ""), os.environ.get("SNYK_TOKEN", "")
        for app in touched:
            app_cfg = quality.apps(CONFIG)[app]
            if app_cfg.get("sonar") and "sonar" not in skips:
                if not sonar_token:
                    rec["sonar"].append({"app": app, "status": "NO_TOKEN",
                                         "message": "Add SONAR_TOKEN to credentials.env (SonarQube > My Account > Security)."})
                else:
                    rec["sonar"].append(quality.run_sonar(app, app_cfg, repo, login, cfg.get("sonar_host"), sonar_token, lines))
            if app_cfg.get("snyk") and "snyk" not in skips:
                rec["snyk"].append(quality.run_snyk(app, app_cfg, repo, snyk_token))
        rec["status"] = _overall(rec["sonar"], rec["snyk"], skips)
    folder = ticket_dir(number) / "quality"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "quality-report.md").write_text(_report(number, rec), encoding="utf-8")
    state["quality"] = dict(rec, sonar=_trim(rec["sonar"]), snyk=_trim(rec["snyk"]))
    _write_state(state_path, state, f"quality {rec['status'].lower()}")
    record_metric(number, "quality", status=rec["status"], apps=touched,
                  sonar_blocking=sum(len(r.get("blocking") or []) for r in rec["sonar"]),
                  snyk_fixable=sum(len(r.get("fixable") or []) for r in rec["snyk"]), skipped=sorted(skips))
    summary = {"sonar": [{k: r.get(k) for k in ("app", "status", "message", "dashboard")} | {
                   "blocking": len(r.get("blocking") or []), "low": len(r.get("low") or [])} for r in rec["sonar"]],
               "snyk": [{k: r.get(k) for k in ("app", "status", "message", "hint")} | {
                   "fixable": len(r.get("fixable") or []), "unfixable": len(r.get("unfixable") or [])} for r in rec["snyk"]]}
    return result(rec["status"], 0, ticket=number, apps=touched, skipped=skips or None,
                  report=str(folder / "quality-report.md"), **summary)


# ---------------------------------------------------------------------- bounce comment

def render_bounce(p: dict) -> str:
    lines = [f"*Build failed on code quality for #{p['ticket']}: back to {p['to_status']}*", ""]
    for app in p["apps"]:
        lines.append(f"* *{app['app']}* (Jenkins {app['job']} #{app['build']}): {', '.join(app['reasons'])}")
        for c in app.get("gate_conditions") or []:
            lines.append(f"** Sonar quality gate: {c['metric']} = {c['actual']} (fails when {c['comparator']} {c['threshold']})")
        for i in (app.get("sonar_issues") or [])[:MAX_LISTED]:
            lines.append(f"** [{i['severity']}] {i['file']}:{i.get('line') or '-'} {i['message']} ({i['rule']})")
        if app.get("snyk"):
            lines.append(f"** Snyk: {app['snyk']['vulnerabilities']} vulnerabilities (Medium or higher fail the build)")
    lines += ["", f"Author {p['author']}: run /dev-ticket {p['ticket']}, fix these, then `dt quality {p['ticket']}` "
                  "before handing off again.", "", BOUNCE_BEGIN,
              json.dumps(p, indent=2, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e"), BOUNCE_END]
    return "\n".join(lines)


def latest_bounce(api: AssemblaApi, number: str):
    """The newest QUALITY-BOUNCE comment: {payload, text, comment_id, writer_login, at} or None."""
    found = [(c, parse_bounce(c.get("comment"))) for c in api.get_comments(number)]
    found = [(c, d) for c, d in found if d is not None]
    if not found:
        return None
    comment, data = found[-1]
    return {"payload": data, "text": comment.get("comment") or "", "comment_id": comment.get("id"),
            "writer_login": api.get_user(comment.get("user_id")).get("login") or "", "at": comment.get("created_on")}


def bounce_findings(payload: dict) -> list:
    """The bounce's reasons in the REVIEW_VERDICT findings shape, so the rework flow reads them the same way."""
    out = []
    for app in payload.get("apps") or []:
        for c in app.get("gate_conditions") or []:
            out.append({"severity": "High", "file_line": app["app"],
                        "finding": f"Sonar quality gate failed: {c['metric']} = {c['actual']} ({c['comparator']} {c['threshold']}).",
                        "fix": "Fix the new Sonar issues below (all severities count for this gate), then dt quality."})
        for i in app.get("sonar_issues") or []:
            out.append({"severity": str(i.get("severity") or "").title(), "file_line": f"{app['app']}: {i['file']}:{i.get('line') or '-'}",
                        "finding": f"{i['message']} ({i['rule']})", "fix": "Fix as Sonar suggests; re-run dt quality."})
        if app.get("snyk"):
            out.append({"severity": "High", "file_line": app["app"],
                        "finding": f"Snyk: {app['snyk']['vulnerabilities']} vulnerabilities (Medium or higher fail the build).",
                        "fix": "Run dt quality: upgrade or patch the fixable ones."})
    return out


def bounce_as_verdict(api: AssemblaApi, number: str, handoff: dict, verdict):
    """A QUALITY-BOUNCE newer than the hand-off and than any review verdict, shaped like a REJECT verdict; else None."""
    bounce = latest_bounce(api, number)
    if not bounce or _ts(bounce["at"]) < _ts(handoff.get("at")):
        return None
    if verdict and _ts(verdict.get("at")) > _ts(bounce["at"]):
        return None
    payload = {"verdict": "REJECT", "round": handoff.get("round"), "head": handoff.get("head"),
               "reviewer": bounce["writer_login"], "findings": bounce_findings(bounce["payload"]),
               "summary": "Jenkins failed the build on code quality (" + ", ".join(bounce["payload"].get("reasons") or [])
                          + "): fix the findings and run dt quality before handing off again."}
    return {"payload": payload, "text": bounce["text"], "comment_id": bounce["comment_id"],
            "writer_login": bounce["writer_login"], "at": bounce["at"], "bounce": True}


# ---------------------------------------------------------------------- dt quality-bounce

def _jenkins():
    import deploy_staging_local as local  # same folder; JENKINS_REMOTE_* from this folder's credentials.env
    return local.JenkinsClient(os.environ.get("JENKINS_REMOTE_USER", ""), os.environ.get("JENKINS_REMOTE_API_TOKEN", ""),
                               os.environ.get("JENKINS_REMOTE_BASE_URL") or local.JENKINS_REMOTE_BASE_URL)


def _sonar_issues(project_key: str, app_folder: str = ""):
    host, token = quality.settings(CONFIG).get("sonar_host"), os.environ.get("SONAR_TOKEN", "")
    if not (project_key and host and token):
        return []
    try:
        issues = quality.SonarClient(host, token).issues(project_key, new_code_only=True)
    except (OSError, ValueError):  # best effort (URLError is an OSError): the gate conditions still say why
        return []
    return [quality.issue_view(i, project_key, app_folder) for i in issues][:MAX_LISTED]


def examine(api: AssemblaApi, jenkins, ticket: dict, bounce_cfg: dict, users: dict) -> dict:
    """Why ticket N is in Build Failed, and who it goes back to. -> a row (never console lines)."""
    number = str(ticket.get("number"))
    row = {"ticket": number, "summary": ticket.get("summary"), "status": ticket.get("status")}
    comments = api.get_comments(number)
    failures = [c for c in comments if BUILD_FAILED_RE.search(c.get("comment") or "")
                or FAILED_APPS_RE.search(c.get("comment") or "")]
    if not failures:
        return dict(row, verdict="NO_BUILD_FAILURE", message="No 'Build for X is failed' comment on the ticket.")
    last = failures[-1]
    failed_at = _ts(last.get("created_on"))
    apps = []
    for c in failures:
        if abs((_ts(c.get("created_on")) - failed_at).total_seconds()) > 3600:
            continue
        text = c.get("comment") or ""
        apps += BUILD_FAILED_RE.findall(text)
        for group in FAILED_APPS_RE.findall(text):
            apps += [a.strip().strip("'\"") for a in group.split(",") if a.strip()]
    apps = sorted(set(apps))
    bounce = latest_bounce(api, number)
    if bounce and _ts(bounce["at"]) >= failed_at:
        return dict(row, verdict="ALREADY_BOUNCED", apps=apps, message="Already sent back for this failure.")
    merge = quality.find_merge_build(jenkins, bounce_cfg.get("merge_jobs") or [], number,
                                     int(failed_at.timestamp() * 1000) + 120_000)
    if not merge:
        return dict(row, verdict="NO_MERGE_RUN", apps=apps,
                    message="No merge job run for this ticket was found near the failure: check Jenkins by hand.")
    details = []
    for app in apps:
        job = (bounce_cfg.get("app_jobs") or {}).get(app, app)
        build = quality.find_app_build(jenkins, job, merge["start"], merge["end"])
        if not build:
            details.append({"app": app, "job": job, "build": None, "reasons": ["UNKNOWN"]})
            continue
        _, text = jenkins._request(f"/job/{job}/{build['number']}/consoleText")
        facts = quality.classify_build_console(text or "")
        entry = {"app": app, "job": job, "build": build["number"], "reasons": facts["reasons"],
                 "gate_conditions": facts["gate_conditions"], "snyk": facts["snyk"]}
        if "SONAR" in facts["reasons"]:
            entry["sonar_issues"] = _sonar_issues(facts["sonar_project"] or app, app)
        details.append(entry)
    reasons = sorted({r for d in details for r in d["reasons"]})
    quality_only = bool(details) and all(set(d["reasons"]) <= {"SONAR", "SNYK"} for d in details)
    found = latest_handoff(api, number)
    author = (found or {}).get("writer_login") or ""
    author_user = users.get(author.lower())
    row.update(apps=details, reasons=reasons, merge={"job": merge["job"], "build": merge["number"]}, author=author or None)
    if not quality_only:
        return dict(row, verdict="NOT_QUALITY",
                    message="At least one app failed for another reason (compile, tests, deploy): the author checks Jenkins.")
    if not author_user:
        return dict(row, verdict="NO_AUTHOR", message="No hand-off author found to send it back to: assign it by hand.")
    return dict(row, verdict="BOUNCE", author_id=author_user.get("id"), round=((found or {}).get("payload") or {}).get("round"),
                head=((found or {}).get("payload") or {}).get("head"))


def cmd_quality_bounce(api: AssemblaApi, args) -> int:
    """List Build Failed tickets, say which failed on Sonar / Snyk, and with --yes send those back to Dev Queue,
    assigned to the author, with the reasons as a comment."""
    from develop_ticket.review import _reviewer_problem
    cfg = quality.settings(CONFIG).get("bounce") or {}
    if not (quality.enabled(CONFIG) and cfg.get("enabled")):
        return result("BOUNCE_OFF", message="The Sonar / Snyk bounce is not enabled for this team (config quality.bounce).")
    me = api.get_current_user()
    problem = _reviewer_problem(api, me)
    if problem:
        return result(problem[0], message=problem[1])
    statuses = set(cfg.get("failed_statuses") or ["Build Failed"])
    tickets = [t for t in api.get_active_tickets() if t.get("status") in statuses]
    if args.ticket:
        tickets = [t for t in tickets if str(t.get("number")) == bare_number(args.ticket)]
        if not tickets:
            return result("NOT_FAILED", ticket=bare_number(args.ticket), message=f"The ticket is not in {sorted(statuses)}.")
    jenkins = _jenkins()
    users = {str(u.get("login", "")).lower(): u for u in api.get_space_users()}
    rows = [examine(api, jenkins, t, cfg, users) for t in tickets]
    to_bounce = [r for r in rows if r["verdict"] == "BOUNCE"]
    plan = [{"ticket": r["ticket"], "author": r["author"], "reasons": r["reasons"]} for r in to_bounce]
    shown = [{k: v for k, v in r.items() if k not in ("author_id",)} for r in rows]
    if not args.yes or not to_bounce:
        return result("PREVIEW" if to_bounce else "NOTHING_TO_BOUNCE", tickets=shown,
                      confirm=common.confirm_token("quality-bounce", plan) if to_bounce else None,
                      message=(f"Re-run with --yes --confirm <confirm> to send {len(to_bounce)} ticket(s) back to "
                               f"{STATUS['queue']}." if to_bounce else "No Build Failed ticket failed on Sonar / Snyk only."))
    refused = common.confirm_refusal("quality-bounce", "", args, plan)
    if refused is not None:
        return refused
    done = []
    for r in to_bounce:
        payload = {"ticket": r["ticket"], "author": r["author"], "round": r.get("round"), "head": r.get("head"),
                   "reasons": r["reasons"], "to_status": STATUS["queue"], "by": me.get("login"), "at": now_iso(),
                   "apps": r["apps"]}
        try:
            api.add_comment(r["ticket"], render_bounce(payload))
            api.assign_and_move(r["ticket"], r["author_id"], STATUS["queue"])
            done.append({"ticket": r["ticket"], "status": "BOUNCED", "author": r["author"]})
            record_metric(r["ticket"], "quality_bounce", reasons=r["reasons"], author=r["author"])
        except AssemblaError as exc:
            done.append({"ticket": r["ticket"], "status": "MOVE_FAILED", "message": str(exc)[:200]})
    failed = [d for d in done if d["status"] != "BOUNCED"]
    return result("BOUNCED" if not failed else "PARTLY_BOUNCED", 1 if failed else 0, results=done)
