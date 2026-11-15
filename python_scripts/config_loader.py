"""Loads dev_ticket.config.json, then a team profile and its repo overlay, then the optional per-developer
dev_ticket.config.local.json (last wins; dicts are deep-merged, everything else is replaced).

Layers:
  1. dev_ticket.config.json             engine defaults (the original team's values)
  2. profiles/<profile>.json            team profile, e.g. profiles/launchpad.json. Chosen by the environment variable
                                        DEV_TICKET_PROFILE (wins; "none" = no profile), else by "profile" in the local file.
                                        "auto": the profile whose repos[*].remote_match is in this repo's origin URL, so
                                        one developer can work in repos of several teams (e.g. Launchpad and C2M CI).
  3. profile["repos"][<key>]["config"]  overlay of the code repo this run works in (the repo whose `origin` URL contains
                                        that repo's "remote_match"); its key is kept as config["repo_key"]
  4. dev_ticket.config.local.json       per-developer, git-ignored: machine-specific values, e.g.
         {"profile": "launchpad",
          "db": {"repo_path": "E:/code/C2M_CI_DB"},
          "ui_checks": {"host_repo_path": "E:/code/C2M_Console_Angular_Beta"}}

Paths may be absolute or relative to this folder. Only the keys you list are overridden.

"project_root": "auto" means: the top level of the git repo the command runs in (the current directory), so one
engine folder can serve several code repos.
"""
import json
import os
import subprocess
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
LOCAL_NAME = "dev_ticket.config.local.json"
PROFILES_DIR = SCRIPT_DIR / "profiles"


def _merge(base: dict, override: dict) -> dict:
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value
    return base


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"{path.name} is not valid JSON: {exc}")


def _git_toplevel(cwd: Path):
    try:
        proc = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return Path(proc.stdout.strip()).resolve() if proc.returncode == 0 and proc.stdout.strip() else None


def _origin_url(repo: Path) -> str:
    try:
        proc = subprocess.run(["git", "-C", str(repo), "remote", "get-url", "origin"], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def resolve_project_root(config: dict) -> Path:
    """The code repo this run works in. "auto": the git repo of the current directory; else a path relative to
    python_scripts (the original layout, `AI_DEV_ENV/python_scripts` two levels below the repo root)."""
    value = config.get("project_root") or "../.."
    if value == "auto":
        top = _git_toplevel(Path.cwd())
        if top is None:
            raise SystemExit("project_root is 'auto' but the current directory is not inside a git repo: run Claude / "
                             "the dt command from the code repo you are working in (e.g. 'Launchpad - CMGMT').")
        return top
    return (SCRIPT_DIR / value).resolve()


def _profile_name(local: dict) -> str:
    name = os.environ.get("DEV_TICKET_PROFILE")
    if name is None:
        name = local.get("profile") or ""
    return "" if name.strip().lower() in ("", "none") else name.strip()


def _repo_key(repos: dict, root: Path) -> str:
    """The key in profile["repos"] whose remote_match is in this repo's origin URL (or whose folder name matches)."""
    url = _origin_url(root).lower()
    for key, repo in repos.items():
        match = str(repo.get("remote_match") or "").lower()
        if match and match in url:
            return key
    for key, repo in repos.items():
        if repo.get("folder") and Path(repo["folder"]).name.lower() == root.name.lower():
            return key
    return ""


def _auto_profile() -> str:
    """The profile whose repos include the git repo of the current directory (matched by origin URL, then folder)."""
    top = _git_toplevel(Path.cwd())
    if top is None:
        raise SystemExit("profile is 'auto' but the current directory is not inside a git repo: run Claude / the dt "
                         "command from the code repo you are working in.")
    known = []
    for path in sorted(PROFILES_DIR.glob("*.json")):
        repos = _read_json(path).get("repos") or {}
        if _repo_key(repos, top):
            return path.stem
        known += [Path(r.get("folder") or k).name for k, r in repos.items()]
    raise SystemExit(f"profile is 'auto' but '{top.name}' is not a code repo of any team profile in {PROFILES_DIR.name}/ "
                     f"(known: {', '.join(known) or 'none'}). Open Claude in one of those repos, or name the profile.")


def load_config() -> dict:
    config = _read_json(SCRIPT_DIR / "dev_ticket.config.json")
    local_path = SCRIPT_DIR / LOCAL_NAME
    local = _read_json(local_path) if local_path.is_file() else {}

    profile = _profile_name(local)
    if profile.lower() == "auto":
        profile = _auto_profile()
    if profile:
        path = PROFILES_DIR / f"{profile}.json"
        if not path.is_file():
            raise SystemExit(f"Unknown profile '{profile}': {path} does not exist (set by DEV_TICKET_PROFILE or "
                             f"\"profile\" in {LOCAL_NAME}).")
        _merge(config, _read_json(path))
        config["profile"] = profile
        repos = config.get("repos") or {}
        if repos:
            key = _repo_key(repos, resolve_project_root(_merge(json.loads(json.dumps(config)), local)))
            if key:
                _merge(config, json.loads(json.dumps(repos[key].get("config") or {})))
            config["repo_key"] = key

    _merge(config, {k: v for k, v in local.items() if k != "profile"})
    return config
