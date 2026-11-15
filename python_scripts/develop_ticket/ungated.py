"""Files a branch would bring to master that no gates run or DB-file list covered (improvements item 20)."""

import hashlib
from pathlib import Path

from develop_ticket import common
from develop_ticket.common import CONFIG, branch_name

SHOWN = 20


def ungated_files(number: str, state: dict, db: bool = False) -> dict:
    """What merging origin/FB_N brings that is not in the gated files (`state.gates.files`; for a DB ticket the DB repo's
    `state.db.files`), documentation files aside. -> {} when nothing extra, {"error": msg} when it could not be
    worked out, else {count, folders, files (first 20), token}. The deploy merges the whole branch, so a commit pushed
    after the gates run, or a master merge, rides along unreviewed. `token` binds an acceptance to this exact tip and
    list: any later push gives a new one."""
    from develop_ticket.review import _is_doc_path  # lazy: review imports this module
    repo: Path = common.DB_REPO if db else None
    branch = branch_name(number)
    base = CONFIG["db"]["base_branch"] if db else CONFIG["base_branch"]
    expected = (state.get("db") or {}).get("files") if db else (state.get("gates") or {}).get("files")
    gated = {f.replace("\\", "/") for f in expected or []}
    try:
        common.git("fetch", "--quiet", "origin", base, branch, check=False, repo=repo)
        diff = common.git("diff", "--name-only", f"origin/{base}...origin/{branch}", repo=repo).stdout
        tip = common.git("rev-parse", f"origin/{branch}", repo=repo).stdout.strip()
    except Exception as exc:  # noqa: BLE001 - git failure or timeout: the caller refuses, never guesses
        return {"error": f"{type(exc).__name__}: {exc}"[:200]}
    extra = sorted(f for f in {n.strip().replace("\\", "/") for n in diff.splitlines()} - gated
                   if f and not _is_doc_path(f))
    if not extra:
        return {}
    token = hashlib.sha256("\n".join([tip, *extra]).encode("utf-8")).hexdigest()[:8]
    return {"count": len(extra), "folders": sorted({f.split("/")[0] for f in extra}), "files": extra[:SHOWN],
            "token": token}
