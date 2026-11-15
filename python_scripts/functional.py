"""Helpers for the functional checks (step 7d): profile choice, local env file, guards.

The local env file (config functional_checks.local_env_file, tracked in git: URLs, the test-DB allow-list and service
commands only, never a connection string or password) holds the developer's values:

    cloud_staging.<environmentTsKey>=<url>    e.g. cloud_staging.GatewayAPIUrl=https://...
    cloud_preprod.<environmentTsKey>=<url>    same as cloud_staging: the UI against the Preprod APIs
    local.<environmentTsKey>=<url>            e.g. local.GatewayAPIUrl=http://localhost:5000
    local.db.connection=<test DB connection string>     (optional, older way) -> ConnectionStrings__Connection env var
    local.db.allowed_host=<host[,host]>       the only DB host(s)/database(s) a service or DB script may use
    local.db.allowed_name=<database[,database]>
    local.env.<VAR>=<value>                   extra environment variables for started services

appsettings values (connection strings included) live in git-ignored overlay files next to each service's appsettings.json:
    <service folder>/appsettings.<profile>.json     e.g. appsettings.local.json, a nested JSON object
`dt env-apply` merges them into the service's appsettings.json (existing keys only), after snapshotting the original;
`dt env-restore` puts it back. ConnectionStrings are accepted for the local profile only and must match the allow-list.
    service.<name>.cwd=<folder relative to the repo root>
    service.<name>.cmd=<command line>
    service.<name>.url=<health URL; host:port is probed>

Nothing here ever prints a value: results carry key names and booleans only.
"""
import os
import re
import socket
import subprocess
import urllib.parse as urlparse
from pathlib import Path

BACKEND_SUFFIXES = (".cs", ".csproj", ".sln", ".sql", ".config", ".pubxml")


def load_env_file(path: Path) -> dict:
    """key -> value from the local env file; {} when it does not exist. Blank lines and # comments are skipped."""
    values = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def read_role(path: Path, key: str, allowed: list):
    """The developer's remembered role from the local env file, or None when unset or not an allowed value."""
    value = load_env_file(path).get(key, "").strip().lower()
    return value if value in allowed else None


def set_env_key(path: Path, key: str, value: str) -> None:
    """Set one key in the local env file: replace its line, else append it. Every other line is kept as it is."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    new_line = f"{key}={value}"
    for index, line in enumerate(lines):
        if line.strip().split("=", 1)[0].strip() == key and "=" in line and not line.strip().startswith("#"):
            lines[index] = new_line
            break
    else:
        lines.append(new_line)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def profile_for(files: list, angular_roots: list) -> str:
    """'cloud_staging' when every changed file is Angular code, 'local' for anything else, 'none' when nothing changed."""
    if not files:
        return "none"
    for name in files:
        in_angular = any(name.startswith(root) for root in angular_roots)
        if not in_angular or name.lower().endswith(BACKEND_SUFFIXES):
            return "local"
    return CLOUD_STAGING


def plan_check_ids(plan_text: str) -> list:
    """Ids (FC-n, FC-nb or UI-n) of the checks listed under '## Functional checks' (or the older '## UI checks').
    A letter suffix makes its own check: FC-5 and FC-5b are counted separately."""
    match = re.search(r"^##\s+(?:Functional|UI) checks\s*$(.*?)(?=^##\s|\Z)", plan_text, re.M | re.S | re.I)
    if not match:
        return []
    return sorted(set(re.findall(r"\b(?:FC|UI)-\d+[A-Za-z]?\b", match.group(1))))


def plan_checks_section(plan_text: str) -> str:
    """Body of the '## Functional checks' (or older '## UI checks') section of PLAN.md, verbatim; '' when absent."""
    match = re.search(r"^##\s+(?:Functional|UI) checks\s*$(.*?)(?=^##\s|\Z)", plan_text, re.M | re.S | re.I)
    return match.group(1).strip() if match else ""


def parse_result_line(text: str):
    """(passed, failed) from the last `passed X failed Y` line of a returned results file; None when there is none."""
    found = re.findall(r"(?im)^\W*passed\s+(\d+)\s+failed\s+(\d+)\W*$", text)
    return (int(found[-1][0]), int(found[-1][1])) if found else None


def desktop_brief(number: str, profile: str, checks: str, out_path: str) -> str:
    """Self-contained brief for running the planned checks in Claude Desktop (Chrome) when this session cannot."""
    where = {CLOUD_STAGING: "the staging APIs (Angular-only change)",
             CLOUD_PREPROD: "the Preprod APIs (Angular-only change)"}.get(
        profile, "local services with the test DB (see the Setup below)")
    return f"""# Functional checks — #{number}

Run in Chrome (Claude Desktop) against {where}. Do NOT read the code diff; judge only what the screen shows.

## Rules
- Sign in yourself in the Chrome window; never ask for or type a password.
- The environment may be shared: create only what the checks below name, never edit or delete anything else, and remove what you created at the end.
- **Save the results file as exactly** `{out_path}` (create the `reviews` folder if missing; screenshots next to it). If the browser session cannot write there, save it in Downloads and tell the main thread the path.
  Contents: a table with one row per check (PASS/FAIL, what was seen, screenshot name), then a final line exactly `passed X failed Y`.
- Every planned check (FC-n) must appear in the table and be counted in the totals.

{checks}

## Afterwards (main thread, not the browser agent)
`dt func-record {number} --from {out_path}` then continue at step 7e.
"""


def gate_problem(state: dict, fingerprint_now):
    """Hand-off gate for the functional checks: None when allowed, else (RESULT status, message).
    `fingerprint_now` is a callable (computed only when a run has to be matched against the files)."""
    if state.get("env_patched"):
        return ("ENV_PATCHED", "environment.ts / appsettings.json are still patched for a profile: "
                "run `dt env-restore N` before hand-off.")
    mode = state.get("functional_mode")
    if mode == "skip":
        if (state.get("functional_skip") or {}).get("reason"):
            return None
        return "FUNCTIONAL_MODE_REQUIRED", "A skip needs the developer's reason: `dt func-mode N skip --reason … --confirmed`."
    if mode != "run":
        return ("FUNCTIONAL_MODE_REQUIRED",
                "Ask the developer once: run the functional checks, skip them, or decide later? "
                "Record it with `dt func-mode N run|skip|later` (skip needs --reason and --confirmed).")
    func = state.get("functional") or {}
    if func.get("status") != "PASS":
        return ("FUNCTIONAL_REQUIRED",
                "Run the functional checks (UI_CHECKS.md) and record them with `dt func-record N`; FAIL does not count. "
                "To skip them instead: `dt func-mode N skip --reason … --confirmed` on the developer's yes.")
    if func.get("fingerprint") != fingerprint_now():
        return "FUNCTIONAL_STALE", "The files changed since the functional checks: re-run them and `dt func-record N`."
    return None


def db_target(connection: str):
    """(host, database) from a connection string, lower-cased; None for a part that is not there."""
    parts = {}
    for item in connection.split(";"):
        if "=" in item:
            k, v = item.split("=", 1)
            parts[k.strip().lower().replace(" ", "")] = v.strip()
    host = parts.get("server") or parts.get("host") or parts.get("datasource")
    name = parts.get("database") or parts.get("initialcatalog")
    return (host.lower() if host else None, name.lower() if name else None)


def _allowed(value: str) -> set:
    """local.db.allowed_host / allowed_name: one value or a comma-separated list, compared case-insensitively."""
    return {item.strip().lower() for item in value.split(",") if item.strip()}


def env_problems(profile: str, env: dict, required_keys: list = (), db_in_overlay: bool = False) -> list:
    """Why the profile can't be used (empty = OK). Reports key names only, never values.
    `db_in_overlay`: the test DB connection strings come from the local.appsettings overlay files (checked by
    `overlay_db_problems`), so local.db.connection is not required."""
    problems = []
    for key in required_keys:
        if not env.get(f"{profile}.{key}"):
            problems.append(f"missing {profile}.{key}")
    if profile == "local":
        needed = ["local.db.allowed_host", "local.db.allowed_name"] + ([] if db_in_overlay else ["local.db.connection"])
        for key in needed:
            if not env.get(key):
                problems.append(f"missing {key}")
        if not problems and env.get("local.db.connection"):
            host, name = db_target(env["local.db.connection"])
            if host not in _allowed(env["local.db.allowed_host"]) or name not in _allowed(env["local.db.allowed_name"]):
                problems.append("local.db.connection does not point at the allowed test DB (host/name mismatch)")
    return problems


# ------------------------------------------------------------- profiles and appsettings overlays

CLOUD_STAGING, CLOUD_PREPROD = "cloud_staging", "cloud_preprod"
PROFILES = (CLOUD_STAGING, CLOUD_PREPROD, "local")
REMOTE_PROFILES = (CLOUD_STAGING, CLOUD_PREPROD)  # the UI runs here against remote APIs: URL keys only, no services, no DB
LEGACY_PROFILE_NAMES = {"staging": CLOUD_STAGING, "preprod": CLOUD_PREPROD}  # in state.json files written before the rename


def url_problems(profile: str, urls: dict) -> list:
    """Key names whose value is not an http(s) URL. Never reports a value."""
    bad = []
    for key, value in urls.items():
        parsed = urlparse.urlparse(value)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            bad.append(f"{profile}.{key} is not an http(s) URL")
    return bad


def overlay_path(service_folder: Path, profile: str) -> Path:
    """The git-ignored overlay holding a service's appsettings values for a profile: appsettings.<profile>.json,
    next to the service's own appsettings.json."""
    return service_folder / f"appsettings.{profile}.json"


def load_overlay(path: Path):
    """The overlay as a dict, or None when the file does not exist. Raises ValueError (no content in the message) when bad."""
    import json
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except ValueError as exc:
        raise ValueError(f"{path.name} is not valid JSON") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} must hold a JSON object")
    return data


def leaf_paths(data, prefix: str = "") -> dict:
    """{'A:B:C': value} for every leaf of a nested dict (arrays are leaves)."""
    out = {}
    for key, value in data.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            out.update(leaf_paths(value, path + ":"))
        else:
            out[path] = value
    return out


def overlay_db_problems(profile: str, overlay: dict, env: dict) -> list:
    """Connection strings in an overlay: only the local profile may carry them, and each must point at the allowed test DB.
    Names the key path only, never the value."""
    strings = {p: v for p, v in leaf_paths(overlay).items()
               if p.lower().startswith("connectionstrings:") and isinstance(v, str)}
    if not strings:
        return []
    if profile != "local":
        return [f"{profile} overlay must not hold ConnectionStrings (only the local profile may)"]
    hosts, names = _allowed(env.get("local.db.allowed_host", "")), _allowed(env.get("local.db.allowed_name", ""))
    problems = []
    for path, value in strings.items():
        host, name = db_target(value)
        if host not in hosts or name not in names:
            problems.append(f"{path} does not point at the allowed test DB (host/name mismatch)")
    return problems


def merge_overlay(data: dict, overlay: dict) -> tuple:
    """Set the overlay's leaves into `data` where the key already exists (like environment.ts: nothing is invented).
    Returns (changed key paths, missing key paths)."""
    changed, missing = [], []

    def walk(target: dict, source: dict, prefix: str):
        for key, value in source.items():
            path = f"{prefix}{key}"
            existing = next((k for k in target if k.lower() == key.lower()), None)  # .NET config keys are case-insensitive
            if isinstance(value, dict):
                if isinstance(target.get(existing), dict):
                    walk(target[existing], value, path + ":")
                else:
                    missing.extend(p for p in leaf_paths(value, path + ":"))
            elif existing is None:
                missing.append(path)
            elif target[existing] != value:
                target[existing] = value
                changed.append(path)

    walk(data, overlay, "")
    return changed, missing


def apply_overlay(path: Path, overlay: dict) -> tuple:
    """Patch an appsettings.json with an overlay; returns (changed, missing). The file is rewritten as plain JSON
    (a local-only run copy: the original is restored by `dt env-restore`). Raises ValueError for an unparseable file."""
    import json
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except ValueError as exc:
        raise ValueError(f"{path.name} is not plain JSON (comments?)") from exc
    changed, missing = merge_overlay(data, overlay)
    if changed:
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return changed, missing


def backup_name(rel: str) -> str:
    return rel.replace("\\", "/").replace("/", "__").replace(":", "_")


def environment_ts_files(root: Path, angular_roots: list, fallback: str) -> list:
    """Repo-relative environment.ts of every Angular app (those that exist), plus the configured one."""
    rels = [f"{r.rstrip('/')}/src/environments/environment.ts" for r in angular_roots]
    if fallback not in rels:
        rels.append(fallback)
    return [r for r in rels if (root / r).is_file()]


def profile_urls(profile: str, env: dict) -> dict:
    """environment.ts key -> URL for the profile."""
    prefix = f"{profile}."
    return {k[len(prefix):]: v for k, v in env.items()
            if k.startswith(prefix) and not k.startswith(prefix + "db.") and not k.startswith(prefix + "env.")}


def apply_environment_ts(path: Path, values: dict) -> list:
    """Set `Key: 'value'` entries in environment.ts (commented-out lines are left alone); returns the keys changed."""
    text = path.read_text(encoding="utf-8")
    changed = []
    for key, value in values.items():
        pattern = re.compile(rf"^(\s*{re.escape(key)}\s*:\s*)(['\"])(.*?)\2", re.M)
        new_text, count = pattern.subn(lambda m: f"{m.group(1)}{m.group(2)}{value}{m.group(2)}", text, count=1)
        if count and new_text != text:
            text = new_text
            changed.append(key)
    if changed:
        path.write_text(text, encoding="utf-8")
    return changed


def port_open(url: str) -> bool:
    parsed = urlparse.urlparse(url)
    try:
        with socket.create_connection((parsed.hostname or "localhost", parsed.port or 80), timeout=2):
            return True
    except OSError:
        return False


def start_service(name: str, env: dict, root: Path, log_path: Path):
    """Start service.<name> detached with the test DB connection and local.env.* applied. Returns the pid."""
    cwd, cmd = env.get(f"service.{name}.cwd"), env.get(f"service.{name}.cmd")
    if not cmd:
        raise ValueError(f"service.{name}.cmd is not set")
    child_env = dict(os.environ)
    if env.get("local.db.connection"):  # the older way; otherwise the connection strings come from the patched appsettings.json
        child_env["ConnectionStrings__Connection"] = env["local.db.connection"]
    for key, value in env.items():
        if key.startswith("local.env."):
            child_env[key[len("local.env."):]] = value
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = open(log_path, "ab")  # noqa: SIM115 - the child owns it
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    proc = subprocess.Popen(cmd, shell=True, cwd=str((root / cwd) if cwd else root), env=child_env,
                            stdout=log, stderr=subprocess.STDOUT, creationflags=flags)
    return proc.pid
