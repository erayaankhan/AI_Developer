"""
Baseline test run for a freshly created branch (dev-ticket, backlog A-4).

A new ticket starts from a green tree: the configured unit-test projects must pass on the commit the branch was
created from. This module runs them with a TRX logger and groups any failures by cause (exception type + first
message line), so the developer can be shown WHY they fail and decide whether each group is a stale test or a code
problem. It decides nothing itself; dev_ticket.py records the outcome and guards the stage change.
"""

import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

NS = {"t": "http://microsoft.com/schemas/VisualStudio/TeamTest/2010"}
FRAMEWORK_FRAME = re.compile(r"^at (System|Microsoft|Moq|Xunit|FluentAssertions|Castle|NUnit)\b")
MAX_GROUPS = 15
MAX_SAMPLES = 3


def signature(message: str) -> str:
    """First line of a failure message with ids and numbers masked, so failures with one cause group together."""
    text = (message or "").strip()
    first = text.splitlines()[0] if text else "(no message)"
    first = re.sub(r"\b[0-9a-f]{8}-[0-9a-f-]{27}\b", "<guid>", first)
    first = re.sub(r"\d{2,}", "<n>", first)
    return first[:140]


def product_frame(stack: str) -> str:
    """First stack frame that is not framework code (the place to look first), shortened."""
    for line in (stack or "").splitlines():
        line = line.strip()
        if line.startswith("at ") and not FRAMEWORK_FRAME.match(line):
            return re.sub(r"\s+in\s+.*$", "", line)[:150]
    return ""


def parse_trx(path: Path) -> dict:
    """{total, passed, failed, groups: [{count, signature, tests, frame}]} from a TRX file."""
    root = ET.parse(path).getroot()
    counters = root.find("t:ResultSummary/t:Counters", NS).attrib
    groups = {}
    for res in root.iterfind(".//t:UnitTestResult", NS):
        if res.get("outcome") != "Failed":
            continue
        err = res.find("t:Output/t:ErrorInfo", NS)
        message = err.findtext("t:Message", default="", namespaces=NS) if err is not None else ""
        stack = err.findtext("t:StackTrace", default="", namespaces=NS) if err is not None else ""
        group = groups.setdefault(signature(message), {"count": 0, "tests": [], "frame": product_frame(stack)})
        group["count"] += 1
        if len(group["tests"]) < MAX_SAMPLES:
            group["tests"].append(res.get("testName", "").split("(")[0])
    ordered = sorted(({"signature": sig, **g} for sig, g in groups.items()), key=lambda g: -g["count"])
    return {"total": int(counters.get("total", 0)), "passed": int(counters.get("passed", 0)),
            "failed": int(counters.get("failed", 0)), "groups": ordered[:MAX_GROUPS]}


def run_project(root: Path, project: str, out_dir: Path, timeout: int, secret_env: tuple) -> dict:
    """dotnet test one project with TRX output. ERROR when no TRX was produced (build failure, timeout)."""
    name = Path(project).stem
    trx = out_dir / f"{name}.trx"
    exe = shutil.which("dotnet")
    if not exe:
        return {"project": name, "status": "ERROR", "message": "'dotnet' was not found on PATH"}
    if not (root / project).is_file():
        return {"project": name, "status": "ERROR", "message": f"{project} does not exist"}
    out_dir.mkdir(parents=True, exist_ok=True)
    trx.unlink(missing_ok=True)
    env ={k: v for k, v in os.environ.items() if k not in secret_env}
    cmd = [exe, "test", str(root / project), "-nologo", "-v", "q",
           "--logger", f"trx;LogFileName={trx.name}", "--results-directory", str(out_dir)]
    try:
        proc = subprocess.run(cmd, cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"project": name, "status": "ERROR", "message": f"timed out after {timeout}s"}
    log = out_dir / f"{name}.log"
    log.write_text((proc.stdout or "") + (proc.stderr or ""), encoding="utf-8")
    if not trx.is_file():
        errors = [l.strip() for l in (proc.stdout or "").splitlines() if "error" in l.lower()][:8]
        return {"project": name, "status": "ERROR", "message": "no test results (build failed?)",
                "errors": errors, "log": str(log)}
    parsed = parse_trx(trx)
    return {"project": name, "status": "FAIL" if parsed["failed"] else "PASS", "log": str(log), **parsed}


def overall_status(results: list) -> str:
    """PASS only when every result passed; ERROR when any produced no results (or was skipped), else FAIL."""
    if any(r["status"] in ("ERROR", "SKIPPED") for r in results):
        return "ERROR"
    return "FAIL" if any(r["status"] == "FAIL" for r in results) else "PASS"


def run_baseline(root: Path, projects: list, out_dir: Path, timeout: int, secret_env: tuple) -> list:
    """dotnet test each project (repo-relative .csproj paths) -> a list of per-project results."""
    return [run_project(root, p, out_dir, timeout, secret_env) for p in projects]
