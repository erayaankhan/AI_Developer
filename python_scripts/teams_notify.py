"""Teams message to the reviewer when a ticket is handed off (config `teams`).

The team creates one Teams "Workflows" webhook on a channel ("Post to a channel when a webhook request is received") and
every member puts its URL in their own git-ignored credentials.env under `teams.env_key` (default TEAMS_WEBHOOK_URL). At
`dt assign --yes` (and `dt teams-notify`) an Adaptive Card is posted there that @mentions the reviewer by their email
(the Assembla user's email, or `teams.emails` {login: email} when Teams uses another address).

The URL carries its own signature, so it is a secret: never printed, never stored in state or a RESULT. Only hosts of
Microsoft's Workflows service are accepted (`teams.allowed_hosts` can add one), so a wrong value cannot post elsewhere.
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_ENV_KEY = "TEAMS_WEBHOOK_URL"
ALLOWED_HOST_SUFFIXES = (".logic.azure.com", ".powerplatform.com", ".webhook.office.com")
TIMEOUT_SECONDS = 15


def settings(config: dict) -> dict:
    return config.get("teams") or {}


def enabled(config: dict) -> bool:
    return bool(settings(config).get("enabled"))


def env_key(config: dict) -> str:
    return settings(config).get("env_key") or DEFAULT_ENV_KEY


def webhook_problem(config: dict):
    """None when a usable webhook URL is configured, else (status, message). The URL itself is never returned."""
    url = os.environ.get(env_key(config), "").strip()
    if not url:
        return ("TEAMS_NOT_CONFIGURED", f"No {env_key(config)} in credentials.env: ask the lead for the team's Teams "
                                        "webhook and add it (see the setup guide).")
    parts = urllib.parse.urlsplit(url)
    hosts = ALLOWED_HOST_SUFFIXES + tuple("." + h.lstrip(".") for h in settings(config).get("allowed_hosts") or [])
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not any(host.endswith(s) or "." + host == s for s in hosts):
        return ("TEAMS_BAD_URL", f"{env_key(config)} is not a Teams Workflows webhook (https on {', '.join(hosts)}).")
    return None


def ticket_url(config: dict, number: str) -> str:
    space = os.environ.get("ASSEMBLA_SPACE") or config.get("assembla_space") or ""
    return f"https://app.assembla.com/spaces/{urllib.parse.quote(space)}/tickets/{number}"


def reviewer_email(config: dict, login: str, user: dict) -> str:
    """The address Teams mentions: `teams.emails[login]` if set, else the Assembla user's email."""
    overrides = {str(k).lower(): v for k, v in (settings(config).get("emails") or {}).items()}
    return str(overrides.get(login.lower()) or user.get("email") or "").strip()


def review_request(*, number: str, title: str, url: str, reviewer: str, reviewer_name: str, email: str,
                   author: str, round_no, risk) -> dict:
    """-> {"text": the plain message (shown in the preview), "payload": the webhook body}."""
    name = reviewer_name or reviewer
    mention = f"<at>{name}</at>" if email else name
    headline = f"{mention}, #{number} is ready for your review"
    facts = [{"title": "Ticket", "value": f"#{number} {title}".strip()},
             {"title": "Author", "value": author or "?"},
             {"title": "Round", "value": str(round_no or 1)},
             {"title": "Risk", "value": str(risk or "?")}]
    card = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "type": "AdaptiveCard", "version": "1.4",
        "body": [{"type": "TextBlock", "text": headline, "wrap": True, "weight": "Bolder"},
                 {"type": "FactSet", "facts": facts},
                 {"type": "TextBlock", "wrap": True, "isSubtle": True,
                  "text": f"In the repo, run /review-and-deploy {number} in Claude Code."}],
        "actions": [{"type": "Action.OpenUrl", "title": "Open in Assembla", "url": url}],
    }
    if email:
        card["msteams"] = {"entities": [{"type": "mention", "text": mention, "mentioned": {"id": email, "name": name}}]}
    payload = {"type": "message", "attachments": [
        {"contentType": "application/vnd.microsoft.card.adaptive", "contentUrl": None, "content": card}]}
    text = (f"@{name}, #{number} {title} is ready for your review (author {author}, round {round_no or 1}, "
            f"risk {risk or '?'}). {url}")
    return {"text": text, "payload": payload}


def post(config: dict, payload: dict, opener=urllib.request.urlopen) -> dict:
    """Post to the team's webhook. -> {"status": "SENT"} or {"status": "TEAMS_FAILED", "error": ...} (no URL in it)."""
    problem = webhook_problem(config)
    if problem:
        return {"status": problem[0], "message": problem[1]}
    request = urllib.request.Request(os.environ[env_key(config)].strip(), data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
    try:
        with opener(request, timeout=TIMEOUT_SECONDS) as response:
            code = response.status
    except urllib.error.HTTPError as exc:
        return {"status": "TEAMS_FAILED", "error": f"HTTP {exc.code}"}
    except (urllib.error.URLError, OSError) as exc:
        return {"status": "TEAMS_FAILED", "error": type(exc).__name__ + ": " + str(getattr(exc, "reason", "") or "")[:120]}
    if code not in (200, 201, 202):
        return {"status": "TEAMS_FAILED", "error": f"HTTP {code}"}
    return {"status": "SENT"}
