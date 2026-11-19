"""The search stage: gather jobs from every source, drop stale ones, add new ones to the tracker.

Sources, most important first:
  1. Company job boards (Greenhouse/Lever/Ashby), polled daily. The list of boards grows from
     the new-grad list's history, Hacker News "Who is hiring", and every source below.
  2. The new-grad list: entry-level roles posted in the last few days, with company apply links.
  3. Job alert emails (LinkedIn, Indeed, Wellfound, Built In, YC), traced to company boards.
  4. Adzuna keyword search, if keys are set.
A job found on a company board wins over the same job from any other source: it has the full
description and the company's own apply link.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx
from googleapiclient.errors import HttpError

from ..config import Settings
from ..tracker import Tracker, dedupe_key, job_id, now_iso
from ..llm import LLMError, make_client, parse
from . import adzuna, gmail, hn, simplify
from .ats import ALWAYS, FETCHERS, BoardNotFound
from .common import age_hours, detect_board
from .discover import BoardFinder

log = logging.getLogger(__name__)

USER_AGENT = "jobagent/0.1 (personal job search)"
# A 404 means the company moved off that board; transient errors get this many retries (days).
MAX_FAILURES = 3


# How many sites of a throttling service are checked at the same time.
# Workday: most companies share a few servers (wd1, wd3, wd5), which throttle cloud addresses hard.
PER_SERVICE = {"workable": 2, "smartrecruiters": 4, "eightfold": 1, "amazon": 1}
# Services checked one site at a time in their own lane, beside everything else (paced in ats.py).
SLOW_LANE = ("workday",)
TEMPORARY = ("network", "timeout", "slow down", "server error", "left for the next run")
# The board-polling step stops starting new sites after this long; the rest go first next time (the oldest
# checked are always polled first), so every site is still covered over a few days.
SEARCH_BUDGET_MINUTES = 45
# After this many "slow down" answers in a row from one server, its remaining sites wait for the next run.
# (Workday serves most companies from a few shared servers, wd1/wd3/wd5…, which throttle cloud addresses hard.)
THROTTLE_STRIKES = 5


class Deferred(Exception):
    """A site not checked in this run (time budget used up, or its server is throttling); it goes first next time."""


def server_of(board: dict) -> str:
    """The server a site lives on, for throttling: a Workday shard like wd5.myworkdayjobs.com, else the service."""
    if board["ats_type"] == "workday":
        host = board["token"].split("|")[0]
        return host.split(".", 1)[1] if "." in host else host
    return board["ats_type"]


def error_kind(e: Exception) -> str:
    """A short, plain label for why a site couldn't be checked."""
    if isinstance(e, Deferred):
        return "left for the next run"
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
        if code == 429:
            return "slow down"
        if code >= 500:
            return "server error"
        if code in (401, 403):
            return "access refused"
        return f"HTTP {code}"
    if isinstance(e, httpx.TimeoutException):
        return "timeout"
    if isinstance(e, (httpx.ConnectError, httpx.NetworkError, OSError)):
        return "network"
    if isinstance(e, ValueError):
        return "unexpected reply"
    return "other"


@dataclass
class SearchReport:
    by_source: dict[str, int] = field(default_factory=dict)
    emails_read: int = 0
    newgrad_listings: int = 0
    intern_listings: int = 0
    new_boards: int = 0
    boards_polled: int = 0
    boards_gone: int = 0
    stale: int = 0
    added: int = 0
    hiccups: dict[str, int] = field(default_factory=dict)  # sites that couldn't be checked, by reason
    # The Workday slow lane, for the summary: sites checked, new jobs, sites throttled, sites left for next run.
    slow_lane: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


def _fresh(job: dict, max_age_days: int, now: datetime) -> bool:
    hours = age_hours(job.get("posted_at"), now)
    return hours is None or hours <= max_age_days * 24


def _gmail_listings(settings: Settings, report: SearchReport, extract) -> list[dict]:
    """Jobs from alert emails not read before. Skipped quietly if Gmail isn't set up."""
    token = settings.data_dir / "gmail_token.json"
    if not token.exists():
        report.errors.append("gmail: not signed in, skipped (run: python -m jobagent gmail-login)")
        return []
    seen_path = settings.data_dir / "gmail_seen.json"
    seen = gmail.load_seen(seen_path)
    try:
        if extract is None:
            client = make_client(settings.anthropic_api_key)

            def extract(system: str, text: str):
                return parse(client, model=settings.model, schema=gmail.AlertJobs, system=system,
                             content=text, max_tokens=8000, effort="low", step="alert emails")

        emails = gmail.fetch_alerts(gmail.service(token), settings.max_age_days, seen)
    except (gmail.GmailAuthError, LLMError) as e:
        report.errors.append(f"gmail: {e}")
        return []
    except HttpError as e:
        report.errors.append(f"gmail: {gmail.explain_http_error(e)}")
        return []
    jobs: list[dict] = []
    for email in emails:
        try:
            jobs.extend(gmail.jobs_from_email(email, extract))
            seen.add(email.id)
        except Exception as e:  # noqa: BLE001 - one bad email shouldn't stop the run
            report.errors.append(f"gmail: email '{email.subject[:60]}': {e}")
    gmail.save_seen(seen_path, seen)
    report.emails_read = len(emails)
    return jobs


def _add_boards(tracker: Tracker, report: SearchReport, boards: dict[tuple[str, str], str]) -> None:
    for (ats, token), company in boards.items():
        if tracker.add_board(ats, token, company=company):
            report.new_boards += 1


def run_search(
    settings: Settings, tracker: Tracker, *, client: httpx.Client | None = None,
    now: datetime | None = None, trace_limit: int = 150, extract=None,
    workers: int = 12, budget_minutes: float | None = SEARCH_BUDGET_MINUTES,
) -> SearchReport:
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=settings.max_age_days)
    report = SearchReport()
    own_client = client is None
    client = client or httpx.Client(timeout=30, headers={"User-Agent": USER_AGENT}, follow_redirects=True)
    found: list[dict] = []
    try:
        listings: list[dict] = []

        # 1. The new-grad list: recent roles, plus every board in its history.
        try:
            raw, fresh_copy = simplify.fetch(client, settings.data_dir / "newgrad_listings.json")
            if not fresh_copy:
                report.errors.append("new-grad list: GitHub was slow, so the last saved copy was used")
            recent = simplify.recent_jobs(raw, since)
            report.newgrad_listings = len(recent)
            listings.extend(recent)
            _add_boards(tracker, report, simplify.boards(raw))
        except (httpx.HTTPError, ValueError, KeyError) as e:
            report.errors.append(f"new-grad list: {e}")

        # 1b. The internship list (Spring/Summer/Fall 2027), for the internship & part-time shortlist.
        try:
            raw, fresh_copy = simplify.fetch(client, settings.data_dir / "intern_listings.json", simplify.INTERNSHIPS)
            recent = simplify.recent_jobs(raw, since, source="intern_list", kind="intern_pt")
            report.intern_listings = len(recent)
            listings.extend(recent)
            _add_boards(tracker, report, simplify.boards(raw))
        except (httpx.HTTPError, ValueError, KeyError) as e:
            report.errors.append(f"internship list: {e}")

        # 1c. Big employers with their own career sites, checked every day.
        _add_boards(tracker, report, {(ats, token): company for ats, token, company in ALWAYS})
        for ats, token, _ in ALWAYS:  # a bad day (e.g. "too many requests") never drops these for good
            tracker.boards[(ats, token)]["failures"] = 0

        # 2. Boards linked from this month's Hacker News "Who is hiring?".
        try:
            _add_boards(tracker, report, hn.boards(client))
        except (httpx.HTTPError, ValueError, KeyError) as e:
            report.errors.append(f"hacker news: {e}")

        # 3. Keyword search across all employers (optional).
        if settings.use_adzuna and settings.adzuna_app_id and settings.adzuna_app_key:
            try:
                listings.extend(adzuna.search(client, settings.adzuna_app_id, settings.adzuna_app_key))
            except httpx.HTTPError as e:
                report.errors.append(f"adzuna: {e}")

        # 4. Job alert emails.
        listings.extend(_gmail_listings(settings, report, extract))

        # 5. Trace each new alert/Adzuna listing to the employer's own board by company name.
        #    New-grad list links already point at the company's site.
        finder = BoardFinder(client, settings.data_dir / "board_guesses.json", since=since)
        known = tracker.dedupe_keys()
        to_trace = [
            j for j in listings
            if j["source"] not in ("newgrad_list", "intern_list")
            and _fresh(j, settings.max_age_days, now)
            and dedupe_key(j["company"], j["title"]) not in known
        ]
        for job in to_trace[:trace_limit]:
            board = detect_board(job["apply_url"])
            if not board:
                hit = finder.find(job["company"], job["title"])
                if hit:
                    ats, token, match = hit
                    board = (ats, token)
                    job.update(apply_url=match["apply_url"], ats_type=ats)
            if board:
                _add_boards(tracker, report, {board: job["company"]})
        finder.save()

        # 6. Poll every board, several at a time. Descriptions are downloaded only for recent
        #    US tech jobs not already in the tracker.
        def skip(job: dict) -> bool:
            return job_id(job["url"]) in tracker.jobs

        # Services that throttle: at most this many of their sites are checked at once.
        gates = {ats: threading.Semaphore(n) for ats, n in PER_SERVICE.items()}

        # A time budget for the whole polling step, and a circuit breaker per server.
        deadline = time.monotonic() + budget_minutes * 60 if budget_minutes else None
        strikes: dict[str, int] = {}
        strikes_lock = threading.Lock()

        def strike(server: str, throttled: bool) -> None:
            with strikes_lock:
                strikes[server] = strikes.get(server, 0) + 1 if throttled else 0

        def poll(b: dict):
            key = (b["ats_type"], b["token"])
            server = server_of(b)
            if deadline and time.monotonic() > deadline:
                return b, None, Deferred("search time budget used up")
            with strikes_lock:
                if strikes.get(server, 0) >= THROTTLE_STRIKES:
                    return b, None, Deferred(f"{server} keeps asking to slow down")
            gate = gates.get(b["ats_type"])
            # Workday retries each request itself; retrying the whole site on top made one slow site take minutes.
            tries = 1 if b["ats_type"] == "workday" else 3
            for attempt in range(tries):
                try:
                    jobs = finder.boards.get(key)  # already fetched earlier in this run
                    if jobs is None:
                        if gate:
                            with gate:
                                jobs = FETCHERS[b["ats_type"]](client, b["token"], since=since, skip=skip)
                        else:
                            jobs = FETCHERS[b["ats_type"]](client, b["token"], since=since, skip=skip)
                    strike(server, throttled=False)
                    return b, jobs, None
                except httpx.HTTPStatusError as e:
                    if e.response.status_code == 429 and attempt == tries - 1:
                        strike(server, throttled=True)
                    if e.response.status_code != 429 or attempt == tries - 1:
                        return b, None, e
                    # "Slow down": wait as long as the service asks (or a few seconds), then try again.
                    time.sleep(min(float(e.response.headers.get("retry-after") or 5 * (attempt + 1)), 30))
                except Exception as e:  # noqa: BLE001 - one bad board shouldn't stop the run
                    return b, None, e
            return b, None, RuntimeError("retries exhausted")

        polled: set[tuple[str, str]] = set()
        boards = tracker.active_boards(MAX_FAILURES)  # least recently checked first
        slow = [b for b in boards if b["ats_type"] in SLOW_LANE]
        fast = [b for b in boards if b["ats_type"] not in SLOW_LANE]
        lane = {"checked": 0, "jobs": 0, "throttled": 0, "left for next run": 0} if slow else {}
        with ThreadPoolExecutor(max_workers=workers) as pool, ThreadPoolExecutor(max_workers=1) as slow_pool:
            # The slow lane walks its sites one at a time, alongside the other services.
            slow_results = slow_pool.submit(lambda: [poll(b) for b in slow])
            results = list(pool.map(poll, fast)) + slow_results.result()
            for b, jobs, err in results:
                if b["ats_type"] in SLOW_LANE:
                    if err is None:
                        lane["checked"] += 1
                        lane["jobs"] += len(jobs)
                    elif isinstance(err, Deferred):
                        lane["left for next run"] += 1
                    elif error_kind(err) == "slow down":
                        lane["throttled"] += 1
                if isinstance(err, BoardNotFound):
                    b["failures"] = MAX_FAILURES
                    report.boards_gone += 1
                    log.info("%s/%s: board gone, dropping it", b["ats_type"], b["token"])
                    continue
                if err:
                    kind = error_kind(err)
                    report.hiccups[kind] = report.hiccups.get(kind, 0) + 1
                    if not isinstance(err, Deferred):  # deferred sites are counted, not logged one by one
                        log.warning("%s/%s: %s", b["ats_type"], b["token"], err)
                    if kind not in TEMPORARY:  # network blips and "slow down" don't count against the site
                        b["failures"] += 1
                    continue
                b.update(last_polled=now_iso(), failures=0)
                if b["ats_type"] != "greenhouse":  # Lever/Ashby feeds only give the board token
                    for j in jobs:
                        j["company"] = b.get("company") or b["token"].replace("-", " ").title()
                found.extend(jobs)
                polled.add((b["ats_type"], b["token"]))
                report.boards_polled += 1
        report.slow_lane = lane
        found.extend(j for j in listings if detect_board(j["apply_url"]) not in polled)
    finally:
        if own_client:
            client.close()

    # 7. Keep only jobs posted within the window, then add (the tracker skips duplicates).
    for job in found:
        if not _fresh(job, settings.max_age_days, now):
            report.stale += 1
            continue
        if tracker.add_job(job):
            report.added += 1
            report.by_source[job["source"]] = report.by_source.get(job["source"], 0) + 1
    for e in report.errors:
        log.warning(e)
    return report
