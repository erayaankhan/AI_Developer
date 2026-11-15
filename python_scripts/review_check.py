"""
Evidence check for the independent code review (dev-ticket, backlog A-10).

Every High, Medium and security finding in the review's findings table must carry the verbatim code line it is about
(the `Evidence` column) next to its `file:line`. This module checks mechanically that the cited file exists, that the
line is in range, and that the excerpt really appears at (or within a few lines of) that line. It cannot judge the
reasoning; it catches invented, misread and shifted citations. Nothing is dropped here: a finding that fails stays in
the report, flagged, and the caller decides what to do with it.
"""

import re
import subprocess
from pathlib import Path

CITE_RE = re.compile(r"([^\s;,|`]+?):(\d+)(?:-(\d+))?")
WINDOW = 3  # lines above and below the cited line in which the excerpt may appear
REQUIRED_SEVERITIES = ("high", "medium")
UNVERIFIED = ("MISSING_EVIDENCE", "BAD_CITATION", "NOT_FOUND")


def split_row(line: str) -> list:
    """Cells of a markdown table row; `\\|` inside a cell is a literal pipe."""
    cells = re.split(r"(?<!\\)\|", line.strip())
    if cells and cells[0].strip() == "":
        cells = cells[1:]
    if cells and cells[-1].strip() == "":
        cells = cells[:-1]
    return [c.strip().replace("\\|", "|") for c in cells]


def parse_findings(text: str) -> list:
    """Rows of the findings table: {num, severity, category, cite, evidence, has_evidence_column}."""
    findings, header = [], None
    for line in text.splitlines():
        if not line.lstrip().startswith("|"):
            if header and findings:
                break  # the table ended
            continue
        cells = split_row(line)
        lowered = [c.lower() for c in cells]
        if "severity" in lowered and "file:line" in lowered:
            header = lowered
            continue
        if header is None or all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
            continue
        cell = lambda name: cells[header.index(name)] if name in header and header.index(name) < len(cells) else ""
        findings.append({"num": cell("#"), "severity": cell("severity"), "category": cell("category"),
                         "cite": cell("file:line"), "evidence": cell("evidence"),
                         "has_evidence_column": "evidence" in header})
    return findings


def normalise(text: str) -> str:
    text = text.strip().strip("`").strip().strip("\"'").strip()
    text = re.sub(r"^(\.\.\.|…)|(\.\.\.|…)$", "", text).strip()
    return re.sub(r"\s+", " ", text)


class Resolver:
    """Turns the path in a citation into a file: as written, in the DB repo (C2M_CI_DB/...), or by unique suffix."""

    def __init__(self, root: Path, db_root: Path):
        self.root, self.db_root, self._files = root, db_root, None

    def _all_files(self) -> list:
        if self._files is None:
            names = []
            for args in (["ls-files"], ["ls-files", "--others", "--exclude-standard"]):
                proc = subprocess.run(["git", "-C", str(self.root), "-c", "core.quotepath=false", *args],
                                      capture_output=True, text=True, encoding="utf-8", errors="replace")
                names += proc.stdout.splitlines()
            self._files = names
        return self._files

    def resolve(self, name: str):
        """(Path or None, problem)."""
        name = name.replace("\\", "/").lstrip("./")
        if name.lower().startswith("c2m_ci_db/"):
            path = self.db_root / name.split("/", 1)[1]
            return (path, "") if path.is_file() else (None, f"{name} not found in the DB repo")
        if (self.root / name).is_file():
            return self.root / name, ""
        matches = [f for f in self._all_files() if f == name or f.endswith("/" + name)]
        if len(matches) == 1:
            return self.root / matches[0], ""
        return None, (f"{name} not found" if not matches else f"{name} is ambiguous ({len(matches)} files)")


def check_finding(resolver: Resolver, finding: dict) -> dict:
    """Status of one finding: OK, NOT_REQUIRED, NOT_CHECKABLE or one of UNVERIFIED."""
    severity = finding["severity"].strip().lower()
    required = severity in REQUIRED_SEVERITIES or "security" in finding["category"].lower()
    evidence = finding["evidence"].strip()
    out = {"num": finding["num"], "severity": finding["severity"], "cite": finding["cite"]}
    if not evidence or evidence in ("—", "-"):
        return {**out, "status": "MISSING_EVIDENCE" if required else "NOT_REQUIRED",
                "detail": "no Evidence excerpt" if required else ""}
    if evidence.lower().startswith("n/a"):
        return {**out, "status": "NOT_CHECKABLE", "detail": evidence[:120]}
    match = CITE_RE.search(finding["cite"])
    if not match:
        return {**out, "status": "BAD_CITATION", "detail": "File:line is not in the form path:line"}
    path, problem = resolver.resolve(match.group(1))
    if path is None:
        return {**out, "status": "BAD_CITATION", "detail": problem}
    start = int(match.group(2))
    end = int(match.group(3) or start)
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if start < 1 or start > len(lines):
        return {**out, "status": "BAD_CITATION", "detail": f"{match.group(1)} has {len(lines)} lines, not {start}"}
    window = normalise(" ".join(lines[max(0, start - 1 - WINDOW):min(len(lines), end + WINDOW)]))
    # a markdown cell escapes a backtick inside the code as \` (a template literal, say): the file has a bare one
    if normalise(evidence) in window or normalise(evidence.replace("\\`", "`")) in window:
        return {**out, "status": "OK", "detail": ""}
    return {**out, "status": "NOT_FOUND",
            "detail": f"the excerpt does not appear within {WINDOW} lines of {match.group(1)}:{start}"}


def check_review(path: Path, root: Path, db_root: Path) -> dict:
    """{checked, ok, unverified: [...], not_checkable: [...]} for one review file."""
    findings = parse_findings(path.read_text(encoding="utf-8", errors="replace"))
    resolver = Resolver(root, db_root)
    results = [check_finding(resolver, f) for f in findings]
    return {"findings": len(results),
            "checked": sum(1 for r in results if r["status"] not in ("NOT_REQUIRED",)),
            "ok": sum(1 for r in results if r["status"] == "OK"),
            "unverified": [r for r in results if r["status"] in UNVERIFIED],
            "not_checkable": [r for r in results if r["status"] == "NOT_CHECKABLE"]}
