"""Local environment and functional checks: host-check, profile, env-check, env-apply, service-start, func-mode,
func-prepare, func-record."""

import re
import urllib.parse as urllib_parse
from pathlib import Path

from assembla_api import bare_number

from develop_ticket import common
from develop_ticket.common import (CONFIG, SCRIPT_DIR, _write_state, inside_ticket_folder, now_iso, record_metric, result,
                                    ticket_dir)


def _port_open(url: str) -> bool:
    import socket
    parsed = urllib_parse.urlparse(url)
    try:
        with socket.create_connection((parsed.hostname or "localhost", parsed.port or 80), timeout=2):
            return True
    except OSError:
        return False


def cmd_host_check(_api, _args) -> int:
    """Step 7d pre-flight (D-29): host project on its dev branch, host and remote ports serving.

    Read-only: never switches the host repo's branch and never starts anything itself.
    """
    ui = CONFIG["ui_checks"]
    host_repo = (SCRIPT_DIR / ui["host_repo_path"]).resolve()
    info = {"host_repo": str(host_repo), "host_url": ui["host_url"], "remote_url": ui["remote_url"]}
    if not (host_repo / ".git").exists() and not (host_repo / "angular.json").exists():
        return result("NO_HOST_REPO", 1, **info, message="Host project not found (config ui_checks.host_repo_path).")
    try:
        branch = common.git("branch", "--show-current", repo=host_repo).stdout.strip()
    except RuntimeError as exc:
        return result("FAILED", 1, **info, message=str(exc))
    info.update(host_branch=branch, host_up=_port_open(ui["host_url"]), remote_up=_port_open(ui["remote_url"]))
    if branch != ui["host_branch"]:
        return result("HOST_WRONG_BRANCH", **info, expected_branch=ui["host_branch"],
                      message=f"Host project is on '{branch}', expected '{ui['host_branch']}'.")
    if not info["host_up"] or not info["remote_up"]:
        return result("NOT_SERVING", **info,
                      start_host=f"cd {host_repo} && {ui['host_serve']}", start_remote=ui["remote_serve"])
    return result("READY", **info)


# ------------------------------------------------------------- functional checks (step 7d)

def _local_env():
    import functional
    cfg = CONFIG["functional_checks"]
    return functional, load_env_path(cfg), cfg


def load_env_path(cfg: dict) -> Path:
    return common.PROJECT_ROOT / cfg["local_env_file"]


def cmd_profile(_api, args) -> int:
    """Which environment the functional checks run against, decided from the changed files (never by judgement):
    cloud_staging / cloud_preprod = Angular-only change (UI against the staging or Preprod APIs), local = anything else
    (local services + test DB)."""
    import local_gates
    functional, env_path, cfg = _local_env()
    number = bare_number(args.ticket)
    state, path = common.load_state(number)
    if path is None:
        return result("NO_STATE", 1, ticket=number, message="Run fetch first.")
    files = local_gates.changed_files()
    decided = functional.profile_for(files, cfg["angular_roots"])
    choose = getattr(args, "choose", None)
    if choose and decided != functional.CLOUD_STAGING:  # the staging/preprod choice only exists for an Angular-only change
        return result("CHOICE_NOT_ALLOWED", 1, ticket=number, decided=decided,
                      message="Staging or Preprod can be chosen only when every changed file is Angular code.")
    profile = args.force or choose or decided
    state["profile"] = {"name": profile, "forced": bool(args.force), "at": now_iso()}
    _write_state(path, state, f"profile={profile}")
    needs_choice = decided == functional.CLOUD_STAGING and not args.force and not choose
    return result("PROFILE", ticket=number, profile=profile, files=len(files), forced=bool(args.force),
                  needs_choice=needs_choice, choose_between=list(functional.REMOTE_PROFILES) if needs_choice else None,
                  message={"cloud_staging": "Angular-only: run the checks in the UI against the staging APIs (or Preprod: "
                                            "ask the developer, then `dt profile N --choose cloud_preprod`)." if needs_choice else
                                            "Angular-only: run the checks in the UI against the staging APIs.",
                           "cloud_preprod": "Angular-only: run the checks in the UI against the Preprod APIs.",
                           "local": "Backend or mixed: run local services with the test DB (dt env-check, dt env-apply).",
                           "none": "No changed files."}[profile])


def _profile_name(state: dict, override=None) -> str:
    import functional
    name = override or (state.get("profile") or {}).get("name") or "local"
    return functional.LEGACY_PROFILE_NAMES.get(name, name)  # a state.json written before the cloud_ rename


def _service_dirs(env: dict) -> dict:
    """service name -> folder, for the services inside this repo (C2M_API lives elsewhere: it is never patched)."""
    root = common.PROJECT_ROOT.resolve()
    out = {}
    for key, cwd in env.items():
        parts = key.split(".")
        if len(parts) == 3 and parts[0] == "service" and parts[2] == "cwd":
            folder = (common.PROJECT_ROOT / cwd).resolve()
            if folder.is_relative_to(root):
                out[parts[1]] = folder
    return out


def _overlays(profile: str, env: dict, cfg: dict) -> dict:
    """service -> (overlay dict, appsettings.json path) for the profile's git-ignored overlay files. Raises ValueError.
    Only the local profile has overlays: the remote profiles start no services, and an appsettings.cloud_*.json found in a
    service folder is that environment's own deploy file, never an overlay."""
    import functional
    out = {}
    if profile in functional.REMOTE_PROFILES:
        return out
    for name, folder in sorted(_service_dirs(env).items()):
        overlay = functional.load_overlay(functional.overlay_path(folder, profile))
        if overlay is not None:
            out[name] = (overlay, folder / "appsettings.json")
    return out


def _has_connection_strings(overlays: dict) -> bool:
    import functional
    return any(p.lower().startswith("connectionstrings:") for overlay, _ in overlays.values()
               for p in functional.leaf_paths(overlay))


def _profile_problems(functional, profile: str, env: dict, urls: dict, overlays: dict) -> list:
    problems = functional.env_problems(profile, env, ["GatewayAPIUrl"] if profile == "local" else [],
                                       db_in_overlay=_has_connection_strings(overlays))
    problems += functional.url_problems(profile, urls)
    for service, (overlay, target) in overlays.items():
        problems += [f"{service}: {p}" for p in functional.overlay_db_problems(profile, overlay, env)]
        if not target.is_file():
            problems.append(f"{service}: appsettings.json not found for the overlay")
    return problems


def _host_repo() -> Path:
    """The host application's repo (config ui_checks.host_repo_path): a separate repo, only its environment.ts is patched."""
    return (common.SCRIPT_DIR / CONFIG["ui_checks"]["host_repo_path"]).resolve()


HOST_PREFIX = "host:"


def _rel(path: Path) -> str:
    """State key of a patched file: repo-relative, or `host:<path inside the host repo>`."""
    resolved = path.resolve()
    if resolved.is_relative_to(_host_repo()):
        return HOST_PREFIX + resolved.relative_to(_host_repo()).as_posix()
    return resolved.relative_to(common.PROJECT_ROOT.resolve()).as_posix()


def _path_of(key: str) -> Path:
    return _host_repo() / key[len(HOST_PREFIX):] if key.startswith(HOST_PREFIX) else common.PROJECT_ROOT / key


def cmd_env_check(_api, args) -> int:
    """Is the chosen profile usable? Key names and booleans only; never a value."""
    functional, env_path, cfg = _local_env()
    number = bare_number(args.ticket)
    state, _ = common.load_state(number)
    profile = _profile_name(state, args.profile)
    if profile == "none":
        return result("ENV_READY", ticket=number, profile=profile)
    env = functional.load_env_file(env_path)
    if not env:
        return result("ENV_FILE_MISSING", 1, ticket=number, profile=profile, file=cfg["local_env_file"],
                      message="Create the local env file (see functional.py docstring); never paste values in chat.")
    try:
        overlays = _overlays(profile, env, cfg)
    except ValueError as exc:
        return result("OVERLAY_INVALID", 1, ticket=number, profile=profile, message=str(exc))
    urls = functional.profile_urls(profile, env)
    problems = _profile_problems(functional, profile, env, urls, overlays)
    if problems:
        return result("ENV_PROBLEMS", 1, ticket=number, profile=profile, problems=problems)
    serving = {k: functional.port_open(v) for k, v in urls.items() if profile == "local"}
    services = sorted({k.split(".")[1] for k in env if k.startswith("service.")})
    overview = {p: {"url_keys": len(functional.profile_urls(p, env)),
                    "appsettings_overlays": sorted(_overlays_quiet(p, env, cfg))} for p in functional.PROFILES}
    return result("ENV_READY", ticket=number, profile=profile, url_keys=sorted(urls), local_serving=serving,
                  services=services, appsettings_overlays=sorted(overlays), profiles=overview,
                  patched=sorted((state or {}).get("env_patched") or {}))


def _overlays_quiet(profile: str, env: dict, cfg: dict) -> dict:
    try:
        return _overlays(profile, env, cfg)
    except ValueError:
        return {}


def _hide_from_git(rel: str) -> bool:
    """skip-worktree: the patched appsettings.json stays out of git status / diff (so it can't enter the gates or a stage)."""
    try:
        return common.git("update-index", "--skip-worktree", rel, check=False).returncode == 0
    except (OSError, RuntimeError):
        return False


def _show_to_git(rel: str) -> None:
    try:
        common.git("update-index", "--no-skip-worktree", rel, check=False)
    except (OSError, RuntimeError):
        pass


def _is_dirty(rel: str) -> bool:
    try:
        return bool(common.git("status", "--porcelain", "--", rel, check=False).stdout.strip())
    except (OSError, RuntimeError):
        return False


def cmd_env_apply(_api, args) -> int:
    """Write the profile's values into the local run files, after snapshotting each original under
    DEV_TICKETS/N/env-backup/: the URLs into every Angular app's environment.ts, and the git-ignored
    <profile>.appsettings.<service>.json overlays (connection strings included) into the service's appsettings.json.
    Nothing is ever staged; `dt func-record` (or hand-off / review-compare, or `dt env-restore`) puts the originals back."""
    import shutil
    functional, env_path, cfg = _local_env()
    number = bare_number(args.ticket)
    state, state_path = common.load_state(number)
    profile = _profile_name(state, args.profile)
    env = functional.load_env_file(env_path)
    try:
        overlays = _overlays(profile, env, cfg)
    except ValueError as exc:
        return result("OVERLAY_INVALID", 1, ticket=number, profile=profile, message=str(exc))
    urls = functional.profile_urls(profile, env)
    if not urls and not overlays:
        return result("NOTHING_TO_APPLY", ticket=number, profile=profile)
    problems = functional.url_problems(profile, urls)
    for service, (overlay, _target) in overlays.items():
        problems += [f"{service}: {p}" for p in functional.overlay_db_problems(profile, overlay, env)]
    if profile == "local" and overlays:
        problems += functional.env_problems("local", env, db_in_overlay=True)
    if problems:
        return result("ENV_PROBLEMS", 1, ticket=number, profile=profile, problems=problems)
    patched = state.setdefault("env_patched", {}) if state is not None else {}
    backup_dir = ticket_dir(number) / "env-backup"
    dirty = [_rel(t) for _o, t in overlays.values()
             if t.is_file() and _rel(t) not in patched and _is_dirty(_rel(t))]
    if dirty:
        return result("APPSETTINGS_DIRTY", 1, ticket=number, profile=profile, files=dirty,
                      message="These appsettings.json files have uncommitted changes of their own: commit or stash "
                              "them first, then re-run. Nothing was written.")

    def patch(path: Path, do_apply, hide: bool):
        rel = _rel(path)
        backup = backup_dir / functional.backup_name(rel)
        fresh = rel not in patched
        if fresh:
            backup_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, backup)
        try:
            outcome = do_apply(path)
        except ValueError:
            if fresh:
                backup.unlink(missing_ok=True)
            raise
        if fresh and outcome[0]:
            patched[rel] = {"skip_worktree": _hide_from_git(rel) if hide else False}
        elif fresh:
            backup.unlink(missing_ok=True)
        return rel, outcome

    files, changed_keys = {}, []
    if urls:
        targets = [common.PROJECT_ROOT / r for r in
                   functional.environment_ts_files(common.PROJECT_ROOT, cfg["angular_roots"], cfg["environment_ts"])]
        host_ts = _host_repo() / "src" / "environments" / "environment.ts"  # the hoster application
        if host_ts.is_file():
            targets.append(host_ts)
        for target in targets:
            rel, (changed,) = patch(target, lambda p: (functional.apply_environment_ts(p, urls),), False)
            files[rel] = {"changed": changed}
            changed_keys += [k for k in changed if k not in changed_keys]
    appsettings = {}
    try:
        for service, (overlay, target) in overlays.items():
            rel, (changed, missing) = patch(target, lambda p, o=overlay: functional.apply_overlay(p, o), True)
            appsettings[service] = {"file": rel, "changed": changed, "missing_keys": missing}
    except ValueError as exc:
        if state_path is not None:
            _write_state(state_path, state, f"env-apply {profile} (stopped: {exc})")
        return result("APPSETTINGS_UNPARSEABLE", 1, ticket=number, profile=profile, message=str(exc))
    if state_path is not None:
        if not patched:
            state.pop("env_patched", None)
        _write_state(state_path, state, f"env-apply {profile}")
    return result("APPLIED", ticket=number, profile=profile, changed_keys=changed_keys,
                  unchanged_keys=sorted(set(urls) - set(changed_keys)), files=files, appsettings=appsettings,
                  message="Local run files only: never stage them. `dt env-restore N` puts the originals back "
                          "(hand-off is refused until then).")


def _restore_patched(number: str, state: dict, state_path, label: str = "env-restore"):
    """Put every file recorded in state["env_patched"] back from its snapshot and write the state. -> (restored, missing)."""
    import shutil
    functional, _env_path, _cfg = _local_env()
    patched = state.get("env_patched") or {}
    backup_dir = ticket_dir(number) / "env-backup"
    restored, missing = [], []
    for rel, info in sorted(patched.items()):
        backup = backup_dir / functional.backup_name(rel)
        if not backup.is_file():
            missing.append(rel)
            continue
        if info.get("skip_worktree"):
            _show_to_git(rel)
        shutil.copyfile(backup, _path_of(rel))
        backup.unlink()
        restored.append(rel)
    if missing:
        state["env_patched"] = {rel: patched[rel] for rel in missing}
    else:
        state.pop("env_patched", None)
    _write_state(state_path, state, f"{label} ({len(restored)} restored)")
    return restored, missing


def auto_restore_env(number: str, state: dict) -> list:
    """Safety net before the hand-off / review gates: put back any run file still patched, so a forgotten
    `dt env-restore` no longer blocks. A file with no snapshot stays recorded (ENV_PATCHED still blocks). -> restored files."""
    if not state.get("env_patched"):
        return []
    _state, state_path = common.load_state(number)
    if state_path is None:
        return []
    restored, missing = _restore_patched(number, state, state_path, "env auto-restore")
    import sys
    print(f"env auto-restore: {len(restored)} patched run file(s) put back"
          + (f"; no snapshot for {', '.join(missing)} (git checkout -- <file>)" if missing else ""), file=sys.stderr)
    return restored


def cmd_env_restore(_api, args) -> int:
    """Put every file patched by `dt env-apply` back from its snapshot and clear the guard."""
    number = bare_number(args.ticket)
    state, state_path = common.load_state(number)
    if state_path is None:
        return result("NO_STATE", 1, ticket=number, message="Run fetch first.")
    if not state.get("env_patched"):
        return result("NOTHING_TO_RESTORE", ticket=number)
    restored, missing = _restore_patched(number, state, state_path)
    return result("RESTORED" if not missing else "RESTORE_INCOMPLETE", 1 if missing else 0, ticket=number,
                  restored=restored, missing_backup=missing,
                  message=None if not missing else "No snapshot for these files: `git checkout -- <file>` restores them.")


def cmd_service_start(_api, args) -> int:
    """Start one local service detached with the test DB connection and env vars applied (local profile only)."""
    functional, env_path, cfg = _local_env()
    number = bare_number(args.ticket)
    state, path = common.load_state(number)
    if _profile_name(state) != "local":
        return result("NOT_LOCAL_PROFILE", 1, ticket=number, profile=_profile_name(state),
                      message="Local services are only started for the local profile.")
    env = functional.load_env_file(env_path)
    try:
        overlays = _overlays("local", env, cfg)
    except ValueError as exc:
        return result("OVERLAY_INVALID", 1, ticket=number, message=str(exc))
    problems = functional.env_problems("local", env, db_in_overlay=_has_connection_strings(overlays))
    for service, (overlay, _target) in overlays.items():
        problems += [f"{service}: {p}" for p in functional.overlay_db_problems("local", overlay, env)]
    if problems:
        return result("ENV_PROBLEMS", 1, ticket=number, problems=problems)
    # an overlay that is not (yet) patched in is only a warning: its values may already equal the file's
    not_applied = sorted(s for s, (_o, t) in overlays.items() if _rel(t) not in (state.get("env_patched") or {}))
    url = env.get(f"service.{args.service}.url")
    if url and functional.port_open(url):
        return result("ALREADY_SERVING", ticket=number, service=args.service)
    try:
        pid = functional.start_service(args.service, env, common.PROJECT_ROOT,
                                       ticket_dir(number) / "services" / f"{args.service}.log")
    except ValueError as exc:
        return result("BAD_SERVICE", 1, ticket=number, service=args.service, message=str(exc))
    state.setdefault("services", {})[args.service] = {"pid": pid, "at": now_iso()}
    _write_state(path, state, f"service {args.service} started")
    return result("STARTED", ticket=number, service=args.service, pid=pid, overlays_not_patched_in=not_applied,
                  log=str(ticket_dir(number) / "services" / f"{args.service}.log"))


def cmd_func_mode(_api, args) -> int:
    """The developer's per-ticket choice for the functional checks: run | skip | later. Claude asks; never decides.
    skip needs a reason and the developer's explicit yes (--confirmed); it is shown in SUMMARY.md."""
    number = bare_number(args.ticket)
    state, path = common.load_state(number)
    if path is None:
        return result("NO_STATE", 1, ticket=number, message="Run fetch first.")
    if args.mode == "skip":
        if not args.reason or not args.reason.strip():
            return result("REASON_REQUIRED", 1, ticket=number, message="A skip needs a one-line reason.")
        if not args.confirmed:
            return result("CONFIRMATION_REQUIRED", 1, ticket=number,
                          message="Skipping needs the developer's explicit yes: re-run with --confirmed.")
        state["functional_skip"] = {"reason": args.reason.strip(), "at": now_iso()}
    else:
        state.pop("functional_skip", None)
    state["functional_mode"] = args.mode
    _write_state(path, state, f"functional_mode={args.mode}")
    record_metric(number, "functional_mode", mode=args.mode)
    return result("MODE_SET", ticket=number, mode=args.mode,
                  note="SUMMARY.md must say 'Functional checks: skipped by the developer (<reason>)'."
                  if args.mode == "skip" else None)


def cmd_func_prepare(_api, args) -> int:
    """Step 7d without Chrome control here: write FUNCTIONAL_CHECKS.md (from PLAN.md) for the developer to run in Claude Desktop."""
    functional, _env, _cfg = _local_env()
    number = bare_number(args.ticket)
    state, path = common.load_state(number)
    if path is None:
        return result("NO_STATE", 1, ticket=number, message="Run fetch first.")
    plan = ticket_dir(number) / "PLAN.md"
    checks = functional.plan_checks_section(plan.read_text(encoding="utf-8")) if plan.is_file() else ""
    if not checks:
        return result("NO_PLANNED_CHECKS", 1, ticket=number,
                      message="PLAN.md has no `## Functional checks` section. Add it, or skip: `dt func-mode N skip`.")
    out = ticket_dir(number) / "reviews" / "functional-checks-rerun.md"
    brief = ticket_dir(number) / "FUNCTIONAL_CHECKS.md"
    brief.write_text(functional.desktop_brief(number, _profile_name(state), "## Checks\n" + checks, str(out)), encoding="utf-8")
    return result("PREPARED", ticket=number, brief=str(brief), results_file=str(out),
                  message=f"Run {brief} in Claude Desktop, then: dt func-record {number} --from {out}")


def cmd_func_record(_api, args) -> int:
    """Record the functional-check run (or a developer-confirmed 'no UI path'). The hand-off gate reads this."""
    import local_gates
    functional, _env, _cfg = _local_env()
    number = bare_number(args.ticket)
    state, path = common.load_state(number)
    if path is None:
        return result("NO_STATE", 1, ticket=number, message="Run fetch first.")
    fingerprint = local_gates.diff_fingerprint(local_gates.changed_files())
    record = {"profile": _profile_name(state), "fingerprint": fingerprint, "at": now_iso()}
    plan = ticket_dir(number) / "PLAN.md"
    ids = functional.plan_check_ids(plan.read_text(encoding="utf-8")) if plan.is_file() else []
    if not ids:
        return result("NO_PLANNED_CHECKS", 1, ticket=number,
                      message="PLAN.md has no `## Functional checks` (FC-n lines). Add them, or skip: `dt func-mode N skip`.")
    if args.from_file:
        src = Path(args.from_file)
        if not inside_ticket_folder(number, args.from_file) or not src.is_file():
            return result("FILE_OUTSIDE_TICKET_FOLDER", 1, ticket=number, file=args.from_file,
                          message=f"--from needs an existing file inside {ticket_dir(number)}.")
        text = src.read_text(encoding="utf-8")
        totals = functional.parse_result_line(text)
        if totals is None:
            return result("NO_RESULT_LINE", 1, ticket=number, file=args.from_file,
                          message="The file has no final `passed X failed Y` line; the run is not finished.")
        absent = [i for i in ids if not re.search(rf"\b{re.escape(i)}\b", text)]
        if absent:
            return result("MISSING_CHECK_IDS", 1, ticket=number, file=args.from_file, missing=absent,
                          message="Every planned check must appear in the results file.")
        args.passed, args.failed = totals
        record["evidence"] = str(src)
    if args.passed + args.failed != len(ids):
        return result("COUNT_MISMATCH", 1, ticket=number, planned=len(ids), reported=args.passed + args.failed,
                      message="Every planned check must be reported as passed or failed.")
    record.update(status="PASS" if args.failed == 0 else "FAIL", passed=args.passed, failed=args.failed,
                  planned=len(ids))
    state["functional"] = record
    if state.get("functional_mode") != "run":  # running them is choosing `run`
        state["functional_mode"] = "run"
        state.pop("functional_skip", None)
    _write_state(path, state, f"functional {record['status']}")
    extra = {}
    if state.get("env_patched") and record["status"] == "PASS":  # a FAIL keeps them: the next round runs on the same patch
        restored, missing = _restore_patched(number, state, path, "env auto-restore")
        extra = {"env_restored": restored}
        if missing:
            extra["env_restore_incomplete"] = missing
    return result(record["status"], 0 if record["status"] != "FAIL" else 1, ticket=number, **{
        k: v for k, v in record.items() if k in ("profile", "passed", "failed", "planned", "reason")}, **extra)
