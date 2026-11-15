#!/usr/bin/env python3
"""
Deploy on staging, run locally - release-cycle orchestrator (copy of CR-9131's
deploy_on_cloud_staging_v1.py, kept inside AI_DEV_ENV so dev-ticket owns it).

Self-contained: standard library only, no import of any other repo file, and it never
calls the local Jenkins or the `claude` CLI. The lowest-numbered Story ticket in Assembla's
"DevOps Staging" status (first "_CURRENT" milestone) is merged and built by triggering the
remote "C2MC - Merge and Deploy Feature Branch" job and following its downstream app builds
until Java_Transfer completes; then the script exits (one ticket per run).

Differences from the original:
  - runs from the developer's machine, not as a Jenkins build step
  - credentials come from AI_DEV_ENV/credentials.env (falls back to environment variables)
  - reports / state go to DEV_TICKETS/_deploy/ (git-ignored), not DEPLOYMENT_REPORT/
  - QA test-case generation and Phase 3 (claude CLI browser testing) are removed
  - build-list entries that are not real Jenkins jobs (e.g. AI_DEV_ENV) are skipped, found by
    asking the remote Jenkins whether the job exists, not by a name pattern

Usage:    python deploy_staging_local.py
Required in credentials.env (or the environment):
    ASSEMBLA_API_KEY, ASSEMBLA_API_SECRET          Assembla
    JENKINS_REMOTE_USER, JENKINS_REMOTE_API_TOKEN  the build server (jenkins-build-server-4)
"""

import ast
import base64
import csv
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import date
from pathlib import Path



def _load_credentials_env() -> None:
    """KEY=VALUE lines of AI_DEV_ENV/credentials.env into os.environ (real environment wins). Never printed."""
    path = Path(__file__).resolve().parent.parent / "credentials.env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            value = value.strip().strip('"').strip("'")
            if value:  # same rule as credentials.py: a non-empty file value wins over the environment
                os.environ[key.strip()] = value


_load_credentials_env()

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace', line_buffering=True)
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace', line_buffering=True)

# ---------------------------------------------------------------------------
# Configuration - read only from the environment, no hardcoded defaults.
# Jenkins supplies the real values as this job's own parameter defaults.
# ---------------------------------------------------------------------------
ASSEMBLA_API_KEY = os.environ.get("ASSEMBLA_API_KEY")
ASSEMBLA_API_SECRET = os.environ.get("ASSEMBLA_API_SECRET")
JENKINS_JOB_NAME = "deploy_staging_local"
JENKINS_BUILD_NUMBER = "L" + time.strftime("%Y%m%d%H%M%S")  # unique per run, replaces Jenkins' BUILD_NUMBER
# Unique identifier per build run — used to scope CSV rows so each build gets its own
# fresh PENDING rows even when the same job re-runs against the same CSV file.
# Stored in the jenkins_jobid column as "#9", "#10", etc. matching the Jenkins build number.
JENKINS_JOB_RUN_ID = f"#{JENKINS_BUILD_NUMBER}"
GIT_REMOTE_URL = os.environ.get("GIT_REMOTE_URL")  # Optional override; falls back to repo's origin URL
# The DB repo's origin (staging_deploy.py sets it from the DB clone): a DB ticket's branch FB_<n> lives there, not in
# the WF repo, so the branch pre-check for a DB ticket must look there.
DB_GIT_REMOTE_URL = os.environ.get("DB_GIT_REMOTE_URL")
DB_NODE_VALUE = "C2M-DB"
JENKINS_REMOTE_USER = os.environ.get("JENKINS_REMOTE_USER")
JENKINS_REMOTE_API_TOKEN = os.environ.get("JENKINS_REMOTE_API_TOKEN")

# Jenkins boolean parameter: set to "false" to skip QA test-case generation and
# Assembla upload entirely. The branch existence pre-check still runs; the ticket
# goes straight to READY so Phase 2 can merge it. Default: true.
GENERATE_QA_TESTCASES: bool = False  # removed in this copy: the dev-ticket workflow never creates QA test cases

ASSEMBLA_SPACE = "c2m-ci"
ASSEMBLA_API_HOST = "https://api.assembla.com"
ASSEMBLA_UPLOAD_HOST = "https://bigfiles.assembla.com"

JENKINS_REMOTE_BASE_URL = os.environ.get("JENKINS_REMOTE_BASE_URL") or "https://jenkins-build-server-4.c2minternal.com"
QA_TESTCASES_JOB = "qa_testcases_from_code"
MERGE_DEPLOY_JOB = "C2MC - Merge and Deploy Feature Branch"
JAVA_TRANSFER_JOB = "Java_Transfer_c2m-ci"

# Resolved from this file's own location: AI_DEV_ENV/python_scripts/ sits two levels below the repo root.
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent  # repo root: AI_DEV_ENV/python_scripts/<this file>
DEPLOYMENT_REPORT_DIR = PROJECT_ROOT / "DEV_TICKETS" / "_deploy" / "DEPLOYMENT_REPORT"
TEST_CASES_DIR = PROJECT_ROOT / "DEV_TICKETS" / "_deploy" / "TEST_CASES"

NON_TERMINAL_RESUMABLE_STATUSES = {"CONFIRMED", "QA_TRIGGERED", "QA_DONE"}
PHASE1_TERMINAL_STATUSES = {"READY", "DONE", "SKIPPED", "BRANCH_NOT_FOUND"}
PHASE2_TERMINAL_STATUSES = {"DONE", "SKIPPED", "BRANCH_NOT_FOUND"}
PHASE2_RETRY_CAP = 4

SYNC_RESULT_RE = re.compile(r"SYNC_RESULT:\s*branch=(\S+)\s+merge_result=(\S+)")
TESTCASES_RESULT_RE = re.compile(
    r"TESTCASES_RESULT:\s*status=(\S+)\s+path=(\S+)\s+count=(\S+)"
)
TICKET_DIGITS_RE = re.compile(r"(\d+)$")
BUILD_LIST_RE = re.compile(r"=== Get a list of what to build\. ===\s*\n(\[[^\n]*\])")

CSV_COLUMNS = [
    "deployment_date", "milestone", "ticket_number", "ticket_title", "ticket_id",
    "sync_status", "merge_result", "build_app", "build_result",
    "java_transfer_result", "final_status", "notes", "jenkins_jobid",
]

UNSAFE_FILENAME_CHARS = re.compile(r'[/\\:*?"<>|]')

# ---------------------------------------------------------------------------
# Application -> staging URL mapping for automated QA testing.
# Keys must match the application/project names exactly as they appear in the
# "=== Get a list of what to build. ===" console block of the merge-and-deploy
# job (the same names stored in the CSV's build_app column and returned by
# parse_build_list). Populate with actual staging base URLs once testing
# automation details are confirmed -- the deployed build_list is already
# passed through to the test runner so it can filter to only the apps that
# changed in this run.
# ---------------------------------------------------------------------------
# Keys must match exactly what parse_build_list() returns from the Jenkins
# "C2MC - Merge and Deploy Feature Branch" console output block.
# Each value is the staging base URL for that application.
#
# V3_Runtime
#   Frontend : C2MWFAng (Angular 18, MSAL/Azure-AD auth, pkg-name "listingcms")
#              Routes: /auth/login, /app_list, /process_control/:name,
#                      /announcement, /quickmind, /legal-document, /ask-ai
#   Backend  : C2MWebAPI\C2MWFWebAPI\Controllers\V3
#              (versioned ASP.NET Core controllers: WFApplicationV3, ApplicationV3,
#               DataModel, FormView, ListView, ManageDropdown, DocumentView, etc.)
#
# V4_Runtime
#   Frontend : c2mwf_runtime (Angular 21 standalone, pkg-name "c2mwf-runtime")
#              Routes: /auth/login, /dashboard, /process_control/:processName,
#                      /announcement, /quick-mind
#   Backend  : C2MWebAPI\C2MWFWebAPI\V4  (feature-folder layout)
#              Features: Application (CRUD/forms/grid/records/users/workflows),
#                        Announcement, Chat, Contact, CustomFilter, DocumentView,
#                        LegalDocument, ListView, ManageDropdown, QuickMind,
#                        Attachment, Media, User, Health
#
# Console_Beta
#   Frontend : c2mwf_admin (Angular 20, module-federation, pkg-name "listingcms")
#              Routes: /auth/login, /admin (BM, data-modeler, wf/wf2,
#                      ai-flow-builder2 [AFD2 legacy], ai-flow-builder3 [AFD3 current]),
#                      /bmwfcompare, /users
#   Backend  : wfapi_bm_service  (BM CRUD: BMSyncController, DataModelController)
#              wfapi_wf_service  (WF + AI flows: AIFlowBuilderController,
#                                 WFApplicationController, WFSyncController,
#                                 UploadDocumentController; AFD3 FinalSaveFlow,
#                                 flat-format load, IncompleteNodes validation)
APP_STAGING_URLS: dict = {
    "V3_Runtime":   "https://cloud-staging.c2m.net/WF003YTN/",
    "V4_Runtime":   "https://cloud-staging.c2m.net/WFB05AHD/",
    "Console_Beta": "https://cloud-staging.c2m.net/console-beta",
}

# Cloudflare (fronting api.assembla.com) has been observed intermittently returning
# an empty 204 body for a well-formed, correctly-authenticated request - a curl-like
# User-Agent/Accept pair reduces how often this happens, but AssemblaClient also
# retries on an empty body regardless, since it can't be trusted as "no results" on
# the first occurrence (see CR-9131 slice 01 implementation notes).
HTTP_HEADERS_BASE = {
    "User-Agent": "deploy_on_cloud_staging/1.0 (+Jenkins)",
    "Accept": "*/*",
}


def parse_sync_result(console_text: str):
    """Parse the `SYNC_RESULT: branch=... merge_result=...` marker line."""
    match = SYNC_RESULT_RE.search(console_text)
    if not match:
        return None
    return {"branch": match.group(1), "merge_result": match.group(2)}


def parse_testcases_result(console_text: str):
    """Parse the `TESTCASES_RESULT: status=... path=... count=...` marker line."""
    match = TESTCASES_RESULT_RE.search(console_text)
    if not match:
        return None
    return {"status": match.group(1), "path": match.group(2), "count": match.group(3)}


def parse_build_list(console_text: str):
    """Parse the '=== Get a list of what to build. ===' console block into a plain
    list of application names. Reusable against any build number - slice 04's retry
    logic re-parses this fresh from each re-triggered build rather than reusing a
    stale list from an earlier one."""
    match = BUILD_LIST_RE.search(console_text)
    if not match:
        return None
    return ast.literal_eval(match.group(1))


def describe_phase2_failure(console_text: str) -> str:
    """Best-effort human-readable reason for a Phase 2 build failure, for a
    ticket's CSV notes. The known "already up to date" signature gets a specific
    message; anything else gets a generic one - this script doesn't need to know
    every possible failure mode, only that this ticket dropped out of DevOps
    Staging because of one."""
    if "There are no changes between From and To" in console_text:
        return "branch already up to date, auto-transitioned to Merge Failed"
    return "build failed, ticket auto-dropped from DevOps Staging"


def append_note(existing_notes: str, message: str) -> str:
    existing_notes = (existing_notes or "").strip()
    return f"{existing_notes}; {message}" if existing_notes else message


def ticket_sort_key(ticket_number: str):
    """Sort key giving true numeric ticket-number order (e.g. C2MC-99 before
    C2MC-100), matching the current skill's documented processing order."""
    match = TICKET_DIGITS_RE.search(ticket_number)
    return int(match.group(1)) if match else ticket_number


def escalate(message: str) -> None:
    """Print an ESCALATION line and exit non-zero.

    This is the one non-recoverable stop condition in the whole script - every other
    failure (a single ticket's status, a single Phase 2 build) is isolated and logged
    instead of exiting. Called only when there is nothing safe to isolate and retry
    around (see CR-9131 PRD, Architectural decisions - Escalation control flow).
    """
    print(f"ESCALATION: {message}")
    sys.exit(1)


class AssemblaClient:
    """Wrapper around the Assembla REST API.

    Write capabilities are intentionally narrow: document upload (attachment)
    and ticket status transitions for tickets this script has already
    disqualified from the current deployment cycle (e.g. BRANCH_NOT_FOUND).
    No method here ever silently modifies a ticket's status as a side effect
    of reading — every status write is explicit and logged.
    """

    def __init__(self, api_key: str, api_secret: str, space: str = ASSEMBLA_SPACE):
        self._headers = dict(HTTP_HEADERS_BASE)
        self._headers["X-Api-Key"] = api_key
        self._headers["X-Api-Secret"] = api_secret
        self._space = space

    def _get_json(self, url: str, max_attempts: int = 5, retry_delay_sec: float = 2.0):
        """GET a URL and parse it as JSON, retrying on an empty response body.

        An empty body (observed as HTTP 204 against api.assembla.com, both as a
        transient blip on requests that should return data, and as the genuine
        end-of-pagination signal past the last page) is retried a few times before
        being trusted - but unlike a transient blip, a real end-of-pagination page
        stays empty across every retry. Returns None once retries are exhausted;
        callers that expect a single object (e.g. get_ticket) must treat None as an
        error, while list-based callers may treat it as "nothing more here".
        """
        last_status = None
        for attempt in range(max_attempts):
            request = urllib.request.Request(url, headers=self._headers, method="GET")
            with urllib.request.urlopen(request, timeout=60) as response:
                last_status = response.status
                body = response.read().decode("utf-8")
            if body.strip():
                return json.loads(body)
            if attempt < max_attempts - 1:
                time.sleep(retry_delay_sec)
        print(
            f"Note: {url} returned an empty body on all {max_attempts} attempts "
            f"(last HTTP status {last_status})."
        )
        return None

    def get_ticket(self, ticket_number, max_attempts: int = 3, retry_delay_sec: float = 5.0) -> dict:
        """Fetch one ticket by its number or number-with-prefix (e.g. 'C2MC-18015' or '18015').

        The Assembla individual-ticket endpoint only accepts the bare numeric
        ticket number in the URL path — the prefixed form (C2MC-XXXXX) returns
        HTTP 404 even when the ticket genuinely exists. This method strips the
        prefix before building the URL so callers can safely pass either format.

        Retries up to `max_attempts` times on HTTP errors. Raises RuntimeError
        only after exhausting all attempts so the per-ticket exception handler
        in run_phase1() marks the row SKIPPED and moves on.
        """
        num_match = TICKET_DIGITS_RE.search(str(ticket_number))
        ticket_num = num_match.group(1) if num_match else str(ticket_number)
        url = f"{ASSEMBLA_API_HOST}/v1/spaces/{self._space}/tickets/{ticket_num}.json"
        last_exc = None
        for attempt in range(max_attempts):
            try:
                ticket = self._get_json(url)
                if ticket is not None:
                    return ticket
                # Empty body — _get_json already printed a note; treat as transient.
                last_exc = RuntimeError(f"Assembla returned an empty body for ticket {ticket_number}.")
            except urllib.error.HTTPError as exc:
                last_exc = exc
                print(
                    f"Warning: GET {url} returned HTTP {exc.code} "
                    f"(attempt {attempt + 1}/{max_attempts})."
                )
            if attempt < max_attempts - 1:
                time.sleep(retry_delay_sec)
        raise RuntimeError(
            f"Could not fetch ticket {ticket_number} after {max_attempts} attempts: {last_exc}"
        )

    def resolve_current_milestone(self):
        """Find the milestone whose title contains '_CURRENT'.

        Falls back to the full milestone list if it isn't in the default
        upcoming-milestones view. Returns None if neither list has it - the caller
        must treat that as a cycle-wide escalation, since there is nothing to
        process at all.
        """
        milestone = self._find_current_milestone(
            f"{ASSEMBLA_API_HOST}/v1/spaces/{self._space}/milestones.json"
        )
        if milestone is not None:
            return milestone
        return self._find_current_milestone(
            f"{ASSEMBLA_API_HOST}/v1/spaces/{self._space}/milestones/all.json"
        )

    def _find_current_milestone(self, url: str, per_page: int = 100, max_pages: int = 50):
        """Search every page of a milestone list for the '_CURRENT' one.

        Assembla returns only 10 milestones per request unless told otherwise, and the space
        now has 100+ (400+ in all.json), so an unpaged request never sees the _CURRENT
        milestone and the whole cycle is reported as "nothing to process". Pages are walked
        explicitly, stopping at the first short page.
        """
        for page in range(1, max_pages + 1):
            batch = self._get_json(f"{url}?per_page={per_page}&page={page}") or []
            for milestone in batch:
                if "_CURRENT" in (milestone.get("title") or ""):
                    return milestone
            if len(batch) < per_page:
                break
        return None

    def get_milestone_tickets(self, milestone_id, per_page: int = 100) -> list:
        """Fetch all tickets in a milestone, paginating until exhausted.

        Assembla's milestone endpoint uses 'per_page' (NOT 'page_size'); sending
        'page_size' is silently ignored and the server defaults to 10 per page.
        We paginate explicitly to collect every ticket regardless of how many
        the milestone contains.
        """
        all_tickets: list = []
        page = 1
        while True:
            url = (
                f"{ASSEMBLA_API_HOST}/v1/spaces/{self._space}/tickets/milestone/"
                f"{milestone_id}.json?per_page={per_page}&page={page}"
            )
            batch = self._get_json(url)
            if not batch:
                break
            all_tickets.extend(batch)
            if len(batch) < per_page:
                break
            page += 1
        return all_tickets

    def upload_document(self, ticket_id, file_path: Path) -> dict:
        """Attach a document to a ticket - an attachment, not a status change.

        Must POST to bigfiles.assembla.com, never api.assembla.com - the latter
        Cloudflare-blocks multipart POSTs to this endpoint (confirmed in the current
        skill's own notes). Uses `ticket_id` (Assembla's internal id), not the ticket
        number, as `attachable_id` - the number silently creates an unattached
        document instead of erroring.
        """
        boundary = uuid.uuid4().hex
        body = bytearray()

        def add_field(name: str, value: str) -> None:
            body.extend(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
                f'{value}\r\n'.encode("utf-8")
            )

        add_field("document[attachable_type]", "Ticket")
        add_field("document[attachable_id]", str(ticket_id))
        body.extend(
            f'--{boundary}\r\nContent-Disposition: form-data; name="document[file]"; '
            f'filename="{file_path.name}"\r\nContent-Type: text/csv\r\n\r\n'.encode("utf-8")
        )
        body.extend(file_path.read_bytes())
        body.extend(f"\r\n--{boundary}--\r\n".encode("utf-8"))

        headers = dict(self._headers)
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"

        url = f"{ASSEMBLA_UPLOAD_HOST}/v1/spaces/{self._space}/documents.json"
        request = urllib.request.Request(
            url, data=bytes(body), headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return {
                    "status": response.status,
                    "body": response.read().decode("utf-8", errors="replace"),
                }
        except urllib.error.HTTPError as exc:
            return {
                "status": exc.code,
                "body": exc.read().decode("utf-8", errors="replace"),
            }

    def update_ticket_status(
        self,
        ticket_number,
        new_status: str,
        max_attempts: int = 3,
        retry_delay_sec: float = 5.0,
    ) -> bool:
        """Transition a ticket's Assembla workflow status.

        Used to move a BRANCH_NOT_FOUND ticket out of "DevOps Staging" before
        Phase 2 runs, so the "C2MC - Merge and Deploy Feature Branch" job does
        not pick it up and fail the whole merge cycle.

        The Assembla individual-ticket PUT endpoint accepts the bare numeric
        ticket number in the URL path (same restriction as GET — the C2MC-
        prefix returns 404). The body is a JSON object with a nested "ticket"
        key containing only the fields to update.

        Returns True on success (HTTP 200/204), False after max_attempts.
        """
        num_match = TICKET_DIGITS_RE.search(str(ticket_number))
        ticket_num = num_match.group(1) if num_match else str(ticket_number)
        url = f"{ASSEMBLA_API_HOST}/v1/spaces/{self._space}/tickets/{ticket_num}.json"
        payload = json.dumps({"ticket": {"status": new_status}}).encode("utf-8")
        headers = dict(self._headers)
        headers["Content-Type"] = "application/json"
        last_exc = None
        for attempt in range(max_attempts):
            try:
                request = urllib.request.Request(
                    url, data=payload, headers=headers, method="PUT"
                )
                with urllib.request.urlopen(request, timeout=30) as response:
                    if response.status in (200, 204):
                        return True
                    last_exc = RuntimeError(
                        f"Unexpected HTTP {response.status} from PUT {url}"
                    )
            except urllib.error.HTTPError as exc:
                last_exc = exc
                print(
                    f"Warning: PUT {url} status={new_status!r} returned "
                    f"HTTP {exc.code} (attempt {attempt + 1}/{max_attempts})."
                )
            if attempt < max_attempts - 1:
                time.sleep(retry_delay_sec)
        print(
            f"Warning: could not update ticket {ticket_number} status to "
            f"{new_status!r} after {max_attempts} attempts: {last_exc}"
        )
        return False

    def add_ticket_comment(self, ticket_number_or_id, body: str) -> dict:
        """Post a plain-text or markdown comment on an Assembla ticket.

        Uses the bare numeric ticket number in the URL path (same restriction as
        GET/PUT — the C2MC- prefix returns 404). Accepts either form as input and
        strips the prefix automatically.
        """
        num_match = TICKET_DIGITS_RE.search(str(ticket_number_or_id))
        ticket_num = num_match.group(1) if num_match else str(ticket_number_or_id)
        url = (
            f"{ASSEMBLA_API_HOST}/v1/spaces/{self._space}/tickets/"
            f"{ticket_num}/ticket_comments.json"
        )
        payload = json.dumps({"ticket_comment": {"comment": body}}).encode("utf-8")
        headers = dict(self._headers)
        headers["Content-Type"] = "application/json"
        try:
            request = urllib.request.Request(url, data=payload, headers=headers, method="POST")
            with urllib.request.urlopen(request, timeout=30) as response:
                return {
                    "ok": response.status in (200, 201),
                    "status": response.status,
                    "body": response.read().decode("utf-8", errors="replace"),
                }
        except urllib.error.HTTPError as exc:
            return {
                "ok": False,
                "status": exc.code,
                "body": exc.read().decode("utf-8", errors="replace"),
            }
        except Exception as exc:
            return {"ok": False, "status": 0, "body": str(exc)}


class JenkinsClient:
    """Wrapper around the Jenkins REST API - HTTP Basic Auth via JENKINS_USER/
    JENKINS_API_TOKEN. Hides crumb handling and the 15-second poll cadence behind a
    simple trigger-then-wait interface, reused for every Jenkins job this script
    drives.
    """

    POLL_INTERVAL_SEC = 15
    BUILD_TIMEOUT_SEC = 45 * 60
    QUEUE_TIMEOUT_SEC = 120

    def __init__(self, user: str, api_token: str, base_url: str = JENKINS_REMOTE_BASE_URL):
        self._base_url = base_url.rstrip("/")
        credentials = f"{user}:{api_token}".encode("utf-8")
        self._auth_header = "Basic " + base64.b64encode(credentials).decode("ascii")

    def _headers(self, extra: dict = None) -> dict:
        headers = dict(HTTP_HEADERS_BASE)
        headers["Authorization"] = self._auth_header
        if extra:
            headers.update(extra)
        return headers

    def _request(self, path: str, method: str = "GET", extra_headers: dict = None, data: bytes = None):
        url = f"{self._base_url}{path}"
        request = urllib.request.Request(
            url, data=data, headers=self._headers(extra_headers), method=method
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, response.read().decode("utf-8", errors="replace")

    def _get_json(self, path: str):
        _, body = self._request(path)
        return json.loads(body) if body.strip() else None

    def _fetch_crumb(self):
        data = self._get_json("/crumbIssuer/api/json")
        if not data:
            return None
        return data.get("crumbRequestField"), data.get("crumb")

    def trigger(self, job_name: str, params: dict = None) -> int:
        """Trigger a job (optionally with build parameters) and return its build
        number once Jenkins has actually queued/started it (an entry with
        `building: true` appears)."""
        job_path = urllib.parse.quote(job_name, safe="")
        crumb = self._fetch_crumb()
        extra_headers = {}
        if crumb:
            field, value = crumb
            extra_headers[field] = value

        if params:
            query = urllib.parse.urlencode(params)
            trigger_path = f"/job/{job_path}/buildWithParameters?{query}"
        else:
            trigger_path = f"/job/{job_path}/build?delay=0sec"

        status, _ = self._request(trigger_path, method="POST", extra_headers=extra_headers, data=b"")
        if status not in (200, 201):
            raise RuntimeError(f"Triggering job '{job_name}' failed with HTTP {status}.")

        deadline = time.time() + self.QUEUE_TIMEOUT_SEC
        tree_path = f"/job/{job_path}/api/json?tree=builds[number,building,result,timestamp]{{0,3}}"
        while time.time() < deadline:
            data = self._get_json(tree_path)
            if data:
                for build in data.get("builds", []):
                    if build.get("building"):
                        return build["number"]
            time.sleep(2)
        raise TimeoutError(f"No new build appeared for '{job_name}' within {self.QUEUE_TIMEOUT_SEC}s.")

    def wait_for_terminal(self, job_name: str, build_number: int):
        """Poll a build's consoleText every 15s until it contains `Finished:
        SUCCESS`/`Finished: FAILURE`, or 45 minutes elapse. Returns
        (result, console_text) where result is one of SUCCESS/FAILURE/TIMEOUT."""
        job_path = urllib.parse.quote(job_name, safe="")
        console_path = f"/job/{job_path}/{build_number}/consoleText"
        deadline = time.time() + self.BUILD_TIMEOUT_SEC
        while time.time() < deadline:
            _, console_text = self._request(console_path)
            if "Finished: SUCCESS" in console_text:
                return "SUCCESS", console_text
            if "Finished: FAILURE" in console_text:
                return "FAILURE", console_text
            time.sleep(self.POLL_INTERVAL_SEC)
        _, console_text = self._request(console_path)
        return "TIMEOUT", console_text

    def get_build_timestamp(self, job_name: str, build_number: int) -> int:
        job_path = urllib.parse.quote(job_name, safe="")
        data = self._get_json(f"/job/{job_path}/{build_number}/api/json?tree=timestamp")
        return (data or {}).get("timestamp", 0)

    def job_exists(self, job_name: str) -> bool:
        """True if the Jenkins job exists (HTTP 404 = no such job). Other errors propagate."""
        try:
            self._request(f"/job/{urllib.parse.quote(job_name, safe='')}/api/json?tree=name")
            return True
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return False
            raise

    def wait_for_build_list(self, job_name: str, build_number: int) -> list:
        """Poll a build's console until the '=== Get a list of what to build. ==='
        block appears, or raise if the build finishes without ever printing it."""
        job_path = urllib.parse.quote(job_name, safe="")
        console_path = f"/job/{job_path}/{build_number}/consoleText"
        deadline = time.time() + self.BUILD_TIMEOUT_SEC
        while time.time() < deadline:
            _, console_text = self._request(console_path)
            build_list = parse_build_list(console_text)
            if build_list is not None:
                return build_list
            if "Finished: SUCCESS" in console_text or "Finished: FAILURE" in console_text:
                raise RuntimeError(
                    f"Build #{build_number} of '{job_name}' finished without ever "
                    f"printing the 'what to build' list."
                )
            time.sleep(self.POLL_INTERVAL_SEC)
        raise TimeoutError(
            f"Build #{build_number} of '{job_name}' never printed the 'what to "
            f"build' list within {self.BUILD_TIMEOUT_SEC}s."
        )

    def wait_for_downstream_completions(self, job_name: str, after_timestamp: int, expected_count: int = 1) -> list:
        """Poll a job this script never triggered itself - the per-application
        builds and Java_Transfer are both triggered by the merge-and-deploy job's
        own downstream automation - until every build with timestamp >=
        after_timestamp has reached a terminal (non-building) state, and at least
        expected_count such builds exist. Returns those builds' raw dicts."""
        job_path = urllib.parse.quote(job_name, safe="")
        tree_path = f"/job/{job_path}/api/json?tree=builds[number,building,result,timestamp]{{0,50}}"
        deadline = time.time() + self.BUILD_TIMEOUT_SEC
        while time.time() < deadline:
            try:
                data = self._get_json(tree_path)
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    raise RuntimeError(
                        f"Jenkins job '{job_name}' not found (HTTP 404). "
                        f"This entry in the build list is not a real Jenkins job — "
                        f"check the '=== Get a list of what to build. ===' block in "
                        f"the merge-and-deploy console."
                    ) from exc
                raise
            matching = [
                b for b in (data or {}).get("builds", [])
                if b.get("timestamp", 0) >= after_timestamp
            ]
            terminal = [b for b in matching if b.get("building") is False]
            if len(terminal) >= expected_count and len(terminal) == len(matching):
                return terminal
            time.sleep(self.POLL_INTERVAL_SEC)
        raise TimeoutError(
            f"Fewer than {expected_count} completed build(s) for '{job_name}' with "
            f"timestamp >= {after_timestamp} within {self.BUILD_TIMEOUT_SEC}s."
        )

    def get_build_artifacts(self, job_name: str, build_number: int) -> list:
        job_path = urllib.parse.quote(job_name, safe="")
        data = self._get_json(
            f"/job/{job_path}/{build_number}/api/json?tree=artifacts[fileName,relativePath]"
        )
        return (data or {}).get("artifacts", [])

    def download_artifact(self, job_name: str, build_number: int, relative_path: str, dest_path: Path) -> None:
        """Download a build artifact directly - this script runs as a plain process
        on the Jenkins host itself, so (unlike the Claude-driven skill it replaces)
        there is no sandboxing proxy blocking a normal artifact download; the old
        Jenkins Script Console workaround is unnecessary here."""
        job_path = urllib.parse.quote(job_name, safe="")
        artifact_path = "/".join(urllib.parse.quote(part) for part in relative_path.split("/"))
        url = f"{self._base_url}/job/{job_path}/{build_number}/artifact/{artifact_path}"
        request = urllib.request.Request(url, headers=self._headers(), method="GET")
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(request, timeout=120) as response:
            dest_path.write_bytes(response.read())


class PhaseTwoState:
    """Durable Phase 2 state: the in-flight merge-and-deploy build number and
    per-ticket retry-failure tracking, in a small companion file next to the CSV
    (never the CSV itself). Written only at the two moments CR-9131 slice 04
    specifies - immediately after triggering, and immediately after a terminal
    result's CSV updates are all complete - never on an intermediate poll.

    Retry counts key off the build number that caused each failure (not just a
    bare counter), so re-processing the same build's failure after a crash can
    never double-increment a ticket's count - a redo for a build already charged
    to a ticket is a no-op for that ticket.
    """

    def __init__(self, state_path: Path):
        self.state_path = state_path
        self._data = self._load()

    def _load(self) -> dict:
        if self.state_path.exists():
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        return {"build_number": None, "retry_counts": {}}

    def _save(self) -> None:
        self.state_path.write_text(json.dumps(self._data, indent=2), encoding="utf-8")

    @property
    def build_number(self):
        return self._data.get("build_number")

    def set_build_number(self, build_number) -> None:
        """Moment 1: immediately after triggering/re-triggering."""
        self._data["build_number"] = build_number
        self._data["pending_drops"] = None
        self._save()

    @property
    def pending_drops(self):
        """The write-ahead record of which tickets were determined to have
        dropped out of DevOps Staging for the current build, if a failure's CSV
        updates were interrupted mid-way through. None means no failure is being
        processed, or none was pending when the process last stopped."""
        return self._data.get("pending_drops")

    def record_pending_drops(self, ticket_numbers: list) -> None:
        """Write-ahead: record which tickets are about to be marked SKIPPED for
        the current build's failure, BEFORE touching any CSV row. Without this, a
        crash partway through updating those rows would make a resume re-derive
        "which tickets dropped" from a CSV that's now partially mutated -
        already-processed tickets would look like they never dropped, and a
        redo could misfire into a false "nothing dropped" cycle-wide escalation.
        With it, a redo replays this exact predetermined list instead."""
        self._data["pending_drops"] = list(ticket_numbers)
        self._save()

    def clear_build_number(self) -> None:
        """Moment 2's persist point: writes build_number=None together with
        whatever retry_counts mutations record_failure() has already applied
        in-memory, as one single atomic file write - not two separate writes -
        so a crash can never land between "retry counts saved" and "build number
        cleared" (which would otherwise make a resume re-run the same failure's
        drop-detection against candidates that already excluded the ticket it
        isolated, misfiring into a false cycle-wide escalation)."""
        self._data["build_number"] = None
        self._data["pending_drops"] = None
        self._save()

    def retry_count_for(self, ticket_number: str) -> int:
        return self._data["retry_counts"].get(ticket_number, {}).get("count", 0)

    def record_failure(self, ticket_number: str, failed_build_number) -> int:
        """Increment this ticket's retry count for `failed_build_number`'s failure,
        unless it was already charged for this exact build. Does not persist by
        itself - call save() once, after every CSV row update for this failure
        batch is complete, so both land as one durable unit before the next
        trigger."""
        entry = self._data["retry_counts"].get(
            ticket_number, {"count": 0, "last_failed_build": None}
        )
        if entry.get("last_failed_build") != failed_build_number:
            entry["count"] += 1
            entry["last_failed_build"] = failed_build_number
            self._data["retry_counts"][ticket_number] = entry
        return entry["count"]

    @staticmethod
    def build_state_path(csv_path: Path) -> Path:
        return csv_path.parent / f"{csv_path.name}.state.json"


class ReportStore:
    """Durable per-cycle state: the deployment-report CSV.

    Hides file I/O and CSV escaping (via the stdlib `csv` module, so commas/quotes in
    ticket titles are handled correctly) and makes a re-run resumable - it never
    duplicates a ticket already present in today's file.
    """

    def __init__(self, csv_path: Path):
        self.csv_path = csv_path

    def ensure_file(self) -> None:
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.csv_path.exists():
            with open(self.csv_path, "w", newline="", encoding="utf-8") as f:
                csv.DictWriter(f, fieldnames=CSV_COLUMNS).writeheader()

    def existing_ticket_numbers(self) -> set:
        """Return ticket numbers already present for THIS Jenkins build run.

        Scoped to JENKINS_JOB_RUN_ID (job name + build number) so each build
        always appends fresh rows, even when the same ticket appeared in a
        prior build's rows.
        """
        if not self.csv_path.exists():
            return set()
        with open(self.csv_path, "r", newline="", encoding="utf-8") as f:
            return {
                row["ticket_number"]
                for row in csv.DictReader(f)
                if row.get("jenkins_jobid") == JENKINS_JOB_RUN_ID
            }

    def append_pending_rows(self, tickets: list, milestone_title: str) -> int:
        """Append one PENDING row per ticket not already present for this build run.

        Each Jenkins build run appends its own fresh rows — prior runs' rows are
        left intact as history. Dedup is scoped to JENKINS_JOB_RUN_ID (job + build
        number) so re-triggering the same job never produces duplicate rows within
        that run, but a new run always gets a clean slate even for previously
        SKIPPED or BRANCH_NOT_FOUND tickets.

        Returns the number of rows actually appended.
        """
        already_present = self.existing_ticket_numbers()
        new_rows = [
            {
                "deployment_date": date.today().isoformat(),
                "milestone": milestone_title,
                "ticket_number": ticket["number_with_prefix"],
                "ticket_title": ticket.get("summary", ""),
                "ticket_id": "",
                "sync_status": "PENDING",
                "merge_result": "",
                "build_app": "",
                "build_result": "",
                "java_transfer_result": "",
                "final_status": "",
                "notes": "",
                "jenkins_jobid": JENKINS_JOB_RUN_ID,
            }
            for ticket in tickets
            if ticket["number_with_prefix"] not in already_present
        ]
        if new_rows:
            with open(self.csv_path, "a", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
                writer.writerows(new_rows)
        return len(new_rows)

    def read_rows(self) -> list:
        """Return ALL rows from the CSV, across all job runs."""
        with open(self.csv_path, "r", newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def read_current_rows(self) -> list:
        """Return only the rows that belong to this Jenkins build run (JENKINS_JOB_RUN_ID)."""
        return [r for r in self.read_rows() if r.get("jenkins_jobid") == JENKINS_JOB_RUN_ID]

    def _write_rows(self, rows: list) -> None:
        """Write all rows back to the CSV, with retry on PermissionError (e.g. file open in Excel)."""
        max_attempts = 6
        retry_delay = 5.0
        last_exc = None
        for attempt in range(max_attempts):
            try:
                with open(self.csv_path, "w", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
                    writer.writeheader()
                    writer.writerows(rows)
                return
            except PermissionError as exc:
                last_exc = exc
                print(
                    f"  WARNING: CSV file is locked by another process "
                    f"(attempt {attempt + 1}/{max_attempts}). "
                    f"Close it in Excel if open, retrying in {retry_delay:.0f}s..."
                )
                if attempt < max_attempts - 1:
                    time.sleep(retry_delay)
        raise PermissionError(
            f"Could not write to {self.csv_path} after {max_attempts} attempts: {last_exc}"
        )

    def reset_non_terminal_to_pending(self) -> int:
        """Reset any row left at CONFIRMED/QA_TRIGGERED/QA_DONE (from a prior crash)
        back to PENDING so it's reprocessed from scratch - Phase 1 per-ticket work is
        cheap and idempotent, so a full redo is simpler than reattaching to whatever
        Jenkins build was in flight when the script stopped.

        Scoped to the current JENKINS_JOB_RUN_ID so prior builds' rows are untouched.
        """
        all_rows = self.read_rows()
        reset_count = 0
        for row in all_rows:
            if (
                row.get("jenkins_jobid") == JENKINS_JOB_RUN_ID
                and row["sync_status"] in NON_TERMINAL_RESUMABLE_STATUSES
            ):
                row["sync_status"] = "PENDING"
                reset_count += 1
        if reset_count:
            self._write_rows(all_rows)
        return reset_count

    def next_unfinished_ticket_number(self):
        """The next ticket (in ticket-number order) not yet READY/DONE/SKIPPED, or
        None if every row has reached a terminal Phase 1/2 state.

        Scoped to the current Jenkins job run via read_current_rows().
        """
        unfinished = [
            row["ticket_number"] for row in self.read_current_rows()
            if row["sync_status"] not in PHASE1_TERMINAL_STATUSES
        ]
        if not unfinished:
            return None
        return min(unfinished, key=ticket_sort_key)

    def update_row(self, ticket_number: str, **fields) -> None:
        """Update the row for ticket_number belonging to this Jenkins build run.

        Matches on both ticket_number and jenkins_jobid (JENKINS_JOB_RUN_ID) so
        that a ticket that appeared in an earlier build run is not accidentally mutated.
        """
        all_rows = self.read_rows()
        for row in all_rows:
            if (
                row["ticket_number"] == ticket_number
                and row.get("jenkins_jobid") == JENKINS_JOB_RUN_ID
            ):
                row.update(fields)
                break
        else:
            raise KeyError(
                f"No CSV row found for ticket {ticket_number} in job run {JENKINS_JOB_RUN_ID}."
            )
        self._write_rows(all_rows)

    @staticmethod
    def build_csv_path(milestone_title: str) -> Path:
        safe_name = milestone_title.replace(" ", "_")
        safe_name = UNSAFE_FILENAME_CHARS.sub("_", safe_name)
        return DEPLOYMENT_REPORT_DIR / f"{safe_name}-{date.today().isoformat()}.csv"


# ---------------------------------------------------------------------------
# Git helpers — branch existence and merge checks before triggering builds.
# ---------------------------------------------------------------------------

def _get_git_remote_url() -> str:
    """Return the remote URL for git branch checks.

    Prefers GIT_REMOTE_URL from the environment; falls back to reading
    origin's URL from the project repo (this script lives inside a git
    clone, so `git remote get-url origin` is always available).
    Returns an empty string if neither source yields a value.
    """
    if GIT_REMOTE_URL:
        return GIT_REMOTE_URL
    try:
        result = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=15, check=False,
        )
        return result.stdout.strip()
    except Exception as exc:
        print(f"Warning: could not read git remote URL: {exc}")
        return ""


def is_db_ticket(ticket: dict) -> bool:
    """A DB ticket (Node = C2M-DB, or titled `DB | #N ...`): its branch is in the DB repo."""
    node = ((ticket.get("custom_fields") or {}).get("Node") or "").strip()
    return node == DB_NODE_VALUE or bool(re.match(r"^DB \| #\d+", ticket.get("summary") or ""))


def branch_exists_in_remote(branch_name: str, remote_url: str = None) -> bool:
    """Return True if `branch_name` exists in the remote repo (the WF repo unless `remote_url` is given).

    Uses `git ls-remote --heads` — no local clone state required.
    Defaults to True (assume exists) when the URL is unknown or the command
    fails, so the QA job is still triggered and the existing FAILED/SKIPPED
    path handles the missing branch — no regression risk.
    """
    remote_url = remote_url or _get_git_remote_url()
    if not remote_url:
        return True  # fail-open: let the QA job detect the missing branch
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--heads", remote_url, branch_name],
            capture_output=True, text=True, timeout=30, check=False,
        )
        return bool(result.stdout.strip())
    except Exception as exc:
        print(f"Warning: branch existence check failed for {branch_name}: {exc}")
        return True  # fail-open


def branch_has_unmerged_commits(branch_name: str) -> bool:
    """Return True if `branch_name` has commits not yet in master.

    Fetches both refs into the local repo clone (authoritative comparison),
    then runs `git log origin/master..origin/<branch>`. An empty log means
    the branch is fully merged; any output means it isn't.

    Defaults to True (treat as unmerged) on any error — Phase 1 re-runs
    and produces the correct outcome naturally (fail-open).
    """
    remote_url = _get_git_remote_url()
    if not remote_url:
        return True
    try:
        subprocess.run(
            [
                "git", "-C", str(PROJECT_ROOT), "fetch", "--quiet", "origin",
                f"refs/heads/{branch_name}:refs/remotes/origin/{branch_name}",
                "refs/heads/master:refs/remotes/origin/master",
            ],
            capture_output=True, timeout=60, check=False,
        )
        result = subprocess.run(
            [
                "git", "-C", str(PROJECT_ROOT), "log",
                f"origin/master..origin/{branch_name}", "--oneline",
            ],
            capture_output=True, text=True, timeout=15, check=False,
        )
        return bool(result.stdout.strip())
    except Exception as exc:
        print(f"Warning: unmerged-commits check failed for {branch_name}: {exc}")
        return True  # fail-open


def process_ticket_phase1(
    ticket_number: str,
    assembla: AssemblaClient,
    jenkins: JenkinsClient,
    report: ReportStore,
    generate_qa: bool = True,
) -> "Path | None":
    """Returns the Path of the test-case CSV downloaded this run, or None."""
    """Take one ticket through QA test-case generation. Any failure here marks the
    row SKIPPED with a reason and returns - never raises, never exits the script.

    When generate_qa is False the qa_testcases_from_code job is skipped entirely:
    the branch existence pre-check still runs, and the ticket goes straight to READY
    so Phase 2 can proceed with the merge.
    """
    print(f"--- Phase 1: {ticket_number} ---")
    ticket = assembla.get_ticket(ticket_number)

    if ticket.get("status") != "DevOps Staging":
        report.update_row(
            ticket_number,
            sync_status="SKIPPED",
            notes=(
                f"Ticket {ticket_number} - expected DevOps Staging but found "
                f"{ticket.get('status')}, skipped"
            ),
        )
        return

    report.update_row(ticket_number, sync_status="CONFIRMED", ticket_id=str(ticket["id"]))

    branch = f"FB_{ticket['number']}"

    # A DB ticket's branch is in the DB repo. Looking for it in the WF repo would "not find" it and move the ticket
    # to QA Queue WITHOUT deploying anything, so a DB ticket is checked against the DB repo, and left alone when that
    # repo's URL is not known.
    db_ticket = is_db_ticket(ticket)
    remote_url = None
    if db_ticket:
        remote_url = DB_GIT_REMOTE_URL
        if not remote_url:
            print(f"  [{ticket_number}] DB ticket but DB_GIT_REMOTE_URL is not set: not checking or moving it.")
            report.update_row(
                ticket_number,
                sync_status="SKIPPED",
                notes=f"Ticket {ticket_number} - DB ticket, DB_GIT_REMOTE_URL not set; left in DevOps Staging",
            )
            return

    # Pre-check: skip immediately if the branch doesn't exist in the remote,
    # rather than wasting a Jenkins build slot on a guaranteed failure.
    if not branch_exists_in_remote(branch, remote_url):
        print(
            f"  [{ticket_number}] BRANCH_NOT_FOUND — {branch} not in {'the DB repo' if db_ticket else 'remote'}. "
            f"Moving ticket from DevOps Staging → QA Queue in Assembla..."
        )
        status_updated = assembla.update_ticket_status(ticket_number, "QA Queue")
        if status_updated:
            print(f"  [{ticket_number}] Assembla status updated to QA Queue.")
        else:
            print(
                f"  [{ticket_number}] WARNING: Could not update Assembla status. "
                f"The ticket may still appear in DevOps Staging and be picked up by "
                f"the merge job — please move it manually."
            )
        report.update_row(
            ticket_number,
            sync_status="BRANCH_NOT_FOUND",
            merge_result="FAILED",
            notes=(
                f"Ticket {ticket_number} — branch {branch} not found in remote; "
                f"skipped without triggering a build; "
                f"Assembla status {'moved to QA Queue' if status_updated else 'NOT updated — move manually'}"
            ),
        )
        return

    if not generate_qa:
        print(f"  [{ticket_number}] QA test-case generation skipped (GENERATE_QA_TESTCASES=false). Marking READY.")
        report.update_row(
            ticket_number,
            sync_status="READY",
            notes=f"Ticket {ticket_number} - QA test-case generation skipped by parameter; branch {branch} exists",
        )
        return

    build_number = jenkins.trigger(QA_TESTCASES_JOB, params={"GIT_BRANCH": branch})
    report.update_row(ticket_number, sync_status="QA_TRIGGERED")

    build_result, console = jenkins.wait_for_terminal(QA_TESTCASES_JOB, build_number)

    sync_result = parse_sync_result(console)
    if sync_result is None:
        report.update_row(
            ticket_number,
            sync_status="SKIPPED",
            notes=(
                f"Ticket {ticket_number} - qa_testcases_from_code build #{build_number} "
                f"never resolved a branch to sync (check GIT_BRANCH), skipped"
            ),
        )
        return

    merge_result = sync_result["merge_result"]
    report.update_row(ticket_number, merge_result=merge_result)

    if merge_result not in ("SUCCESS", "ALREADY_UP_TO_DATE"):
        reasons = {
            "FAILED": f"branch {branch} not found (or sync error), skipped",
            "CONFLICT": "merge conflict with master, skipped (needs manual resolution)",
            "PUSH_FAILED": "push failed, skipped",
        }
        reason = reasons.get(
            merge_result,
            f"qa_testcases_from_code build #{build_number} failed, QA CSV skipped",
        )
        report.update_row(
            ticket_number, sync_status="SKIPPED", notes=f"Ticket {ticket_number} - {reason}"
        )
        return

    if build_result != "SUCCESS":
        report.update_row(
            ticket_number,
            sync_status="SKIPPED",
            notes=(
                f"Ticket {ticket_number} - qa_testcases_from_code build #{build_number} "
                f"{build_result.lower()}, QA CSV skipped"
            ),
        )
        return

    report.update_row(ticket_number, sync_status="QA_DONE")

    testcases_result = parse_testcases_result(console)
    if testcases_result is None:
        report.update_row(
            ticket_number,
            sync_status="SKIPPED",
            notes=(
                f"Ticket {ticket_number} - qa_testcases_from_code build #{build_number} "
                f"produced no TESTCASES_RESULT marker, skipped"
            ),
        )
        return

    status = testcases_result["status"]
    if status == "skipped":
        report.update_row(
            ticket_number,
            sync_status="READY",
            notes=(
                f"Ticket {ticket_number} - no committed differences vs. master, "
                f"QA CSV skipped"
            ),
        )
        return

    if status != "ok":
        report.update_row(
            ticket_number,
            sync_status="SKIPPED",
            notes=(
                f"Ticket {ticket_number} - qa_testcases_from_code reported "
                f"TESTCASES_RESULT status={status}, needs review"
            ),
        )
        return

    artifacts = jenkins.get_build_artifacts(QA_TESTCASES_JOB, build_number)
    if not artifacts:
        report.update_row(
            ticket_number,
            sync_status="READY",
            notes=(
                f"Ticket {ticket_number} - TESTCASES_RESULT reported status=ok but no "
                f"build artifact was found; QA CSV not attached"
            ),
        )
        return

    # Use the path reported by TESTCASES_RESULT to select the specific artifact,
    # not artifacts[0] which sorts alphabetically and may pick a stale file from
    # a previous run (the Jenkins TEST_CASES/ folder accumulates all past CSVs).
    tc_path = testcases_result["path"].replace("\\", "/")
    artifact = next(
        (a for a in artifacts if a["relativePath"].replace("\\", "/") == tc_path),
        None,
    )
    if artifact is None:
        print(
            f"  [{ticket_number}] Could not find artifact matching path '{tc_path}' in build #{build_number}. "
            f"Available artifacts: {[a['relativePath'] for a in artifacts]}. Skipping upload."
        )
        report.update_row(
            ticket_number,
            sync_status="READY",
            notes=(
                f"Ticket {ticket_number} - TESTCASES_RESULT path '{tc_path}' not found as a build artifact; "
                f"QA CSV not attached"
            ),
        )
        return
    local_path = TEST_CASES_DIR / artifact["fileName"]
    jenkins.download_artifact(
        QA_TESTCASES_JOB, build_number, artifact["relativePath"], local_path
    )

    upload = assembla.upload_document(ticket["id"], local_path)
    upload_confirmed = False
    if upload["status"] == 201 and upload["body"].strip():
        try:
            parsed = json.loads(upload["body"])
            upload_confirmed = (
                parsed.get("attachable_type") == "Ticket"
                and str(parsed.get("attachable_id")) == str(ticket["id"])
            )
        except json.JSONDecodeError:
            upload_confirmed = False

    if upload_confirmed:
        note = f"Ticket {ticket_number} - QA test case CSV attached to Assembla"
    else:
        note = (
            f"Ticket {ticket_number} - QA test case CSV upload could not be "
            f"confirmed (HTTP {upload['status']})"
        )
    report.update_row(ticket_number, sync_status="READY", notes=note)
    return local_path


def run_phase1(
    ticket_number: str,
    assembla: AssemblaClient,
    jenkins: JenkinsClient,
    report: ReportStore,
    generate_qa: bool = True,
) -> "Path | None":
    """QA test-case generation for the single selected ticket.

    Returns the Path of the test-case CSV downloaded this run, or None when
    QA generation was skipped or failed.

    Resets any non-terminal row left by a previous crash back to PENDING before
    running so the step is safely re-entrant. When generate_qa is False the
    qa_testcases_from_code job is skipped and the ticket goes straight to READY
    after the branch existence check.
    """
    reset_count = report.reset_non_terminal_to_pending()
    if reset_count:
        print(f"Reset {reset_count} row(s) left at a non-terminal state to PENDING.")

    try:
        return process_ticket_phase1(ticket_number, assembla, jenkins, report, generate_qa=generate_qa)
    except Exception as exc:
        report.update_row(
            ticket_number,
            sync_status="SKIPPED",
            notes=f"Ticket {ticket_number} - unexpected error: {exc}",
        )
        return None


class Phase2BuildFailure(Exception):
    """Raised when a tracked application or Java_Transfer build (not the parent
    merge-and-deploy job itself) comes back FAILURE. Carries no console text of
    its own - the "already up to date" signature only ever appears in the parent
    job's console - so it's handled identically to a parent-job failure, just
    with an empty console (describe_phase2_failure then correctly falls back to
    the generic reason)."""


def _fill_na_blanks(report: ReportStore, row: dict) -> None:
    blanks = {
        field: "N/A"
        for field in ("build_app", "build_result", "java_transfer_result", "final_status")
        if not row.get(field)
    }
    if blanks:
        report.update_row(row["ticket_number"], **blanks)


def _finalize_ready_rows(assembla: AssemblaClient, report: ReportStore, build_app: str, build_result: str) -> None:
    for row in report.read_current_rows():
        if row["sync_status"] == "READY":
            ticket = assembla.get_ticket(row["ticket_number"])
            report.update_row(
                row["ticket_number"],
                build_app=build_app,
                build_result=build_result,
                java_transfer_result="SUCCESS",
                final_status=ticket.get("status", ""),
                sync_status="DONE",
            )
        elif row["sync_status"] in ("SKIPPED", "BRANCH_NOT_FOUND"):
            _fill_na_blanks(report, row)


def _finalize_skipped_blanks(report: ReportStore) -> None:
    for row in report.read_current_rows():
        if row["sync_status"] in ("SKIPPED", "BRANCH_NOT_FOUND"):
            _fill_na_blanks(report, row)


def _handle_phase2_failure(
    build_number: int,
    console_text: str,
    assembla: AssemblaClient,
    report: ReportStore,
    state: PhaseTwoState,
) -> bool:
    """A Phase 2 build (parent, application, or transfer) came back FAILURE.
    Isolate any ticket that dropped out of DevOps Staging because of it (marking
    it SKIPPED and bumping its persisted retry count), or escalate the whole
    cycle if none did - there's nothing ticket-specific to isolate and retry
    around. Returns True if the caller should re-trigger and keep going, False if
    the cycle has nothing left to build.
    """
    pending = state.pending_drops
    if pending is not None:
        # Resuming after a crash mid-way through a previous pass over this same
        # build's failure - replay the exact predetermined ticket list rather
        # than re-deriving "who dropped" from a CSV that may already be
        # partially updated (which would make an already-processed ticket look
        # like it never dropped at all).
        dropped_tickets = pending
    else:
        rows_by_ticket = {r["ticket_number"]: r for r in report.read_current_rows()}
        candidates = [
            t for t, r in rows_by_ticket.items() if r["sync_status"] not in PHASE2_TERMINAL_STATUSES
        ]
        dropped_tickets = [
            t for t in candidates if assembla.get_ticket(t).get("status") != "DevOps Staging"
        ]

        if not dropped_tickets:
            message = (
                f"Merge-and-deploy build #{build_number} FAILED with no ticket-specific "
                f"cause detected (no cycle ticket left DevOps Staging)."
            )
            for ticket_number in candidates:
                report.update_row(
                    ticket_number,
                    notes=append_note(rows_by_ticket[ticket_number]["notes"], f"ESCALATION: {message}"),
                )
            state.clear_build_number()
            escalate(message)

        # Write-ahead: record the list before touching any CSV row.
        state.record_pending_drops(dropped_tickets)

    reason = describe_phase2_failure(console_text)
    for ticket_number in dropped_tickets:
        ticket = assembla.get_ticket(ticket_number)
        new_status = ticket.get("status")
        retry_count = state.record_failure(ticket_number, build_number)
        if retry_count >= PHASE2_RETRY_CAP:
            note = (
                f'Ticket {ticket_number} - repeated "{reason}" failures, gave up after '
                f'{PHASE2_RETRY_CAP - 1} retries; left in {new_status} for manual review'
            )
        else:
            note = f"Ticket {ticket_number} - {reason} (now {new_status}, retry {retry_count})"
        existing_row = next(
            (r for r in report.read_current_rows() if r["ticket_number"] == ticket_number), {}
        )
        report.update_row(
            ticket_number,
            sync_status="SKIPPED",
            notes=append_note(existing_row.get("notes", ""), note),
        )

    remaining = [
        r["ticket_number"] for r in report.read_current_rows()
        if r["sync_status"] not in PHASE2_TERMINAL_STATUSES
    ]
    state.clear_build_number()
    if not remaining:
        _finalize_skipped_blanks(report)
        return False
    return True


def run_phase2(jenkins: JenkinsClient, assembla: AssemblaClient, report: ReportStore, state: PhaseTwoState) -> None:
    """Trigger (or resume tracking of) the merge-and-deploy build through to
    Java_Transfer completion for the single selected ticket.

    Crash-safe: a previously recorded build number in PhaseTwoState is reused
    so a mid-run restart re-attaches to the in-flight build rather than
    triggering a duplicate. On a build failure the ticket is marked SKIPPED
    and the function returns — there is no retry in single-ticket mode.
    """
    build_number = state.build_number
    if build_number is None:
        build_number = jenkins.trigger(MERGE_DEPLOY_JOB)
        state.set_build_number(build_number)
        print(f"Merge-and-deploy build #{build_number} triggered.")
    else:
        print(f"Resuming tracking of merge-and-deploy build #{build_number} from prior state.")

    parent_timestamp = jenkins.get_build_timestamp(MERGE_DEPLOY_JOB, build_number)
    result, console = jenkins.wait_for_terminal(MERGE_DEPLOY_JOB, build_number)

    if result == "TIMEOUT":
        state.clear_build_number()
        escalate(
            f"Merge-and-deploy build #{build_number} did not reach a terminal "
            f"state within {JenkinsClient.BUILD_TIMEOUT_SEC}s."
        )

    if result == "FAILURE":
        _handle_phase2_failure(build_number, console, assembla, report, state)
        return

    # result == "SUCCESS" - track the applications and Java_Transfer.
    try:
        build_list_raw = jenkins.wait_for_build_list(MERGE_DEPLOY_JOB, build_number)
        # Keep only entries that are real Jenkins jobs. The build list is derived from the folders a
        # branch changes, so tooling/doc folders (AI_DEV_ENV, docs, ...) and masked names (c2mwf_****)
        # show up in it; asking the server is exact, a name pattern is not.
        build_list = [
            app for app in build_list_raw
            if "*" not in app and not app.startswith("_tmp_") and jenkins.job_exists(app)
        ]
        skipped = [app for app in build_list_raw if app not in build_list]
        if skipped:
            print(f"  WARNING: skipped non-job entries from build list: {skipped}")
        print(f"Applications to build this cycle: {build_list}")
        if not build_list:
            print("WARNING: no applications to build this cycle - nothing was built or deployed. "
                  "(The merge only touched non-job folders, or the code was already merged by an earlier run.)")

        build_results = []
        for app in build_list:
            completed = jenkins.wait_for_downstream_completions(app, parent_timestamp, expected_count=1)
            app_build = completed[0]
            if app_build.get("result") != "SUCCESS":
                raise Phase2BuildFailure(
                    f"Application build '{app}' #{app_build.get('number')} finished "
                    f"with result={app_build.get('result')} (expected SUCCESS)."
                )
            build_results.append("SUCCESS")
            print(f"Application '{app}' build #{app_build['number']}: SUCCESS")

        transfer_builds = jenkins.wait_for_downstream_completions(
            JAVA_TRANSFER_JOB, parent_timestamp, expected_count=len(build_list)
        )
        for transfer_build in transfer_builds:
            if transfer_build.get("result") != "SUCCESS":
                raise Phase2BuildFailure(
                    f"Java_Transfer build #{transfer_build.get('number')} finished "
                    f"with result={transfer_build.get('result')} (expected SUCCESS)."
                )
        print(f"Java_Transfer: {len(transfer_builds)} matching run(s), all SUCCESS.")
    except Phase2BuildFailure as exc:
        print(f"Phase 2 failure: {exc}")
        _handle_phase2_failure(build_number, "", assembla, report, state)
        return

    _finalize_ready_rows(assembla, report, "; ".join(build_list), "; ".join(build_results))
    state.clear_build_number()
    return


def main() -> int:
    if not ASSEMBLA_API_KEY or not ASSEMBLA_API_SECRET:
        escalate("ASSEMBLA_API_KEY/ASSEMBLA_API_SECRET are not set.")
    if not JENKINS_REMOTE_USER or not JENKINS_REMOTE_API_TOKEN:
        escalate("JENKINS_REMOTE_USER/JENKINS_REMOTE_API_TOKEN are not set (AI_DEV_ENV/credentials.env).")

    assembla = AssemblaClient(ASSEMBLA_API_KEY, ASSEMBLA_API_SECRET)
    jenkins_remote = JenkinsClient(JENKINS_REMOTE_USER, JENKINS_REMOTE_API_TOKEN, JENKINS_REMOTE_BASE_URL)

    milestone = assembla.resolve_current_milestone()
    if milestone is None:
        escalate(
            "No milestone with '_CURRENT' in its title was found in "
            "milestones.json or milestones/all.json - nothing to process this cycle."
        )

    print(f"Resolved milestone: {milestone['title']} (id={milestone['id']})")

    # Fetch all tickets in the _CURRENT milestone, then filter for the exact
    # combination the DevOps team acts on: Plan=Story, Status=DevOps Staging.
    # hierarchy_type 1 == Story in Assembla's ticket hierarchy.
    milestone_tickets = assembla.get_milestone_tickets(milestone["id"])

    all_devops_staging = [
        t for t in milestone_tickets
        if t.get("status") == "DevOps Staging"
        and t.get("hierarchy_type") == 2  # Story in this workspace
    ]
    print(
        f"Found {len(all_devops_staging)} Story ticket(s) in DevOps Staging "
        f"in milestone '{milestone['title']}' "
        f"(scanned {len(milestone_tickets)} total milestone ticket(s))."
    )

    # Single-ticket-per-run mode: pick the lowest-numbered ticket and deploy it
    # end-to-end. Re-trigger the Jenkins job to deploy the next ticket in the queue.
    if not all_devops_staging:
        print("No tickets in DevOps Staging -- nothing to deploy this run.")
        return 0
    next_ticket = min(all_devops_staging, key=lambda t: ticket_sort_key(t.get("number_with_prefix", "")))
    devops_staging_stories = [next_ticket]
    remaining = len(all_devops_staging) - 1
    print(
        f"Single-ticket mode: selected {next_ticket['number_with_prefix']} for this run. "
        + (f"{remaining} ticket(s) remaining in DevOps Staging queue -- re-trigger to deploy next." if remaining else "This is the only ticket in the queue.")
    )

    csv_path = ReportStore.build_csv_path(milestone["title"])
    report = ReportStore(csv_path)
    report.ensure_file()
    added = report.append_pending_rows(devops_staging_stories, milestone["title"])
    print(f"Deployment report: {csv_path} ({added} new PENDING row(s) added)")
    print(f"QA test-case generation: {'ENABLED' if GENERATE_QA_TESTCASES else 'DISABLED (GENERATE_QA_TESTCASES=false)'}")

    run_phase1(next_ticket["number_with_prefix"], assembla, None, report, generate_qa=False)

    # Point 2: only trigger Phase 2 if at least one ticket reached READY.
    # If every ticket was SKIPPED (e.g. all had 404s or missing branches),
    # triggering merge-and-deploy with nothing to merge will always fail and
    # produce a misleading "no ticket-specific cause detected" escalation.
    ready_tickets = [r for r in report.read_current_rows() if r.get("sync_status") == "READY"]
    if not ready_tickets:
        print(
            "No READY tickets after Phase 1 — all tickets were skipped or already done. "
            "Phase 2 not triggered."
        )
        return 0

    state = PhaseTwoState(PhaseTwoState.build_state_path(csv_path))
    run_phase2(jenkins_remote, assembla, report, state)

    # Phase 3 (claude CLI browser testing) is not part of this copy.
    return 0


if __name__ == "__main__":
    sys.exit(main())
