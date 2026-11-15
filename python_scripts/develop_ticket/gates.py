"""Gate-side commands run while developing: dt baseline, dt gate-round, dt deps."""

import re

from assembla_api import bare_number
import metrics
from state_io import write_state

from develop_ticket import common
from develop_ticket.common import CONFIG, now_iso, result, ticket_dir


PLAN_PATH_RE = re.compile(r"`([\w.\-]+(?:[\\/][\w.\-]+)+\.\w+)`")


def planned_files(number: str, explicit: list = None) -> tuple:
    """(all planned paths, the source files among them): --files, else the backticked repo paths in PLAN.md."""
    import local_gates  # lazy: it reconfigures stdout on import
    if explicit:
        names = [f.replace("\\", "/") for f in explicit]
    else:
        plan = ticket_dir(number) / "PLAN.md"
        text = plan.read_text(encoding="utf-8", errors="replace") if plan.is_file() else ""
        names = [m.replace("\\", "/") for m in PLAN_PATH_RE.findall(text)]
    source = sorted({n for n in names if n.startswith(local_gates.SOURCE_ROOTS) and not local_gates.is_local_only(n)})
    return sorted(set(names)), source


def cmd_baseline(_api, args) -> int:
    """Run the unit tests of the projects this ticket plans to modify, on the untouched start commit (backlog A-4).

    Run at the end of step 5, once PLAN.md says which files change and before the first edit: a branch created from
    master must start green for the code it is about to touch. The test projects come from the planned files by the
    same rules the gates use (nearest .csproj and the test projects that reference it; Angular specs next to the
    files), so only the projects being modified are run. On BASELINE_FAIL the failures come back grouped by cause so
    the developer can be shown why (stale test or code problem) and decide (S11); step 6 stays blocked until the
    baseline passes or the developer accepts it (`dt state N --set baseline.accepted=true`). Only for a branch
    created from master (state.branch_origin == created); refused unless HEAD is still state.start_head and the tree
    is clean."""
    number = bare_number(args.ticket)
    state, path = common.load_state(number)
    if path is None:
        return result("NO_STATE", 1, ticket=number, message="Run fetch first.")
    if state.get("branch_origin") != "created":
        return result("NOT_NEW_BRANCH", 1, ticket=number, branch_origin=state.get("branch_origin"),
                      message="The baseline applies to a branch created from master. On an existing branch work from "
                              "the ticket's functionality and fix the related failures instead.")
    head = common.git("rev-parse", "--short", "HEAD").stdout.strip()
    if not state.get("start_head") or state["start_head"] != head:
        return result("NOT_AT_START", 1, ticket=number, start_head=state.get("start_head"), head=head,
                      message="A baseline is only meaningful on the commit the ticket started from.")
    unclean = common.start_dirty_files()
    if unclean:
        return result("NOT_CLEAN_START", 1, ticket=number, files=unclean[:20],
                      message="The tree changed since the clean start; a baseline would include the ticket's own work.")
    import baseline
    import local_gates
    paths, files = planned_files(number, args.files)
    if not paths:  # no paths at all: the plan didn't say what changes. Docs-only paths are fine (nothing to baseline).
        return result("NO_FILES", 1, ticket=number,
                      message="No planned files found. Put repo-relative paths in backticks in PLAN.md "
                              "(files: `C2MWebAPI/…/X.cs`) or pass --files.")
    built = sorted({p for p in (local_gates.nearest_csproj(f) for f in files if f.endswith((".cs", ".csproj"))) if p})
    dotnet_projects = [str(p.relative_to(common.PROJECT_ROOT)).replace("\\", "/") for p in local_gates.test_projects_for(built)]
    ng_gates = [g for g in local_gates.plan_gates(files, {"pre-commit", "dotnet-build", "dotnet-test", "db-checks"}, number)
                if g["name"].startswith("ng-test")]
    out_dir = ticket_dir(number) / "gates" / "baseline"
    results = baseline.run_baseline(common.PROJECT_ROOT, dotnet_projects, out_dir,
                                    int(CONFIG.get("gate_timeout_seconds", 1800)), local_gates.SECRET_ENV)
    if ng_gates:
        out_dir.mkdir(parents=True, exist_ok=True)
        for gate in ng_gates:
            outcome = local_gates.run_gate(gate, out_dir)
            results.append({"project": gate["name"], "status": outcome["status"], "errors": outcome.get("errors", []),
                            "log": outcome.get("log")})
    status = baseline.overall_status(results)  # no test project for these files: nothing to run, nothing to block
    state["baseline"] = {"status": status, "at": now_iso(), "head": head, "files": files,
                         "projects": [{k: p.get(k) for k in ("project", "status", "total", "failed")} for p in results]}
    write_state(path, state)
    failed = sum(p.get("failed") or 0 for p in results)
    messages = {"PASS": ("Baseline is green." if results else "No unit-test project covers the planned files: "
                                                                "nothing to baseline.") + " Step 6 can start.",
                "FAIL": f"{failed or 'Some'} test(s) fail on the untouched start commit. Find out why for each group "
                        "(read the test and the code under test), show the developer whether it is a stale test or a "
                        "code problem, and let them decide (S11). Do not start step 6 first.",
                "ERROR": "A test project produced no results (build failure, timeout or a missing tool): see errors and log."}
    return result("BASELINE_" + status, 0 if status == "PASS" else 2, ticket=number, files=files,
                  projects=results, message=messages[status])


def latest_gates_dir(number):
    """The newest timestamped DEV_TICKETS/{N}/gates/<yyyymmdd-hhmmss> folder (the baseline folder is excluded)."""
    base = ticket_dir(number) / "gates"
    runs = sorted(p for p in base.glob("*") if p.is_dir() and re.fullmatch(r"\d{8}-\d{6}", p.name)) if base.is_dir() else []
    return runs[-1] if runs else None


def cmd_gate_round(_api, args) -> int:
    """Start the next automated fix round of the 7a unit-test gate (improvements item 3).

    Reads the failures of the last gates run, checks which of them already fail on the ticket's start commit (a
    temporary checkout, nothing in the working tree is touched; --no-verify skips this), counts the rounds used from
    metrics.jsonl and logs the next `fix_round` itself, so the cap (gate_rounds.MAX_ROUNDS = 3) is enforced by the
    script. ROUND_READY lists the failures in groups: auto_fixable (pre-existing NG0100 test setup: Claude may edit
    the test setup without asking), needs_decision (pre-existing, anything else: ask unless the fix leaves every
    assertion and expected value alone), ticket_failures (not failing on the start commit: the ticket's own, fixed
    as in the normal build loop) and unverified. ROUND_CAP means ask the developer (S9)."""
    import gate_rounds
    import local_gates
    number = bare_number(args.ticket)
    state, path = common.load_state(number)
    if path is None:
        return result("NO_STATE", 1, ticket=number, message="Run fetch first.")
    run_dir = latest_gates_dir(number)
    logs = sorted(run_dir.glob("ng-test*.log")) if run_dir else []
    if not logs:
        return result("NO_TEST_LOG", 1, ticket=number,
                      message="No ng-test log in the last gates run: run local_gates.py --ticket N first.")
    failures = []
    for log in logs:
        failures += gate_rounds.parse_karma_failures(log.read_text(encoding="utf-8", errors="replace"))
    failures = gate_rounds.mark_pre_existing(failures, [], checked=False)
    used = gate_rounds.rounds_used(metrics.read(common.PROJECT_ROOT, CONFIG["tickets_dir"], number))
    plan = gate_rounds.plan_round(failures, used)
    if plan["status"] == "NO_FAILURES":
        return result("NO_FAILURES", ticket=number, gates_run=run_dir.name,
                      message="The last gates run has no failing unit test.")
    if plan["status"] == "ROUND_CAP":
        return result("ROUND_CAP", 2, ticket=number, rounds_used=used, cap=plan["cap"], failures=failures,
                      message=f"{used} fix rounds used (cap {plan['cap']}). Show the developer what was tried and "
                              "what still fails, and ask how to proceed (S9).")

    verification = "skipped (--no-verify)"
    if not args.no_verify:
        verification = "not run: no start commit recorded"
        start_head = state.get("start_head")
        specs = local_gates.angular_specs(list((state.get("gates") or {}).get("files") or []))
        if start_head and specs:
            start_failures, checked, notes = [], True, []
            timeout = int(CONFIG.get("gate_timeout_seconds", 1800))
            for project, project_specs in specs.items():
                outcome = gate_rounds.start_check(common.PROJECT_ROOT, start_head, project, project_specs,
                                                  ticket_dir(number), timeout)
                notes.append(f"{project}: {outcome['message']}")
                if outcome["ok"]:
                    start_failures += outcome["failures"]
                else:
                    checked = False
            failures = gate_rounds.mark_pre_existing(failures, start_failures, checked)
            plan = gate_rounds.plan_round(failures, used)
            verification = "; ".join(notes) if checked else "incomplete: " + "; ".join(notes)

    metrics.record(common.PROJECT_ROOT, CONFIG["tickets_dir"], number, "fix_round", step=gate_rounds.STEP,
                   attempt=plan["attempt"])
    note = ticket_dir(number) / "gates" / f"round-{plan['attempt']}.md"
    lines = [f"# 7a fix round {plan['attempt']} of {plan['cap']} (gates run {run_dir.name})", "",
             f"Start-commit check: {verification}", "", "| Test | Cause | Failed on start commit | Group |", "|---|---|---|---|"]
    groups = {"auto_fixable": "auto-fix (test setup)", "needs_decision": "needs a decision",
              "ticket_failures": "the ticket's own", "unverified": "unverified"}
    for key, label in groups.items():
        for item in plan[key]:
            lines.append(f"| {item['test']} | {item['category']} | {item['pre_existing']} | {label} |")
    lines += ["", "## What was changed and why", "", "(Claude fills this in after the round.)", ""]
    note.write_text("\n".join(lines), encoding="utf-8")
    return result("ROUND_READY", ticket=number, attempt=plan["attempt"], cap=plan["cap"], verification=verification,
                  auto_fixable=plan["auto_fixable"], needs_decision=plan["needs_decision"],
                  ticket_failures=plan["ticket_failures"], unverified=plan["unverified"], note=str(note))


def cmd_deps(_api, args) -> int:
    """Fresh dependencies for the Angular app(s) the ticket changes (improvements item 12).

    Run once the planned files are known: right before `baseline` on a new branch, or before the first edit on an
    existing branch / resume. Deletes the local package-lock.json (only when it is not tracked) and runs
    `npm install --no-package-lock` in each app, so tests start on installed versions that match package.json and the
    tree stays clean for the baseline. DEPS_CONFLICT (peer conflict) is shown to the developer; only on their yes
    re-run with --legacy-peer-deps. --dry-run runs `npm install --dry-run` (nothing installed, lockfile kept)."""
    import deps
    import local_gates
    number = bare_number(args.ticket)
    state, path = common.load_state(number)
    if path is None:
        return result("NO_STATE", 1, ticket=number, message="Run fetch first.")
    projects = [args.project] if args.project else deps.projects_for_files(
        planned_files(number, args.files)[1], local_gates.ANGULAR_PROJECTS)
    if not projects:
        return result("NO_ANGULAR_APP", 0, ticket=number,
                      message="The planned files touch no Angular app (c2mwf_admin, C2MWFAng, c2mwf_runtime): "
                              "nothing to install. Pass --project to force one.")
    if any(p not in local_gates.ANGULAR_PROJECTS for p in projects):
        return result("BAD_PROJECT", 1, ticket=number, allowed=list(local_gates.ANGULAR_PROJECTS))
    timeout = int(CONFIG.get("gate_timeout_seconds", 1800))
    outcomes = [deps.prepare(common.PROJECT_ROOT, p, timeout=timeout, legacy=args.legacy_peer_deps, dry_run=args.dry_run)
                for p in projects]
    ready = all(o["status"] == "DEPS_READY" for o in outcomes)
    if ready and not args.dry_run:
        state["deps"] = {"projects": projects, "at": now_iso(), "legacy_peer_deps": args.legacy_peer_deps}
        write_state(path, state)
    first_problem = next((o["status"] for o in outcomes if o["status"] != "DEPS_READY"), "DEPS_READY")
    return result(first_problem, 0 if ready else 2, ticket=number, projects=outcomes)
