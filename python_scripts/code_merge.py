"""Staging deploy through the DevOps "Code_Merge" Jenkins job (profile: staging_deploy.adapter = "code_merge").

How the job works (read from its builds, 2026-11-10): it has no parameters and runs every 2 hours on a timer (it can
also be started by hand). Each run takes ONE ticket in "DevOps Staging" from the whole Assembla space (any Node, in an
order this workflow cannot see), opens and merges an Assembla merge request FB_<n> -> master in that Node's repo, then
builds the applications the merge touched and ships them to staging (Java_Transfer). IT Support then moves the
ticket to QA Queue (all built), or Merge Failed / Build Failed (the merge is reset).

This module never copies the console into a file or a RESULT: the job prints environment files that contain secrets.
Only the lines it needs are parsed (which ticket, which apps, which builds failed).
"""
import ast
import re
import time
import urllib.parse

TICKET_RE = re.compile(r"Creating merge request for ticket #\s*(\d+)")
NOTHING_MARK = "Nothing to create merge request"
APPS_RE = re.compile(r"=== Get a list of what to build\. ===\s*\n(\[[^\n]*\])")
BUILD_RE = re.compile(r"^Build for (\S+) is (successful|failed)\.", re.M)
RESET_RE = re.compile(r"Build failed for (\d+)\. Will reset")
MERGE_FAILED_RE = re.compile(r"Merge Request failed for ID")
FINISHED_RE = re.compile(r"^Finished: (\w+)", re.M)

POLL_SECONDS = 15
QUEUE_TIMEOUT_SECONDS = 180
BUILD_TIMEOUT_SECONDS = 90 * 60


def parse_console(text: str) -> dict:
    """The facts of one Code_Merge run, from its console text. Never returns console lines."""
    apps = APPS_RE.search(text)
    try:
        app_list = ast.literal_eval(apps.group(1)) if apps else None
    except (ValueError, SyntaxError):
        app_list = None
    builds = {}
    for app, outcome in BUILD_RE.findall(text):
        builds[app] = outcome  # a later line for the same app (a retry) wins
    finished = FINISHED_RE.findall(text)
    return {
        "tickets": list(dict.fromkeys(TICKET_RE.findall(text))),
        "nothing_to_merge": NOTHING_MARK in text,
        "apps": app_list,
        "builds": builds,
        "failed_apps": sorted(a for a, o in builds.items() if o == "failed"),
        "reset_for": list(dict.fromkeys(RESET_RE.findall(text))),
        "merge_failed": bool(MERGE_FAILED_RE.search(text)),
        "finished": finished[-1] if finished else None,
    }


def space_queue(api, staging_status: str) -> list:
    """Every ticket in the staging status in the whole space, lowest number first: Code_Merge chooses among all of them."""
    tickets = [t for t in api.get_active_tickets() if t.get("status") == staging_status]
    return sorted(tickets, key=lambda t: int(t.get("number") or 0))


def queue_token(numbers: list) -> str:
    """A short token naming exactly these other tickets, so an acceptance can't silently cover a changed queue."""
    import hashlib
    return hashlib.sha256(",".join(sorted(numbers)).encode()).hexdigest()[:10]


def queue_position(api, staging_status: str, number: str) -> dict:
    """ALONE: ours is the only ticket the job can take. SHARED: others too (`others`, `token`). NOT_IN_QUEUE."""
    queue = space_queue(api, staging_status)
    numbers = [str(t.get("number")) for t in queue]
    if number not in numbers:
        return {"status": "NOT_IN_QUEUE", "queue": numbers}
    others = [n for n in numbers if n != number]
    if not others:
        return {"status": "ALONE", "queue": numbers}
    return {"status": "SHARED", "queue": numbers, "others": others, "token": queue_token(others)}


class CodeMergeJob:
    """Trigger and follow one Code_Merge build. `client` is deploy_staging_local.JenkinsClient (auth + crumb)."""

    def __init__(self, client, job: str, sleep=time.sleep, clock=time.time):
        self.client = client
        self.job = job
        self.path = "/job/" + urllib.parse.quote(job, safe="")
        self.sleep, self.clock = sleep, clock

    def exists(self) -> bool:
        return self.client.job_exists(self.job)

    def busy(self):
        """None when idle, else what is going on (a build running or one waiting in the queue)."""
        data = self.client._get_json(self.path + "/api/json?tree=inQueue,lastBuild[number,building]") or {}
        last = data.get("lastBuild") or {}
        if last.get("building"):
            return {"running_build": last.get("number")}
        if data.get("inQueue"):
            return {"queued": True}
        return None

    def next_number(self) -> int:
        data = self.client._get_json(self.path + "/api/json?tree=nextBuildNumber") or {}
        return int(data.get("nextBuildNumber") or 0)

    def trigger(self) -> int:
        """Start a build; -> its number (the first build numbered at or after nextBuildNumber before the trigger)."""
        first = self.next_number()
        crumb = self.client._fetch_crumb()
        headers = {crumb[0]: crumb[1]} if crumb else {}
        status, _ = self.client._request(self.path + "/build?delay=0sec", method="POST", extra_headers=headers, data=b"")
        if status not in (200, 201):
            raise RuntimeError(f"Triggering {self.job} failed with HTTP {status}.")
        deadline = self.clock() + QUEUE_TIMEOUT_SECONDS
        while self.clock() < deadline:
            data = self.client._get_json(self.path + "/api/json?tree=builds[number]{0,5}") or {}
            started = sorted(b["number"] for b in data.get("builds") or [] if b.get("number", 0) >= first)
            if started:
                return started[0]
            self.sleep(2)
        raise TimeoutError(f"{self.job} was triggered but no build started within {QUEUE_TIMEOUT_SECONDS}s.")

    def wait(self, number: int) -> dict:
        """Poll until the build finishes; -> parse_console() of it plus "build" (TIMEOUT when it never finished)."""
        deadline = self.clock() + BUILD_TIMEOUT_SECONDS
        while True:
            info = self.client._get_json(f"{self.path}/{number}/api/json?tree=building,result") or {}
            if not info.get("building") and info.get("result"):
                break
            if self.clock() >= deadline:
                break
            self.sleep(POLL_SECONDS)
        _, text = self.client._request(f"{self.path}/{number}/consoleText")
        facts = parse_console(text)
        facts["build"] = number
        facts["result"] = info.get("result") or "TIMEOUT"
        return facts
