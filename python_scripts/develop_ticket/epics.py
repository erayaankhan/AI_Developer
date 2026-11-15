"""Epic candidates for a ticket: dt epics."""

import json
import re
import tempfile
from datetime import datetime
from pathlib import Path

from assembla_api import AssemblaApi, bare_number

from develop_ticket.common import CONFIG, STATUS, result


# ---------------------------------------------------------------------- epics / attribute fixes

STOPWORDS = set("""the and for with from that this into when then than are was were have has not
but all any can will should would could able use used using via per its it's your our their them
they you new add added update updated support story epic defect bug change request issue
ticket page screen user users value values data acceptance criteria expected currently current
between across each other provide both requirements return automatically also such should must
need needs make made only more like""".split())


def _tokens(text: str) -> set:
    return {w for w in re.findall(r"[a-z][a-z0-9]{2,}", (text or "").lower()) if w not in STOPWORDS}


def active_tickets_cached(api: AssemblaApi, refresh: bool = False) -> list:
    """All active tickets in the space (slim), cached for epics.cache_minutes (full scan ~25 s)."""
    cache = Path(tempfile.gettempdir()) / f"dev_ticket_active_{api.space}.json"
    if not refresh and cache.is_file():
        age_min = (datetime.now().timestamp() - cache.stat().st_mtime) / 60
        if age_min < CONFIG["epics"]["cache_minutes"]:
            return json.loads(cache.read_text(encoding="utf-8"))
    keep = ("number", "id", "summary", "status", "milestone_id", "hierarchy_type", "custom_fields", "description")
    tickets = [{k: t.get(k) for k in keep} for t in api.get_active_tickets()]
    for t in tickets:
        t["description"] = (t["description"] or "")[:1500]
    cache.write_text(json.dumps(tickets), encoding="utf-8")
    return tickets


def active_wf_epics(api: AssemblaApi, refresh: bool = False) -> list:
    """Active Epics (Node = WF) from the whole space."""
    cfg = CONFIG["epics"]
    inactive = set(cfg["inactive_statuses"])
    epics = [
        {"ticket": t.get("number"), "id": t.get("id"), "summary": t.get("summary"),
         "status": t.get("status"), "milestone_id": t.get("milestone_id"),
         "module": _area((t.get("custom_fields") or {}).get("C2M-Module")),
         "sub_module": _area((t.get("custom_fields") or {}).get("C2M-Sub-Module")),
         "description": (t.get("description") or "")[:1500]}
        for t in active_tickets_cached(api, refresh)
        if t.get("hierarchy_type") == 3 and t.get("status") not in inactive
        and ((t.get("custom_fields") or {}).get(CONFIG["deploy_attributes"]["node_field"]) or "").strip() == cfg["node_value"]
    ]
    return epics


def _area(value) -> str:
    value = (value or "").strip()
    return "" if value in ("", "NA", "-NA-") else value


def sibling_epic_counts(api: AssemblaApi, ticket: dict, epics: list, max_siblings: int = 30) -> dict:
    """{epic number: how many same-area tickets sit under it} (D-15 functional-area signal).

    Epics rarely carry C2M-Module themselves (36 of 40 blank, 2026-09-29), so the area is
    inferred from where other active tickets with the same C2M-Module (Sub-Module
    preferred) are parented. One association call per sibling, matched against the known
    Epic ids - no per-link ticket lookups.
    """
    cf = ticket.get("custom_fields") or {}
    module, sub = _area(cf.get("C2M-Module")), _area(cf.get("C2M-Sub-Module"))
    if not module:
        return {}
    epic_by_id = {e["id"]: e["ticket"] for e in epics}
    same = [t for t in active_tickets_cached(api)
            if t.get("number") != ticket.get("number") and t.get("hierarchy_type") != 3
            and _area((t.get("custom_fields") or {}).get("C2M-Module")) == module]
    same.sort(key=lambda t: _area((t.get("custom_fields") or {}).get("C2M-Sub-Module")) != sub)
    counts = {}
    for sibling in same[:max_siblings]:
        for assoc in api.get_associations(sibling["number"]):
            for side in (assoc.get("ticket1_id"), assoc.get("ticket2_id")):
                if side in epic_by_id and side != sibling["id"]:
                    counts[epic_by_id[side]] = counts.get(epic_by_id[side], 0) + 1
    return counts


def rank_epics(ticket: dict, epics: list, siblings: dict = None) -> list:
    """Rank Epics by functional-area similarity to the ticket in development (D-14, D-15).

    Functional area comes first: same C2M-Module (+6) and C2M-Sub-Module (+4) on the
    Epic itself, or the module named in the Epic summary (+3). Text overlap is the
    secondary signal (summary words count double), then same milestone (+2) and an Epic
    in development (+1). Claude re-orders by functional area when the fields are
    blank; the developer always picks.
    """
    cf = ticket.get("custom_fields") or {}
    t_module, t_sub = _area(cf.get("C2M-Module")), _area(cf.get("C2M-Sub-Module"))
    module_words = _tokens(f"{t_module} {t_sub}")
    t_summary = _tokens(re.sub(r"^[^|]*\|", "", ticket.get("summary") or ""))
    t_all = t_summary | _tokens((ticket.get("description") or "")[:3000]) | module_words
    ranked = []
    for e in epics:
        e_summary = _tokens(re.sub(r"^[^|]*\|", "", e["summary"] or ""))
        e_all = e_summary | _tokens(e["description"])
        shared_summary = t_summary & e_summary
        shared = t_all & e_all
        score, reasons = 2 * len(shared_summary) + len(shared - shared_summary), []
        used_by = (siblings or {}).get(e["ticket"], 0)
        if used_by:
            score += 3 * min(used_by, 5)
            reasons.append(f"parent of {used_by} other {t_module} ticket(s)")
        if t_module and e.get("module") == t_module:
            score += 6
            reasons.append(f"same module ({t_module})")
        if t_sub and e.get("sub_module") == t_sub:
            score += 4
            reasons.append(f"same sub-module ({t_sub})")
        if module_words and module_words & e_summary:
            score += 3
            reasons.append("module named in Epic")
        if shared:
            reasons.append("shared: " + ", ".join(sorted(shared, key=lambda w: (w not in shared_summary, w))[:6]))
        if e["milestone_id"] and e["milestone_id"] == ticket.get("milestone_id"):
            score += 2
            reasons.append("same milestone")
        if e["status"] == STATUS["in_process"]:
            score += 1
            reasons.append("Epic in development")
        ranked.append({"ticket": e["ticket"], "summary": e["summary"], "status": e["status"],
                       "score": score, "why": "; ".join(reasons) or "-"})
    ranked.sort(key=lambda r: (-r["score"], -int(r["ticket"])))
    return ranked


def cmd_epics(api: AssemblaApi, args) -> int:
    number = bare_number(args.ticket)
    ticket = api.get_ticket(number)
    epics = active_wf_epics(api, refresh=args.refresh)
    ranked = rank_epics(ticket, epics, sibling_epic_counts(api, ticket, epics))
    ranked = ranked[: args.limit or CONFIG["epics"]["candidates_shown"]]
    for i, r in enumerate(ranked, 1):
        print(f"{i:>2}. #{r['ticket']} [{r['status']}] {r['summary']}  (score {r['score']}: {r['why']})")
    return result("CANDIDATES", ticket=number, total_active_wf_epics=len(epics), candidates=ranked)
