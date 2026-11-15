"""
Loads dev-ticket credentials from AI_DEV_ENV/credentials.env (git-ignored).

File format: one KEY=VALUE per line; blank lines and lines starting with # are ignored;
optional surrounding quotes are stripped. Recognised keys:

    ASSEMBLA_API_KEY, ASSEMBLA_API_SECRET, ASSEMBLA_SPACE,
    JENKINS_USER, JENKINS_API_TOKEN, JENKINS_BASE_URL,
    JENKINS_REMOTE_USER, JENKINS_REMOTE_API_TOKEN, JENKINS_REMOTE_BASE_URL,
    SONAR_TOKEN, SNYK_TOKEN (dt quality: SonarQube user token, Snyk auth token)

Precedence: a non-empty value in the file wins over an environment variable of the same
name, so the file is the single source of truth. Keys that are missing or empty in the
file fall back to the environment. Override the file location with DEV_TICKET_CREDENTIALS.

Values are only ever put into os.environ for this process; they are never printed.
"""

import os
from pathlib import Path

KEYS = ("ASSEMBLA_API_KEY", "ASSEMBLA_API_SECRET", "ASSEMBLA_SPACE",
        "JENKINS_USER", "JENKINS_API_TOKEN", "JENKINS_BASE_URL",
        "JENKINS_REMOTE_USER", "JENKINS_REMOTE_API_TOKEN", "JENKINS_REMOTE_BASE_URL",
        "SONAR_TOKEN", "SNYK_TOKEN")

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "credentials.env"


def credentials_path() -> Path:
    return Path(os.environ.get("DEV_TICKET_CREDENTIALS") or DEFAULT_PATH)


def load() -> dict:
    """Apply the file's values to os.environ. Returns {key: "file"|"env"|"missing"} (no values)."""
    path = credentials_path()
    if path.is_file():
        for raw in path.read_text(encoding="utf-8-sig").splitlines():  # -sig: Notepad saves a BOM
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = (part.strip() for part in line.split("=", 1))
            value = value.strip('"').strip("'")
            if (key in KEYS or key.startswith("TEAMS_WEBHOOK_URL")) and value:  # Teams: one key per team profile
                os.environ[key] = value
                os.environ[f"_DEV_TICKET_SRC_{key}"] = "file"
    return {k: os.environ.get(f"_DEV_TICKET_SRC_{k}") or ("env" if os.environ.get(k) else "missing") for k in KEYS}


SOURCES = load()
