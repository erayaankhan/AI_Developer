"""
Quality-metrics event log for the dev-ticket workflow (D-53).

Each ticket gets an append-only `DEV_TICKETS/{N}/metrics.jsonl`: one JSON object per line,
{"at": ISO-UTC, "event": <type>, ...fields}. Local only, per developer (decision: metrics stay on
the developer's PC). `metrics_report.py` reads these files plus Assembla status history.

Events written automatically by the scripts:
    gates        local_gates.py --ticket   status, failed (gate names)
    risk_check   risk_check.py --ticket    min_tier, bumped
    db_checks    db_checks.py --ticket     status, errors, stops, warnings
    attr_fix     dev_ticket.py fix-attributes --yes   fixes
    status       dev_ticket.py status --yes           previous, current
    merge_master dev_ticket.py merge-master         status (PREVIEW), behind, files
    pause/resume dev_ticket.py handover / restore
    deploy       staging_deploy.py deploy             status
Events Claude records with `dev_ticket.py event N <type> k=v ...` (only it knows them):
    stop, review, ui_checks, fallback, fix_round, questions  (see EVENT_FIELDS)
"""

import json
from datetime import datetime, timezone
from pathlib import Path

# Allowed fields per Claude-recorded event type (values are validated loosely: ints or short text).
EVENT_FIELDS = {
    "stop":      {"rule", "reason"},                       # a stop-and-ask rule fired (S1..S10, S6a..)
    "review":    {"round", "verdict", "high", "medium", "low", "security", "unverified"},
    "ui_checks": {"where", "passed", "failed"},            # where = local | staging
    "fallback":  {"reason"},                               # LOW -> STANDARD
    "fix_round": {"step", "attempt"},                      # 6 build / 7a gates / 7d ui / 7e fixing a review finding
    "questions": {"count", "step"},                        # questions asked to the developer
}
INT_FIELDS = {"round", "high", "medium", "low", "security", "passed", "failed", "attempt", "count"}


def ticket_dir(project_root: Path, tickets_dir: str, ticket: str) -> Path:
    return Path(project_root) / tickets_dir / str(ticket)


def record(project_root: Path, tickets_dir: str, ticket: str, event: str, **fields) -> bool:
    """Append one event. Silently skipped when the ticket has no work folder (ad-hoc runs)."""
    folder = ticket_dir(project_root, tickets_dir, ticket)
    if not ticket or not folder.is_dir():
        return False
    row = {"at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "event": event}
    row.update({k: v for k, v in fields.items() if v is not None})
    with (folder / "metrics.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return True


def read(project_root: Path, tickets_dir: str, ticket: str) -> list:
    path = ticket_dir(project_root, tickets_dir, ticket) / "metrics.jsonl"
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def parse_claude_event(event: str, pairs: list) -> tuple:
    """Validate `event N <type> k=v ...` input → (fields, error)."""
    if event not in EVENT_FIELDS:
        return None, f"unknown event '{event}'; allowed: {sorted(EVENT_FIELDS)}"
    fields = {}
    for pair in pairs:
        if "=" not in pair:
            return None, f"'{pair}' is not key=value"
        key, value = (s.strip() for s in pair.split("=", 1))
        if key not in EVENT_FIELDS[event]:
            return None, f"'{key}' isn't a field of '{event}'; allowed: {sorted(EVENT_FIELDS[event])}"
        if key in INT_FIELDS:
            if not value.isdigit():
                return None, f"'{key}' must be a whole number"
            value = int(value)
        elif len(value) > 200:
            return None, f"'{key}' is too long (max 200 chars)"
        fields[key] = value
    return fields, None
