#!/usr/bin/env python3
"""
Quality-metrics report for the dev-ticket workflow (D-53) → one Markdown file.

Decisions: quality first (not speed); local, per developer; baseline = the developer's own
tickets in the last 2 milestones before adoption; output is a .md file.

Two groups are compared on the SAME measures, taken from Assembla status history (every ticket
comment carries `ticket_changes`, e.g. status: QA Queue -> Re-Open, with its timestamp):
  - workflow tickets: every DEV_TICKETS/{N}/ with a state.json
  - baseline tickets: tickets assigned to you in the N milestones (milestone_filter, by due date)
    that ended before your first workflow ticket started
Measures: QA re-open rate, Merge/Build Failed after hand-off, sent back for missing details.

Workflow tickets add what only the workflow records (DEV_TICKETS/{N}/metrics.jsonl): review
rounds and findings by severity, first-round review PASS rate, gate and DB-check failures, risk
bumps, LOW→STANDARD fallbacks, UI-check failures, stops by rule, attribute fixes.

    python metrics_report.py                      # writes DEV_TICKETS/METRICS-<date>.md
    python metrics_report.py --baseline "Workflow 3.15.7" "Workflow 3.16.0"   # pick milestones
    python metrics_report.py --no-baseline        # workflow tickets only (no Assembla history calls for baseline)

Ends with: RESULT: {"status": "WRITTEN", "file": ..., "workflow_tickets": n, "baseline_tickets": n}
"""

import argparse
import json
import re
import sys
from collections import Counter
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import metrics  # noqa: E402
from assembla_api import AssemblaApi, AssemblaError  # noqa: E402
from dev_ticket import CONFIG, PROJECT_ROOT  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

M = CONFIG["metrics"]
TICKETS = PROJECT_ROOT / CONFIG["tickets_dir"]


# ---------------------------------------------------------------------- Assembla status history

def parse_changes(yaml_text: str) -> list:
    """`ticket_changes` YAML ('- - status\\n  - From\\n  - To') → [(field, from, to), ...]."""
    items, current = [], None
    for line in (yaml_text or "").splitlines():
        if line.startswith("- - "):
            current = [line[4:].strip()]
            items.append(current)
        elif line.startswith("  - ") and current is not None:
            current.append(line[4:].strip())
    return [(i[0], i[1] if len(i) > 1 else "", i[2] if len(i) > 2 else "") for i in items]


def status_history(api: AssemblaApi, number) -> list:
    """[(at, from, to)] status moves, oldest first."""
    raw = api._get_paged(api._space_path(f"/tickets/{number}/ticket_comments.json"))
    moves = []
    for c in raw:
        for field, old, new in parse_changes(c.get("ticket_changes")):
            if field == "status":
                moves.append((c.get("created_on") or "", old, new))
    return sorted(moves)


def quality_from_history(moves: list) -> dict:
    """The shared measures, identical for workflow and baseline tickets."""
    qa_at = next((at for at, _, to in moves if to == M["qa_status"]), None)
    after_qa = [(at, old, new) for at, old, new in moves if qa_at and at > qa_at]
    # One re-open = entering the re-open group from outside it (QA Queue -> Re-Open -> Dev In
    # Process is a single re-open, not two).
    reopens = [m for m in after_qa if m[2] in M["reopen_statuses"] and m[1] not in M["reopen_statuses"]]
    return {
        "reached_qa": qa_at is not None,
        "reopens": len(reopens),
        "failures": sum(1 for _, _, to in moves if to in M["failure_statuses"]),
        "missing_details": sum(1 for _, _, to in moves if to in M["missing_details_statuses"]),
    }


def group_summary(rows: list) -> dict:
    n = len(rows)
    qa = [r for r in rows if r["reached_qa"]]
    return {
        "tickets": n,
        "reached_qa": len(qa),
        "reopen_rate": (sum(1 for r in qa if r["reopens"]) / len(qa)) if qa else None,
        "reopens_per_qa_ticket": (sum(r["reopens"] for r in qa) / len(qa)) if qa else None,
        "failure_rate": (sum(1 for r in rows if r["failures"]) / n) if n else None,
        "missing_details_rate": (sum(1 for r in rows if r["missing_details"]) / n) if n else None,
    }


# ---------------------------------------------------------------------- ticket groups

HANDED_OFF = ("8-handoff", "done")


def workflow_tickets() -> tuple:
    """(handed_off, in_progress). Only tickets that reached hand-off count as workflow results;
    a ticket that was only fetched or is still being built would flatter every rate."""
    done, in_progress = [], []
    for folder in sorted(TICKETS.iterdir()) if TICKETS.is_dir() else []:
        state_path = folder / "state.json"
        if folder.is_dir() and folder.name.isdigit() and state_path.is_file():
            state = json.loads(state_path.read_text(encoding="utf-8"))
            started = min((h.get("at") for h in state.get("history", []) if h.get("at")), default=None)
            row = {"ticket": folder.name, "state": state, "started": started}
            stage = state.get("paused_from") if state.get("stage") == "paused" else state.get("stage")
            (done if stage in HANDED_OFF else in_progress).append(row)
    return done, in_progress


VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def version_of(title: str):
    m = VERSION_RE.search(title or "")
    return tuple(int(x) for x in m.groups()) if m else None


def baseline_milestones(api: AssemblaApi, adoption_version, names: list, me_id: str) -> list:
    """[(milestone, my tickets incl. closed)] for the baseline.

    Due dates are mostly empty and future milestones are pre-created, so milestones are ordered
    by the version in their title: the N highest `milestone_filter` versions BELOW the version the
    workflow was adopted in, that contain at least one of my tickets (closed ones included).
    """
    all_ms = list({m["id"]: m for path in ("/milestones/all.json", "/milestones.json")
                   for m in api._get_paged(api._space_path(path))}.values())
    if names:
        chosen = [m for m in all_ms if m.get("title") in names]
    else:
        candidates = [m for m in all_ms if M["milestone_filter"].lower() in (m.get("title") or "").lower()
                      and version_of(m.get("title")) and (adoption_version is None or version_of(m["title"]) < adoption_version)]
        chosen = sorted(candidates, key=lambda m: version_of(m["title"]), reverse=True)
    picked = []
    for m in chosen[:15]:  # bounded scan
        mine = [t for t in api.get_milestone_tickets(m["id"], all_statuses=True) if t.get("assigned_to_id") == me_id]
        if mine or names:
            picked.append((m, mine))
        if not names and len(picked) == M["baseline_milestones"]:
            break
    return picked


# ---------------------------------------------------------------------- workflow-only measures

def workflow_measures(ticket: str) -> dict:
    events = metrics.read(PROJECT_ROOT, CONFIG["tickets_dir"], ticket)
    reviews = [e for e in events if e["event"] == "review"]
    by = Counter(e["event"] for e in events)
    return {
        "review_rounds": len(reviews),
        "first_round_pass": bool(reviews) and reviews[0].get("verdict") == "PASS",
        "findings": {k: sum(int(e.get(k) or 0) for e in reviews) for k in ("high", "medium", "low", "security")},
        "gate_fails": sum(1 for e in events if e["event"] == "gates" and e.get("status") != "PASS"),
        "db_stops": sum(int(e.get("stops") or 0) for e in events if e["event"] == "db_checks"),
        "db_errors": sum(int(e.get("errors") or 0) for e in events if e["event"] == "db_checks"),
        "risk_bumps": sum(1 for e in events if e["event"] == "risk_check" and e.get("bumped")),
        "fallbacks": by["fallback"],
        "ui_fails_local": sum(int(e.get("failed") or 0) for e in events if e["event"] == "ui_checks" and e.get("where") == "local"),
        "ui_fails_staging": sum(int(e.get("failed") or 0) for e in events if e["event"] == "ui_checks" and e.get("where") == "staging"),
        "stops": Counter(e.get("rule") for e in events if e["event"] == "stop"),
        "attr_fixes": by["attr_fix"],
        "pauses": by["pause"],
    }


# ---------------------------------------------------------------------- report

def pct(v):
    return "–" if v is None else f"{v * 100:.0f}%"


def num(v):
    return "–" if v is None else f"{v:.2f}"


def render(me, wf_rows, wf_summary, base_rows, base_summary, base_ms) -> str:
    L = [f"# dev-ticket quality metrics — {me}", "",
         f"_Generated {date.today().isoformat()} by `metrics_report.py`. Local to this PC (D-53). "
         "Lower is better for every rate below._", ""]
    L += ["## Workflow vs baseline (same measures, from Assembla status history)", "",
          "| Measure | Workflow | Baseline |", "|---|---|---|"]
    b = base_summary or {}
    for label, key, fmt in (("Tickets", "tickets", str), ("Reached QA Queue", "reached_qa", str),
                            ("**QA re-open rate** (tickets re-opened after QA Queue)", "reopen_rate", pct),
                            ("Re-opens per ticket that reached QA", "reopens_per_qa_ticket", num),
                            ("Merge / Build Failed rate", "failure_rate", pct),
                            ("Sent back for missing details", "missing_details_rate", pct)):
        L.append(f"| {label} | {fmt(wf_summary.get(key)) if wf_summary.get(key) is not None else '–'} | "
                 f"{fmt(b.get(key)) if b.get(key) is not None else '–'} |")
    L += ["", "Baseline milestones: " + (", ".join(m.get("title") for m in base_ms) if base_ms else "_none (skipped or not found)_"), ""]

    tot = Counter()
    stops = Counter()
    for r in wf_rows:
        w = r["wf"]
        for k, v in w["findings"].items():
            tot[k] += v
        for k in ("review_rounds", "gate_fails", "db_stops", "db_errors", "risk_bumps", "fallbacks",
                  "ui_fails_local", "ui_fails_staging", "attr_fixes", "pauses"):
            tot[k] += w[k]
        tot["first_round_pass"] += int(w["first_round_pass"])
        stops.update(w["stops"])
    n = len(wf_rows) or 1
    L += ["## Workflow-only quality signals", "",
          "| Signal | Total | Per ticket |", "|---|---|---|",
          f"| Review findings — High / Medium / Low | {tot['high']} / {tot['medium']} / {tot['low']} | "
          f"{tot['high']/n:.2f} / {tot['medium']/n:.2f} / {tot['low']/n:.2f} |",
          f"| Security findings | {tot['security']} | {tot['security']/n:.2f} |",
          f"| Review rounds | {tot['review_rounds']} | {tot['review_rounds']/n:.2f} |",
          f"| First-round review PASS | {tot['first_round_pass']} | {pct(tot['first_round_pass']/n) if wf_rows else '–'} |",
          f"| Gate runs that failed | {tot['gate_fails']} | {tot['gate_fails']/n:.2f} |",
          f"| DB-check stops / errors | {tot['db_stops']} / {tot['db_errors']} | – |",
          f"| Risk bumps (7b) | {tot['risk_bumps']} | – |",
          f"| LOW → STANDARD fallbacks | {tot['fallbacks']} | – |",
          f"| UI-check failures — local / staging | {tot['ui_fails_local']} / {tot['ui_fails_staging']} | – |",
          f"| Staging-attribute fixes | {tot['attr_fixes']} | – |",
          f"| Pauses | {tot['pauses']} | – |", ""]
    L += ["**Stops by rule:** " + (", ".join(f"{k} ×{v}" for k, v in stops.most_common()) or "none"), ""]

    L += ["## Per ticket (workflow)", "",
          "| Ticket | Type | Risk / path | Re-opens | Merge/Build failed | Review H/M/L/Sec | Rounds | Gate fails | Fallback |",
          "|---|---|---|---|---|---|---|---|---|"]
    for r in wf_rows:
        s, w, f = r["state"], r["wf"], r["wf"]["findings"]
        L.append(f"| #{r['ticket']} | {s.get('type') or '–'} | {s.get('risk') or '–'} / {s.get('path') or '–'} | "
                 f"{r['reopens']} | {r['failures']} | {f['high']}/{f['medium']}/{f['low']}/{f['security']} | "
                 f"{w['review_rounds']} | {w['gate_fails']} | {w['fallbacks']} |")
    if base_rows:
        L += ["", "## Per ticket (baseline)", "", "| Ticket | Milestone | Re-opens | Merge/Build failed | Missing details |",
              "|---|---|---|---|---|"]
        for r in base_rows:
            L.append(f"| #{r['ticket']} | {r['milestone']} | {r['reopens']} | {r['failures']} | {r['missing_details']} |")
    L += ["", "_Notes: re-open = a move to one of " + ", ".join(M["reopen_statuses"]) +
          f" after the ticket first reached {M['qa_status']}. Baseline = your own tickets (current assignee) "
          "in the baseline milestones, so small samples swing a lot._", ""]
    return "\n".join(L)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--baseline", nargs="+", metavar="MILESTONE", help="Baseline milestone titles.")
    parser.add_argument("--no-baseline", action="store_true")
    args = parser.parse_args()

    try:
        api = AssemblaApi()
        me = api.get_current_user()
    except AssemblaError as exc:
        print("RESULT: " + json.dumps({"status": "ERROR", "message": str(exc)}))
        return 1

    wf, in_progress = workflow_tickets()
    versions = []
    for r in wf + in_progress:  # adoption = earliest milestone touched by the workflow at all
        snap = TICKETS / r["ticket"] / "ticket.json"
        if snap.is_file():
            mid = (json.loads(snap.read_text(encoding="utf-8")).get("ticket") or {}).get("milestone_id")
            v = version_of(api.get_milestone(mid).get("title")) if mid else None
            if v:
                versions.append(v)
    for r in wf:
        r.update(quality_from_history(status_history(api, r["ticket"])))
        r["wf"] = workflow_measures(r["ticket"])
    adoption_version = min(versions) if versions else None  # earliest milestone worked with the workflow

    base_rows, base_ms = [], []
    if not args.no_baseline:
        wf_numbers = {r["ticket"] for r in wf}
        for ms, mine in baseline_milestones(api, adoption_version, args.baseline, me["id"]):
            base_ms.append(ms)
            for t in mine:
                node = ((t.get("custom_fields") or {}).get(CONFIG["deploy_attributes"]["node_field"]) or "").strip()
                if node == CONFIG["db"]["node_value"]:
                    continue  # the milestone's shared DB ticket isn't a development ticket
                if str(t.get("number")) not in wf_numbers:
                    row = quality_from_history(status_history(api, t["number"]))
                    base_rows.append(dict(row, ticket=t["number"], milestone=ms.get("title")))

    report = render(me.get("login"), wf, group_summary(wf), base_rows,
                    group_summary(base_rows) if base_rows else None, base_ms)
    if in_progress:
        report += ("\n_Not counted yet (not handed off): " +
                   ", ".join(f"#{r['ticket']} ({r['state'].get('stage')})" for r in in_progress) + "._\n")
    TICKETS.mkdir(parents=True, exist_ok=True)
    out = TICKETS / f"METRICS-{date.today().isoformat()}.md"
    out.write_text(report, encoding="utf-8")
    print(f"Wrote {out}")
    print("RESULT: " + json.dumps({"status": "WRITTEN", "file": str(out), "workflow_tickets": len(wf),
                                   "baseline_tickets": len(base_rows),
                                   "baseline_milestones": [m.get("title") for m in base_ms]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
