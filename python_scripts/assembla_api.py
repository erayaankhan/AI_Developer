#!/usr/bin/env python3
"""
Assembla REST API client for the dev-ticket workflow.

Stdlib only (no `requests`), same convention as the other scripts in this repo.
Talks to api.assembla.com directly - no MCP server, no Jenkins.

Credentials come from AI_DEV_ENV/credentials.env (see credentials.py), falling back to the
environment, and must be the *developer's own* key pair:
the workflow identifies "who am I" from them (GET /v1/user), so a shared key would
make every developer look like the same person.

    ASSEMBLA_API_KEY      - required
    ASSEMBLA_API_SECRET   - required
    ASSEMBLA_SPACE        - optional, default "c2m-ci"

Quirks carried over from python_scripts/deploy_on_cloud_staging_v1.py:
- Cloudflare in front of api.assembla.com intermittently answers a valid GET with
  an empty 204 body, so an empty body is retried and never trusted as "no results"
  on the first attempt. A curl-like User-Agent/Accept pair makes it rarer.
- Ticket numbers may arrive as "C2MC-18474"; only the trailing digits are used.
"""

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

import credentials

credentials.load()  # AI_DEV_ENV/credentials.env → os.environ, before any key is read (idempotent)

API_HOST = "https://api.assembla.com"
UPLOAD_HOST = "https://bigfiles.assembla.com"
DEFAULT_SPACE = "c2m-ci"


def _profile_setting(key: str):
    """A top-level value of the merged config (team profile), e.g. assembla_space / current_milestones. Read lazily so
    this client stays usable without the config files."""
    try:
        from config_loader import load_config
        return load_config().get(key)
    except (ImportError, SystemExit, OSError):
        return None
TICKET_DIGITS_RE = re.compile(r"(?:[A-Za-z][A-Za-z0-9_]*[-_]|#)?(\d+)")

HTTP_HEADERS_BASE = {
    "User-Agent": "dev-ticket/1.0 (+Claude Code)",
    "Accept": "*/*",
}

# hierarchy_type values on a ticket.
PLAN_LEVELS = {0: "No plan level", 1: "Subtask", 2: "Story", 3: "Epic"}


class AssemblaError(RuntimeError):
    pass


class _StripAuthOnForeignRedirect(urllib.request.HTTPRedirectHandler):
    """urllib copies every request header onto a redirect. Downloads redirect to S3, so without this the
    Assembla API key and secret would be sent to a third-party host."""

    SECRET_HEADERS = ("x-api-key", "x-api-secret")

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urllib.parse.urlsplit(newurl).netloc != urllib.parse.urlsplit(req.full_url).netloc:
            for store in (new.headers, new.unredirected_hdrs):
                for name in [n for n in store if n.lower() in self.SECRET_HEADERS]:
                    del store[name]
        return new


def bare_number(value) -> str:
    """'C2MC-18474' / '#18474' / 18474 -> '18474'. Anything else is refused: the value ends up in
    folder names, branch names and URL paths, so '../x' must never get through."""
    match = TICKET_DIGITS_RE.fullmatch(str(value).strip())
    if not match:
        raise AssemblaError(f"'{value}' is not a ticket number (expected digits, e.g. 18474 or C2MC-18474).")
    return match.group(1)


class AssemblaApi:

    def __init__(self, api_key: str = None, api_secret: str = None, space: str = None):
        api_key = api_key or os.environ.get("ASSEMBLA_API_KEY")
        api_secret = api_secret or os.environ.get("ASSEMBLA_API_SECRET")
        if not api_key or not api_secret:
            raise AssemblaError(
                "ASSEMBLA_API_KEY / ASSEMBLA_API_SECRET are not set. Fill them in "
                "AI_DEV_ENV/credentials.env with your own key pair (SETUP.md §2)."
            )
        self.space = space or os.environ.get("ASSEMBLA_SPACE") or _profile_setting("assembla_space") or DEFAULT_SPACE
        self._headers = dict(HTTP_HEADERS_BASE)
        self._headers["X-Api-Key"] = api_key
        self._headers["X-Api-Secret"] = api_secret
        self._user_cache = {}

    # ------------------------------------------------------------------ transport

    def _url(self, path: str, query: dict = None) -> str:
        url = f"{API_HOST}{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        return url

    def _get(self, path: str, query: dict = None, max_attempts: int = 4,
             retry_delay_sec: float = 2.0, allow_404: bool = False):
        """GET JSON. Returns None for a persistent empty body (or 404 when allowed)."""
        url = self._url(path, query)
        last_status = None
        for attempt in range(max_attempts):
            request = urllib.request.Request(url, headers=self._headers, method="GET")
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    last_status = response.status
                    body = response.read().decode("utf-8", errors="replace")
            except urllib.error.HTTPError as exc:
                if exc.code == 404 and allow_404:
                    return None
                if exc.code in (401, 403):
                    raise AssemblaError(f"GET {path} -> HTTP {exc.code} (check API key/secret and space access)")
                if exc.code == 404:
                    raise AssemblaError(f"GET {path} -> HTTP 404 (not found)")
                last_status = exc.code
                body = ""
            except urllib.error.URLError as exc:
                last_status = f"network error: {exc.reason}"
                body = ""
            if body.strip():
                return json.loads(body)
            if attempt < max_attempts - 1:
                time.sleep(retry_delay_sec)
        # An empty body on every attempt is what Assembla returns for an empty list too.
        if last_status in (200, 204):
            return None
        raise AssemblaError(f"GET {path} failed after {max_attempts} attempts (last status {last_status})")

    def _send(self, method: str, path: str, payload: dict):
        headers = dict(self._headers)
        headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self._url(path), data=json.dumps(payload).encode("utf-8"),
            headers=headers, method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read().decode("utf-8", errors="replace")
                return response.status, body
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise AssemblaError(f"{method} {path}: no response ({exc}). The write may or may not have gone "
                                "through: check Assembla before retrying.") from exc

    def _get_paged(self, path: str, query: dict = None, per_page: int = 100, max_pages: int = 50) -> list:
        items, page = [], 1
        while page <= max_pages:
            batch = self._get(path, dict(query or {}, per_page=per_page, page=page), allow_404=True)
            if not batch:
                break
            items.extend(batch)
            if len(batch) < per_page:
                break
            page += 1
        return items

    def _space_path(self, suffix: str) -> str:
        return f"/v1/spaces/{self.space}{suffix}"

    # ------------------------------------------------------------------ users

    def get_current_user(self) -> dict:
        user = self._get("/v1/user.json")
        if not user or not user.get("id"):
            raise AssemblaError("GET /v1/user returned no user - cannot identify the developer.")
        return user

    def get_user(self, user_id) -> dict:
        if not user_id:
            return {}
        if user_id not in self._user_cache:
            self._user_cache[user_id] = self._get(f"/v1/users/{user_id}.json", allow_404=True) or {}
        return self._user_cache[user_id]

    def get_space_users(self) -> list:
        """Every user of the space (id, login, name, ...): lets a login be resolved to the id Assembla assigns by."""
        return self._get_paged(self._space_path("/users.json"))

    def user_label(self, user_id) -> str:
        user = self.get_user(user_id)
        return user.get("login") or user.get("name") or (str(user_id) if user_id else "(unassigned)")

    # ------------------------------------------------------------------ tickets (read)

    def get_ticket(self, number) -> dict:
        ticket = self._get(self._space_path(f"/tickets/{bare_number(number)}.json"))
        if not ticket:
            raise AssemblaError(f"Ticket {number} returned an empty body.")
        return ticket

    def get_ticket_by_id(self, ticket_id) -> dict:
        return self._get(self._space_path(f"/tickets/id/{ticket_id}.json"), allow_404=True) or {}

    def get_comments(self, number) -> list:
        comments = self._get_paged(self._space_path(f"/tickets/{bare_number(number)}/ticket_comments.json"))
        # Assembla also records field changes as "comments" with an empty body; keep only text.
        comments = [c for c in comments if (c.get("comment") or "").strip()]
        return sorted(comments, key=lambda c: c.get("created_on") or "")

    # Tags and associations are legitimately empty for many tickets, and Assembla answers
    # "none" with an empty body - so these use 2 quick attempts instead of the default 4 x 2 s
    # (which made a 30-ticket sibling scan take ~90 s).
    def get_tags(self, number) -> list:
        tags = self._get(self._space_path(f"/tickets/{bare_number(number)}/tags.json"),
                         max_attempts=2, retry_delay_sec=0.5, allow_404=True) or []
        return [t.get("name") for t in tags if t.get("name")]

    def get_associations(self, number) -> list:
        return self._get(
            self._space_path(f"/tickets/{bare_number(number)}/ticket_associations.json"),
            max_attempts=2, retry_delay_sec=0.5, allow_404=True,
        ) or []

    def upload_attachment(self, ticket_id, file_path, content_type: str = "text/markdown") -> dict:
        """Attach a file to a ticket. Adapted from deploy_on_cloud_staging_v1.py upload_document:
        must POST to bigfiles.assembla.com (api.assembla.com Cloudflare-blocks multipart), and
        attachable_id must be the ticket's internal *id*, not its number (a number silently
        creates an unattached document)."""
        import uuid
        from pathlib import Path
        path = Path(file_path)
        boundary = uuid.uuid4().hex
        body = bytearray()
        for name, value in (("document[attachable_type]", "Ticket"), ("document[attachable_id]", str(ticket_id))):
            body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode("utf-8"))
        body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="document[file]"; '
                    f'filename="{path.name}"\r\nContent-Type: {content_type}\r\n\r\n'.encode("utf-8"))
        body.extend(path.read_bytes())
        body.extend(f"\r\n--{boundary}--\r\n".encode("utf-8"))
        headers = dict(self._headers)
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        request = urllib.request.Request(f"{UPLOAD_HOST}/v1/spaces/{self.space}/documents.json",
                                         data=bytes(body), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return json.loads(response.read().decode("utf-8", errors="replace") or "{}")
        except urllib.error.HTTPError as exc:
            raise AssemblaError(f"Upload failed: HTTP {exc.code} {exc.read().decode('utf-8', 'replace')[:300]}")

    def download_attachment(self, attachment: dict) -> bytes:
        """Download an attachment via its 'url' (/documents/{id}/download, which redirects to S3).
        Verified 2026-09-29 on #17581 (HTTP 200, byte count matched filesize)."""
        request = urllib.request.Request(attachment["url"], headers=self._headers)
        opener = urllib.request.build_opener(_StripAuthOnForeignRedirect)
        with opener.open(request, timeout=120) as response:
            return response.read()

    def get_attachments(self, number) -> list:
        """Ticket attachments: name, url (download), filesize, created_at. Verified 2026-09-29
        (#17581 -> 9). An empty list means the ticket really has none."""
        try:
            return self._get(
                self._space_path(f"/tickets/{bare_number(number)}/attachments.json"), allow_404=True
            ) or []
        except AssemblaError:
            return []

    def get_my_active_tickets(self) -> list:
        """Active (not closed) tickets assigned to the API key's owner.

        Verified 2026-09-29 against c2m-ci: /tickets/my_active returns open statuses
        only (New, Dev Queue, Re-Open, ...). Report 8 on /tickets.json is NOT this -
        it returned only closed tickets (Fixed or Prod-Deployed, Invalid, Closed).
        """
        return self._get_paged(self._space_path("/tickets/my_active.json"))

    def get_active_tickets(self) -> list:
        """Every active ticket in the space (report 1). ~2,200 tickets / ~25 s on c2m-ci (2026-09-29)."""
        return self._get_paged(self._space_path("/tickets.json"), {"report": 1}, max_pages=200)

    def get_current_milestones(self) -> list:
        """Every milestone whose title contains '_CURRENT' (one per product line is possible). A profile that sets
        `current_milestones` (a list of exact titles, e.g. ["Release 4.20.0"]) uses those milestones instead."""
        named = [str(t).strip() for t in (_profile_setting("current_milestones") or []) if str(t).strip()]
        found = {}
        for path in ("/milestones.json", "/milestones/all.json"):
            for milestone in self._get_paged(self._space_path(path)):
                title = (milestone.get("title") or "").strip()
                if (title in named) if named else ("_CURRENT" in title):
                    found[milestone["id"]] = milestone
        return list(found.values())

    def get_milestone(self, milestone_id) -> dict:
        if not milestone_id:
            return {}
        return self._get(self._space_path(f"/milestones/{milestone_id}.json"), allow_404=True) or {}

    def get_milestone_tickets(self, milestone_id, all_statuses: bool = False) -> list:
        """Open tickets of a milestone; all_statuses=True includes closed ones (verified 2026-09-29:
        Workflow 3.16.0 → 0 open, 203 with ticket_status=all)."""
        query = {"ticket_status": "all"} if all_statuses else None
        return self._get_paged(self._space_path(f"/tickets/milestone/{milestone_id}.json"), query)

    # ------------------------------------------------------------------ tickets (write)

    def add_comment(self, number, body: str):
        """Post a comment; returns its id (None if the response has none) so it can be edited later."""
        code, text = self._send(
            "POST", self._space_path(f"/tickets/{bare_number(number)}/ticket_comments.json"),
            {"ticket_comment": {"comment": body}},
        )
        if code not in (200, 201):
            raise AssemblaError(f"Add comment failed: HTTP {code} {text[:300]}")
        try:
            return json.loads(text).get("id")
        except (ValueError, AttributeError):
            return None

    def edit_comment(self, number, comment_id, body: str) -> None:
        """PUT a new body onto an existing comment (used to keep ONE summary comment current)."""
        code, text = self._send(
            "PUT", self._space_path(f"/tickets/{bare_number(number)}/ticket_comments/{comment_id}.json"),
            {"ticket_comment": {"comment": body}},
        )
        if code not in (200, 201, 204):
            raise AssemblaError(f"Edit comment failed: HTTP {code} {text[:300]}")

    def update_ticket(self, number, fields: dict) -> None:
        """PUT arbitrary ticket fields (same payload shapes as create_feature_branch_v1.py:
        hierarchy_type, custom_fields {name: value}, tags [names]). Caller re-reads to verify."""
        code, text = self._send("PUT", self._space_path(f"/tickets/{bare_number(number)}.json"), {"ticket": fields})
        if code not in (200, 204):
            raise AssemblaError(f"Ticket update {list(fields)} failed: HTTP {code} {text[:300]}")

    def add_parent(self, number, ticket_id, parent_id) -> None:
        """Link parent_id as the parent of this ticket (relationship 0: "ticket2 is Parent of ticket1")."""
        code, text = self._send(
            "POST", self._space_path(f"/tickets/{bare_number(number)}/ticket_associations.json"),
            {"ticket_association": {"ticket1_id": ticket_id, "ticket2_id": parent_id, "relationship": 0}},
        )
        if code not in (200, 201):
            raise AssemblaError(f"Link parent failed: HTTP {code} {text[:300]}")

    def add_association(self, number, ticket_id, other_id, relationship: int) -> None:
        """Link two tickets: POST from ticket `number` (id `ticket_id`) to `other_id`. `relationship` is Assembla's
        integer: 0 Parent ("ticket2 is Parent of ticket1"), 2 Related (per the API docs; the API returns no label, so the
        caller re-reads the association to verify)."""
        code, text = self._send(
            "POST", self._space_path(f"/tickets/{bare_number(number)}/ticket_associations.json"),
            {"ticket_association": {"ticket1_id": ticket_id, "ticket2_id": other_id, "relationship": relationship}},
        )
        if code not in (200, 201):
            raise AssemblaError(f"Link tickets (relationship {relationship}) failed: HTTP {code} {text[:300]}")

    def create_ticket(self, fields: dict) -> dict:
        """POST a new ticket (summary, description, milestone_id, assigned_to_id, priority ...) and return it (with its
        number and id). Plan level, custom fields, tags and status are applied afterwards by the caller, which re-reads
        the ticket to verify them."""
        code, text = self._send("POST", self._space_path("/tickets.json"), {"ticket": fields})
        if code not in (200, 201):
            raise AssemblaError(f"Create ticket failed: HTTP {code} {text[:300]}")
        try:
            created = json.loads(text)
        except ValueError as exc:
            raise AssemblaError(f"Create ticket: unreadable answer ({text[:200]}). It may have been created: check "
                                "Assembla before retrying.") from exc
        if not created.get("number"):
            raise AssemblaError("Create ticket: no ticket number in the answer. It may have been created: check "
                                "Assembla before retrying.")
        return created

    def delete_association(self, number, association_id) -> None:
        """Remove one ticket association (verified 2026-09-29 on #18359)."""
        request = urllib.request.Request(
            self._url(self._space_path(f"/tickets/{bare_number(number)}/ticket_associations/{association_id}.json")),
            headers=self._headers, method="DELETE",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                if response.status not in (200, 204):
                    raise AssemblaError(f"Unlink failed: HTTP {response.status}")
        except urllib.error.HTTPError as exc:
            raise AssemblaError(f"Unlink failed: HTTP {exc.code} {exc.read().decode('utf-8', 'replace')[:300]}")

    def assign_ticket(self, number, user_id) -> None:
        self.update_ticket(number, {"assigned_to_id": user_id})
        # Never trust the PUT alone: re-read, as update_status does.
        actual = self.get_ticket(number).get("assigned_to_id")
        if str(actual) != str(user_id):
            raise AssemblaError("Assignee update not applied: the ticket is not assigned to the requested user.")

    def assign_and_move(self, number, user_id, new_status: str) -> None:
        """Assignee and status in ONE update, so a ticket is never left half-way (e.g. in Dev Queue but still assigned to
        the reviewer). Re-reads and checks both: Assembla silently ignores what it does not accept."""
        self.update_ticket(number, {"assigned_to_id": user_id, "status": new_status})
        actual = self.get_ticket(number)
        ignored = [name for name, ok in (("assignee", str(actual.get("assigned_to_id")) == str(user_id)),
                                         ("status", actual.get("status") == new_status)) if not ok]
        if ignored:
            raise AssemblaError(f"Update not applied: the ticket's {' and '.join(ignored)} did not change as requested.")

    def update_status(self, number, new_status: str) -> None:
        code, text = self._send(
            "PUT", self._space_path(f"/tickets/{bare_number(number)}.json"),
            {"ticket": {"status": new_status}},
        )
        if code not in (200, 204):
            raise AssemblaError(f"Status update failed: HTTP {code} {text[:300]}")
        # Never trust the PUT alone - Assembla silently ignores unknown status names.
        actual = self.get_ticket(number).get("status")
        if actual != new_status:
            raise AssemblaError(f"Status update not applied: ticket is '{actual}', expected '{new_status}'.")
