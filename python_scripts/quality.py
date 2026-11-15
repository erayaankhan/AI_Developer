"""Sonar and Snyk, the developer's last step before hand-off (`dt quality N`), and the reading of a Jenkins build that
failed on them (`dt quality-bounce`). Config: `quality` (team profile, apps per repo overlay).

What Jenkins does (read from jenkins-build-server-3, 2026-11-11): after Code_Merge_Launchpad_* merges FB_N, each changed
app's job builds it, runs the SonarScanner into the app's real project and fails the build when the project's quality
gate is ERROR (the gate fails on ANY new issue: `new_violations > 0`). Some apps (DeviceMgmtAPI, asset-data-transformer)
also run `snyk test --severity-threshold=medium`, which fails on any vulnerability of Medium or more.

The local check never touches a real Sonar project: it scans into `<project_key>__dev_<login>` (a project of its own per
developer) and keeps only the issues on the lines this ticket changed. Blocking: High / Medium (Blocker included); Low
issues are listed (the Jenkins gate still counts them). Snyk: fixable vulnerabilities (an upgrade or a patch exists)
block; unfixable ones are listed. Tokens come from credentials.env (SONAR_TOKEN, SNYK_TOKEN) and are passed to the tools
through the environment, never on a command line, never printed.
"""
import base64
import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SONAR_SCAN_PACKAGE = "@sonar/scan@5.0.2"
SNYK_PACKAGE = "snyk@1.1308.3"
SONAR_MAVEN_PLUGIN = "org.sonarsource.scanner.maven:sonar-maven-plugin:5.0.0.4389:sonar"
BLOCKING_IMPACTS = {"BLOCKER", "HIGH", "MEDIUM"}
BLOCKING_LEGACY = {"BLOCKER", "CRITICAL", "MAJOR"}
CE_TIMEOUT_SECONDS = 15 * 60
SCAN_TIMEOUT_SECONDS = 30 * 60
DEV_SUFFIX = "__dev_"


# ---------------------------------------------------------------------- config

def settings(config: dict) -> dict:
    return config.get("quality") or {}


def enabled(config: dict) -> bool:
    return bool(settings(config).get("enabled"))


def apps(config: dict) -> dict:
    """{app folder (relative to the repo root): {sonar: {...} | None, snyk: {...} | None}}."""
    return settings(config).get("apps") or {}


def touched_apps(config: dict, files: list) -> list:
    """The configured apps that hold at least one of the changed files (as Jenkins builds only the changed apps)."""
    folders = sorted(apps(config), key=len, reverse=True)
    hit = []
    for name in files:
        for folder in folders:
            if name == folder or name.startswith(folder.rstrip("/") + "/"):
                if folder not in hit:
                    hit.append(folder)
                break
    return sorted(hit)


def dev_project_key(project_key: str, login: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", login or "dev")
    return f"{project_key}{DEV_SUFFIX}{safe}"


# ---------------------------------------------------------------------- the ticket's changed lines

HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.M)


def parse_unified_zero(diff_text: str) -> dict:
    """{file: set(new-side line numbers)} from `git diff -U0` output."""
    lines, current = {}, None
    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            path = line[4:].strip()
            current = None if path == "/dev/null" else re.sub(r"^b/", "", path)
            if current:
                lines.setdefault(current, set())
            continue
        m = HUNK_RE.match(line)
        if m and current:
            start, count = int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1)
            lines[current].update(range(start, start + count))
    return lines


def changed_lines(repo: Path, base: str, files: list) -> dict:
    """The lines this ticket adds or changes, committed (origin/base...HEAD) and not yet committed (HEAD). A new file
    that git does not track yet counts with all its lines."""
    out = {}
    for spec in ([f"origin/{base}...HEAD"], ["HEAD"]):
        proc = subprocess.run(["git", "-C", str(repo), "diff", "-U0", "--no-color", *spec, "--", *files],
                              capture_output=True, text=True, encoding="utf-8", errors="replace")
        for name, nums in parse_unified_zero(proc.stdout).items():
            out.setdefault(name, set()).update(nums)
    for name in files:
        if name not in out and (repo / name).is_file():
            tracked = subprocess.run(["git", "-C", str(repo), "ls-files", "--error-unmatch", name],
                                     capture_output=True, text=True).returncode == 0
            if not tracked:
                try:
                    count = len((repo / name).read_text(encoding="utf-8", errors="replace").splitlines())
                except OSError:
                    count = 0
                out[name] = set(range(1, count + 1))
    return out


# ---------------------------------------------------------------------- SonarQube

class SonarClient:
    def __init__(self, host: str, token: str, opener=urllib.request.urlopen):
        self.host, self.token, self.opener = host.rstrip("/"), token, opener

    def get(self, path: str, query: dict = None) -> dict:
        url = self.host + path + ("?" + urllib.parse.urlencode(query) if query else "")
        auth = base64.b64encode((self.token + ":").encode()).decode()
        request = urllib.request.Request(url, headers={"Authorization": "Basic " + auth})
        with self.opener(request, timeout=60) as response:
            return json.load(response)

    def wait_task(self, task_id: str, sleep=time.sleep, clock=time.time) -> str:
        deadline = clock() + CE_TIMEOUT_SECONDS
        while clock() < deadline:
            status = (self.get("/api/ce/task", {"id": task_id}).get("task") or {}).get("status")
            if status in ("SUCCESS", "FAILED", "CANCELED"):
                return status
            sleep(5)
        return "TIMEOUT"

    def issues(self, project_key: str, new_code_only: bool = False) -> list:
        found, page = [], 1
        while True:
            query = {"componentKeys": project_key, "resolved": "false", "ps": 500, "p": page}
            if new_code_only:
                query["inNewCodePeriod"] = "true"
            data = self.get("/api/issues/search", query)
            found += data.get("issues") or []
            if page * 500 >= min(int(data.get("total") or 0), 10000):
                return found
            page += 1


def issue_view(issue: dict, project_key: str, app_folder: str) -> dict:
    """The facts of one Sonar issue, with its file as a repo path."""
    component = str(issue.get("component") or "")
    rel = component.split(":", 1)[1] if component.startswith(project_key + ":") else component.split(":", 1)[-1]
    impacts = [str(i.get("severity") or "").upper() for i in issue.get("impacts") or []]
    legacy = str(issue.get("severity") or "").upper()
    blocking = bool(set(impacts) & BLOCKING_IMPACTS) if impacts else legacy in BLOCKING_LEGACY
    order = ["BLOCKER", "HIGH", "MEDIUM", "LOW", "INFO"]
    level = min(impacts, key=lambda s: order.index(s) if s in order else 9) if impacts else legacy
    return {"file": f"{app_folder.rstrip('/')}/{rel}" if app_folder else rel, "line": issue.get("line"),
            "severity": level, "blocking": blocking, "rule": issue.get("rule"), "message": issue.get("message"),
            "type": issue.get("type")}


def on_changed_lines(views: list, lines: dict) -> list:
    """Only the issues this ticket can be blamed for: on a changed line, or file-level in a changed file."""
    kept = []
    for v in views:
        changed = lines.get(v["file"])
        if changed is None:
            continue
        if v["line"] is None or int(v["line"]) in changed:
            kept.append(v)
    return kept


def sonar_scan_command(app_cfg: dict, dev_key: str, host: str, base_dir: Path) -> list:
    """The SonarScanner (npm @sonar/scan) with the same properties Jenkins passes. The token goes in SONAR_TOKEN."""
    sonar = app_cfg.get("sonar") or {}
    props = {"sonar.host.url": host, "sonar.projectKey": dev_key, "sonar.projectName": dev_key,
             "sonar.sourceEncoding": "UTF-8", "sonar.projectBaseDir": str(base_dir),
             "sonar.sources": sonar.get("sources") or "."}
    if sonar.get("exclusions"):
        props["sonar.exclusions"] = sonar["exclusions"]
    for key, value in (sonar.get("properties") or {}).items():
        props[key] = value
    npx = shutil.which("npx") or "npx"
    return [npx, "--yes", SONAR_SCAN_PACKAGE] + [f"-D{k}={v}" for k, v in props.items()]


def dotnet_scan_commands(app_cfg: dict, dev_key: str, host: str, base_dir: Path) -> list:
    """SonarScanner for .NET: begin, build, end (needs `dotnet tool install -g dotnet-sonarscanner`)."""
    sonar = app_cfg.get("sonar") or {}
    project = sonar.get("build_target") or "."
    return [["dotnet", "sonarscanner", "begin", f"/k:{dev_key}", f"/n:{dev_key}", f"/d:sonar.host.url={host}",
             "/d:sonar.sourceEncoding=UTF-8"],
            ["dotnet", "build", project, "--nologo", "-v", "q"],
            ["dotnet", "sonarscanner", "end"]]


def maven_scan_command(app_cfg: dict, dev_key: str, host: str) -> list:
    """Compile (tests skipped) and run the Sonar Maven plugin, as the Jenkins Maven jobs do. The token goes in
    MAVEN_OPTS (-Dsonar.token), never on the command line."""
    sonar = app_cfg.get("sonar") or {}
    props = {"sonar.host.url": host, "sonar.projectKey": dev_key, "sonar.projectName": dev_key,
             "sonar.sourceEncoding": "UTF-8"}
    if sonar.get("exclusions"):
        props["sonar.exclusions"] = sonar["exclusions"]
    for key, value in (sonar.get("properties") or {}).items():
        props[key] = value
    mvn = shutil.which("mvn") or "mvn"
    return [mvn, "-B", "-q", "-DskipTests", "compile", SONAR_MAVEN_PLUGIN] + [f"-D{k}={v}" for k, v in props.items()]


def report_task_id(base_dir: Path, scanner: str):
    """The Compute Engine task id the scanner wrote (report-task.txt)."""
    candidates = {"dotnet": [base_dir / ".sonarqube" / "out" / ".sonar" / "report-task.txt"],
                  "maven": [base_dir / "target" / "sonar" / "report-task.txt"]}.get(
                      scanner, [base_dir / ".scannerwork" / "report-task.txt"])
    for path in candidates:
        if path.is_file():
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("ceTaskId="):
                    return line.split("=", 1)[1].strip()
    return None


def run_sonar(app_folder: str, app_cfg: dict, repo: Path, login: str, host: str, token: str, lines: dict,
              client=None, runner=subprocess.run, tool_ok=None) -> dict:
    """Scan one app into the developer's own project and return its issues on the ticket's changed lines."""
    sonar = app_cfg.get("sonar") or {}
    scanner = sonar.get("scanner") or "cli"
    dev_key = dev_project_key(sonar.get("project_key") or app_folder, login)
    base = repo / app_folder
    out = {"app": app_folder, "dev_project": dev_key, "scanner": scanner}
    if scanner not in ("cli", "dotnet", "maven"):
        return dict(out, status="NOT_SUPPORTED", message=f"Scanner '{scanner}' is not run locally; Jenkins checks it.")
    if scanner == "maven" and not (tool_ok or (lambda: bool(shutil.which("mvn"))))():
        return dict(out, status="TOOL_MISSING", message="Install Maven (mvn) to run the Sonar scan of this Java app.")
    if scanner == "dotnet" and not (tool_ok or _dotnet_sonarscanner_installed)():
        return dict(out, status="TOOL_MISSING",
                    message="Install the .NET scanner once: dotnet tool install -g dotnet-sonarscanner")
    env = dict(os.environ, SONAR_TOKEN=token, SONAR_HOST_URL=host)
    if scanner == "maven":
        env["MAVEN_OPTS"] = (env.get("MAVEN_OPTS", "") + f" -Dsonar.token={token}").strip()
    commands = ([sonar_scan_command(app_cfg, dev_key, host, base)] if scanner == "cli"
                else [maven_scan_command(app_cfg, dev_key, host)] if scanner == "maven"
                else dotnet_scan_commands(app_cfg, dev_key, host, base))
    for command in commands:
        try:
            proc = runner(command, cwd=str(base), env=env, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=SCAN_TIMEOUT_SECONDS)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return dict(out, status="SCAN_FAILED", message=f"{type(exc).__name__}: {str(exc)[:200]}")
        if proc.returncode != 0:
            tail = [ln for ln in (proc.stdout + "\n" + proc.stderr).splitlines() if "ERROR" in ln or "error" in ln][-5:]
            return dict(out, status="SCAN_FAILED", step=" ".join(command[:3]), message="\n".join(tail)[-800:])
    client = client or SonarClient(host, token)
    task = report_task_id(base, scanner)
    if task:
        status = client.wait_task(task)
        if status != "SUCCESS":
            return dict(out, status="SCAN_FAILED", message=f"SonarQube processing ended {status}.")
    views = [issue_view(i, dev_key, app_folder) for i in client.issues(dev_key)]
    mine = on_changed_lines(views, lines)
    blocking = [v for v in mine if v["blocking"]]
    return dict(out, status="FAIL" if blocking else "PASS", blocking=blocking,
                low=[v for v in mine if not v["blocking"]], total_in_project=len(views),
                dashboard=f"{host.rstrip('/')}/dashboard?id={urllib.parse.quote(dev_key)}")


def _dotnet_sonarscanner_installed() -> bool:
    try:
        proc = subprocess.run(["dotnet", "tool", "list", "-g"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return "dotnet-sonarscanner" in proc.stdout.lower()


# ---------------------------------------------------------------------- Snyk

SNYK_THRESHOLDS = ("low", "medium", "high", "critical")


def snyk_command(target_file: str, threshold: str = "medium") -> list:
    """`snyk test` at the app's Jenkins threshold (snyk.severity_threshold; MAI-DC jobs mostly use low)."""
    exe = shutil.which("snyk")
    base = [exe] if exe else [shutil.which("npx") or "npx", "--yes", SNYK_PACKAGE]
    threshold = threshold if threshold in SNYK_THRESHOLDS else "medium"
    return base + ["test", "--json", f"--severity-threshold={threshold}", f"--file={target_file}"]


def parse_snyk(text: str) -> dict:
    """{fixable, unfixable, error} from `snyk test --json` (one object, or a list of them for several projects)."""
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return {"error": (text or "").strip()[:300] or "no output"}
    results = data if isinstance(data, list) else [data]
    fixable, unfixable, errors, seen = [], [], [], set()
    for res in results:
        if res.get("error") and not res.get("vulnerabilities"):
            errors.append(str(res.get("error"))[:300])
            continue
        for v in res.get("vulnerabilities") or []:
            key = (v.get("id"), v.get("packageName"), v.get("version"))
            if key in seen:
                continue
            seen.add(key)
            upgrade = [p for p in (v.get("upgradePath") or []) if p][-1:] if v.get("isUpgradable") else []
            view = {"id": v.get("id"), "title": v.get("title"), "severity": v.get("severity"),
                    "package": f"{v.get('packageName')}@{v.get('version')}", "fixed_in": v.get("fixedIn") or [],
                    "upgrade_to": upgrade[0] if upgrade else None, "patchable": bool(v.get("isPatchable")),
                    "path": " > ".join((v.get("from") or [])[:4])}
            (fixable if v.get("isUpgradable") or v.get("isPatchable") else unfixable).append(view)
    return {"fixable": fixable, "unfixable": unfixable, "error": "; ".join(errors) or None}


def run_snyk(app_folder: str, app_cfg: dict, repo: Path, token: str, runner=subprocess.run) -> dict:
    snyk = app_cfg.get("snyk") or {}
    base = repo / app_folder
    out = {"app": app_folder, "file": snyk.get("file")}
    env = dict(os.environ)
    if token:
        env["SNYK_TOKEN"] = token
    if snyk.get("prepare"):
        try:
            prep = runner(snyk["prepare"], cwd=str(base), env=env, capture_output=True, text=True, timeout=SCAN_TIMEOUT_SECONDS)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return dict(out, status="SCAN_FAILED", message=f"prepare: {type(exc).__name__}")
        if prep.returncode != 0:
            return dict(out, status="SCAN_FAILED", message=f"prepare ({' '.join(snyk['prepare'])}) failed: "
                                                            f"{(prep.stderr or prep.stdout)[-300:]}")
    try:
        proc = runner(snyk_command(snyk.get("file") or ".", str(snyk.get("severity_threshold") or "medium").lower()), cwd=str(base), env=env, capture_output=True, text=True,
                      encoding="utf-8", errors="replace", timeout=SCAN_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return dict(out, status="SCAN_FAILED", message=f"{type(exc).__name__}: {str(exc)[:200]}")
    if proc.returncode not in (0, 1):  # 0 clean, 1 vulnerabilities found, 2 failure, 3 no supported project
        text = (proc.stdout + proc.stderr)
        status = "NOT_AUTHENTICATED" if re.search(r"(?i)auth|401|SNYK-0005", text) else "SCAN_FAILED"
        parsed = parse_snyk(proc.stdout)
        return dict(out, status=status, message=(parsed.get("error") or text.strip()[-300:]),
                    **({"hint": "Add SNYK_TOKEN to credentials.env (Snyk > Account settings > Auth token)."}
                       if status == "NOT_AUTHENTICATED" else {}))
    parsed = parse_snyk(proc.stdout)
    if parsed.get("error") and not (parsed.get("fixable") or parsed.get("unfixable")):
        return dict(out, status="SCAN_FAILED", message=parsed["error"])
    return dict(out, status="FAIL" if parsed["fixable"] else "PASS", fixable=parsed["fixable"],
                unfixable=parsed["unfixable"])


# ---------------------------------------------------------------------- a Jenkins build that failed (bounce)

GATE_FAILED_RE = re.compile(r"Quality Gate failed", re.I)
GATE_JSON_RE = re.compile(r'^\s*(?:set RAW_JSON=)?(\{"projectStatus".*\})\s*$', re.M)
SNYK_FAILED_RE = re.compile(r"Snyk has detected security vulnerabilities|ERROR: Snyk", re.I)
SNYK_SUMMARY_RE = re.compile(r"(\d+) known vulnerabilit\w* \| (\d+) dependencies|Tested (\d+) dependencies for known issues, "
                             r"found (\d+) issues?", re.I)
PROJECT_KEY_RE = re.compile(r"(?:-Dsonar\.projectKey=|/k:)(\S+)")
MERGE_TICKET_RE = r"Creating merge request for ticket #\s*{n}\b"


def classify_build_console(text: str) -> dict:
    """Why one app build failed: SONAR (quality gate), SNYK, or OTHER. Never returns console lines (secrets)."""
    reasons, conditions = [], []
    if GATE_FAILED_RE.search(text):
        reasons.append("SONAR")
        for raw in GATE_JSON_RE.findall(text):
            try:
                status = json.loads(raw).get("projectStatus") or {}
            except ValueError:
                continue
            conditions = [{"metric": c.get("metricKey"), "actual": c.get("actualValue"),
                           "threshold": c.get("errorThreshold"), "comparator": c.get("comparator")}
                          for c in status.get("conditions") or [] if c.get("status") == "ERROR"]
    snyk = None
    if SNYK_FAILED_RE.search(text):
        reasons.append("SNYK")
        m = SNYK_SUMMARY_RE.search(text)
        if m:
            snyk = {"vulnerabilities": int(m.group(1) or m.group(4)), "dependencies": int(m.group(2) or m.group(3))}
    key = PROJECT_KEY_RE.search(text)
    return {"reasons": reasons or ["OTHER"], "gate_conditions": conditions, "snyk": snyk,
            "sonar_project": key.group(1) if key else None}


def find_merge_build(jenkins, jobs: list, number: str, before_ms: int, window_hours: int = 12):
    """The merge job run that merged ticket N last (at or before `before_ms`): {job, number, start, end} or None."""
    best = None
    pattern = re.compile(MERGE_TICKET_RE.format(n=re.escape(str(number))))
    for job in jobs:
        path = "/job/" + urllib.parse.quote(job, safe="")
        data = jenkins._get_json(path + "/api/json?tree=builds[number,timestamp,duration]{0,80}") or {}
        for b in data.get("builds") or []:
            start = int(b.get("timestamp") or 0)
            if not (before_ms - window_hours * 3600_000 <= start <= before_ms):
                continue
            if best and start <= best["start"]:
                continue
            _, text = jenkins._request(f"{path}/{b['number']}/consoleText")
            if pattern.search(text or ""):
                end = start + int(b.get("duration") or 0) or before_ms
                best = {"job": job, "number": b["number"], "start": start, "end": max(end, start)}
    return best


def find_app_build(jenkins, app_job: str, start_ms: int, end_ms: int):
    """The app job's build that started while the merge run was running (the latest such one)."""
    path = "/job/" + urllib.parse.quote(app_job, safe="")
    data = jenkins._get_json(path + "/api/json?tree=builds[number,timestamp,result]{0,40}") or {}
    inside = [b for b in data.get("builds") or [] if start_ms <= int(b.get("timestamp") or 0) <= end_ms + 60_000]
    return max(inside, key=lambda b: int(b.get("timestamp") or 0)) if inside else None
