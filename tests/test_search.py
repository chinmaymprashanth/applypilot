from pathlib import Path
import dataclasses
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from jobagent.config import load_settings
from jobagent.search import adzuna, hn, simplify
from jobagent.search.ats import fetch_ashby, fetch_greenhouse, fetch_lever
from jobagent.search.common import detect_board, html_to_text, is_us, iso_utc, role_tier, work_mode
from jobagent.search.runner import run_search
from jobagent.tracker import Tracker

NOW = datetime(2026, 11, 11, 12, tzinfo=timezone.utc)
RECENT = (NOW - timedelta(hours=5)).isoformat()
OLD = (NOW - timedelta(days=10)).isoformat()


@pytest.mark.parametrize("title,tier", [
    ("Software Engineer, Backend", 1), ("SDE I", 1), ("Junior Python Developer", 1),
    ("Applied AI Engineer", 1), ("LLM Engineer", 1), ("Data Engineer II", 2),
    ("Technical Support Engineer", 2), ("Account Executive", None), ("Recruiter", None),
])
def test_role_tier(title, tier):
    assert role_tier(title) == tier


@pytest.mark.parametrize("loc,country,us", [
    ("San Francisco, CA", None, True), ("Remote - US", None, True), ("Remote", None, True),
    ("London, UK", None, False), ("Bengaluru, India", None, False), ("Toronto, Canada", None, False),
    ("Anywhere", "US", True), ("Singapore, Singapore", "SG", False),
    ("San Francisco", None, True), ("Dallas, Texas", None, True), ("Hyde Park, Hamilton County", None, True),
    ("NYC Office", None, True), ("Munich", None, False), ("Hyderabad", None, False),
])
def test_is_us(loc, country, us):
    assert is_us(loc, country) is us


def test_detect_board():
    assert detect_board("https://job-boards.greenhouse.io/anthropic/jobs/123") == ("greenhouse", "anthropic")
    assert detect_board("https://jobs.lever.co/Palantir/abc/apply") == ("lever", "palantir")
    assert detect_board("https://jobs.ashbyhq.com/ramp/xyz") == ("ashby", "ramp")
    assert detect_board("https://Acme.wd5.myworkdayjobs.com/en-US/External/job/X_1") == (
        "workday", "acme.wd5.myworkdayjobs.com|acme|external")
    assert detect_board("https://careers.acme.com/jobs/1") is None


def test_helpers():
    assert html_to_text("&lt;p&gt;Hi &amp;amp; bye&lt;/p&gt;") == "Hi & bye"
    assert iso_utc(1786469891368).startswith("2026-")
    assert iso_utc("2026-10-19T11:17:24-04:00") == "2026-10-19T15:17:24+00:00"
    assert work_mode("Hybrid") == "hybrid" and work_mode(None, "Remote - US") == "remote"


def _mock(routes, seen=None):
    def handler(request: httpx.Request):
        if seen is not None:
            seen.append(str(request.url))
        for prefix, payload in sorted(routes.items(), key=lambda kv: -len(kv[0])):  # most specific first
            if str(request.url).startswith(prefix):
                return payload if isinstance(payload, httpx.Response) else httpx.Response(200, json=payload)
        return httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(handler))


GH = {"jobs": [
    {"id": 1, "title": "Software Engineer", "company_name": "Acme", "location": {"name": "Remote - US"},
     "first_published": RECENT, "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/1"},
    {"id": 2, "title": "Software Engineer, Old", "company_name": "Acme", "location": {"name": "NYC, NY"},
     "first_published": OLD, "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/2"},
    {"id": 3, "title": "Recruiter", "company_name": "Acme", "location": {"name": "NYC, NY"},
     "first_published": RECENT, "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/3"},
]}
GH_ROUTES = {
    "https://boards-api.greenhouse.io/v1/boards/acme/jobs": GH,
    "https://boards-api.greenhouse.io/v1/boards/acme/jobs/1": {"content": "&lt;p&gt;Python&lt;/p&gt;"},
    "https://boards-api.greenhouse.io/v1/boards/acme/jobs/2": {"content": ""},
}


def test_fetchers_parse_each_feed():
    lever = [{"text": "Backend Engineer", "country": "US", "createdAt": 1786469891368, "workplaceType": "remote",
              "categories": {"location": "Austin, TX"}, "descriptionPlain": "Go and Python",
              "hostedUrl": "https://jobs.lever.co/beta/1", "applyUrl": "https://jobs.lever.co/beta/1/apply"}]
    ashby = {"jobs": [{"title": "AI Engineer", "location": "Remote", "isRemote": True, "isListed": True,
                       "publishedAt": RECENT, "jobUrl": "https://jobs.ashbyhq.com/gamma/1",
                       "descriptionPlain": "LLMs", "address": {"postalAddress": {"addressCountry": "Canada"}}}]}
    c = _mock({**GH_ROUTES, "https://api.lever.co": lever, "https://api.ashbyhq.com": ashby})
    gh = fetch_greenhouse(c, "acme")
    assert [j["title"] for j in gh] == ["Software Engineer", "Software Engineer, Old"]
    assert gh[0]["description"] == "Python" and gh[0]["work_mode"] == "remote"
    (lv,) = fetch_lever(c, "beta")
    assert lv["apply_url"].endswith("/apply") and lv["work_mode"] == "remote"
    (ab,) = fetch_ashby(c, "gamma")  # remote role kept even though the office is in Canada
    assert ab["ats_type"] == "ashby"


def test_run_search_discovers_board_and_drops_stale(tmp_path, monkeypatch):
    monkeypatch.setenv("ADZUNA_APP_ID", "id")
    monkeypatch.setenv("ADZUNA_APP_KEY", "key")
    monkeypatch.setattr(adzuna, "SEARCHES", [({"category": "it-jobs", "title_only": "engineer"}, 1)])
    settings = dataclasses.replace(load_settings(tmp_path), use_adzuna=True)  # off by default
    tracker = Tracker(tmp_path / "tracker.xlsx", tmp_path / "jobs")
    adz = {"results": [{
        "id": "9", "title": "Software Engineer", "company": {"display_name": "Acme Corporation Inc"},
        "location": {"display_name": "Remote - US"}, "created": RECENT, "description": "snippet",
        "redirect_url": "https://www.adzuna.com/land/ad/9",
    }]}
    # Adzuna's own link is never followed; the board is found from the company name.
    c = _mock({"https://api.adzuna.com": adz, **GH_ROUTES})
    monkeypatch.setattr(adzuna.time, "sleep", lambda s: None)

    report = run_search(settings, tracker, client=c, now=NOW)

    assert report.new_boards == 1 and report.boards_polled == 1
    assert report.stale == 0  # the 10-day-old Greenhouse job is filtered inside the fetcher
    # Adzuna's copy is a duplicate of the board's job, so only the board version (full text) is kept.
    assert report.added == 1 and report.by_source == {"greenhouse": 1}
    (row,) = tracker.jobs.values()
    assert tracker.description(row["id"]) == "Python"


def test_gone_board_is_dropped_at_once(tmp_path):
    tracker = Tracker(tmp_path / "tracker.xlsx", tmp_path / "jobs")
    tracker.add_board("lever", "gone")
    report = run_search(load_settings(tmp_path), tracker, client=_mock({}), now=NOW)
    assert report.boards_gone == 1 and not tracker.active_boards()


def test_greenhouse_skips_old_and_tracked_jobs_without_downloading(tmp_path):
    seen: list[str] = []
    c = _mock(GH_ROUTES, seen)
    jobs = fetch_greenhouse(c, "acme", since=NOW - timedelta(days=7),
                            skip=lambda j: j["url"].endswith("/1"))
    assert [j["title"] for j in jobs] == ["Software Engineer"]  # old one filtered, recruiter not tech
    assert jobs[0]["description"] == ""  # already tracked: no detail request
    assert seen == ["https://boards-api.greenhouse.io/v1/boards/acme/jobs"]


def _listing(**over):
    base = {"source": "Simplify", "category": "Software", "company_name": "Acme", "id": "x", "title": "Software Engineer",
            "active": True, "is_visible": True, "date_posted": int((NOW - timedelta(days=1)).timestamp()),
            "url": "https://job-boards.greenhouse.io/acme/jobs/1?utm_source=Simplify", "locations": ["NYC, NY"]}
    return {**base, **over}


def test_newgrad_list():
    listings = [
        _listing(),
        _listing(title="Hardware Engineer", category="Hardware"),
        _listing(title="Software Engineer, Old", date_posted=int((NOW - timedelta(days=30)).timestamp()),
                 url="https://jobs.ashbyhq.com/beta/1", company_name="Beta"),
        _listing(title="SWE", locations=["Toronto, Canada"], url="https://jobs.lever.co/gamma/1"),
        _listing(title="Software Engineer", active=False, url="https://acme.wd5.myworkdayjobs.com/x"),
        _listing(title="Data Analyst", locations=["London, UK", "Austin, TX"], url="https://acme.wd5.myworkdayjobs.com/y"),
    ]
    recent = simplify.recent_jobs(listings, NOW - timedelta(days=7))
    assert [(j["title"], j["location"], j["ats_type"]) for j in recent] == [
        ("Software Engineer", "NYC, NY", "greenhouse"), ("Data Analyst", "Austin, TX", "workday")]
    # Every board in the history is worth polling, even for closed or old roles; Workday sites included.
    assert simplify.boards(listings) == {("greenhouse", "acme"): "Acme", ("ashby", "beta"): "Beta",
                                         ("lever", "gamma"): "Acme",
                                         ("workday", "acme.wd5.myworkdayjobs.com|acme|x"): "Acme",
                                         ("workday", "acme.wd5.myworkdayjobs.com|acme|y"): "Acme"}


def test_hn_boards():
    search = {"hits": [{"objectID": "2", "title": "Ask HN: Who wants to be hired? (November 2026)"},
                       {"objectID": "1", "title": "Ask HN: Who is hiring? (November 2026)"}]}
    item = {"children": [
        {"text": "Zeta AI | Software Engineer | NYC<p>Apply: <a href=\"https:&#x2F;&#x2F;jobs.ashbyhq.com&#x2F;zeta\">"
                 "https:&#x2F;&#x2F;jobs.ashbyhq.com&#x2F;zeta</a>"},
        {"text": "Omega | Remote | email jobs@omega.dev"},
    ]}
    c = _mock({"https://hn.algolia.com/api/v1/search_by_date": search, "https://hn.algolia.com/api/v1/items/1": item})
    assert hn.boards(c) == {("ashby", "zeta"): "Zeta AI"}


def test_board_job_wins_over_newgrad_listing(tmp_path, monkeypatch):
    raw = [_listing(url="https://job-boards.greenhouse.io/acme/jobs/1"),
           _listing(title="Data Analyst", company_name="Big", url="https://big.wd5.myworkdayjobs.com/y")]
    c = _mock({simplify.LISTINGS: raw, **GH_ROUTES})
    tracker = Tracker(tmp_path / "tracker.xlsx", tmp_path / "jobs")
    report = run_search(load_settings(tmp_path), tracker, client=c, now=NOW)
    assert report.new_boards == 2 and report.boards_polled == 1  # the Workday site isn't served by the mock
    assert report.by_source == {"greenhouse": 1, "newgrad_list": 1}
    gh = next(r for r in tracker.jobs.values() if r["source"] == "greenhouse")
    assert tracker.description(gh["id"]) == "Python"


def test_adzuna_keeps_county_style_us_locations(monkeypatch):
    monkeypatch.setattr(adzuna, "SEARCHES", [({"category": "it-jobs"}, 1)])
    monkeypatch.setattr(adzuna.time, "sleep", lambda s: None)
    res = {"results": [{"id": "1", "title": "Software Engineer", "company": {"display_name": "Eliassen Group"},
                        "location": {"display_name": "Hyde Park, Hamilton County"}, "created": RECENT,
                        "redirect_url": "https://www.adzuna.com/land/ad/1"}]}
    (job,) = adzuna.search(_mock({"https://api.adzuna.com": res}), "id", "key")
    assert job["location"] == "Hyde Park, Hamilton County"


def test_newgrad_list_falls_back_to_saved_copy(tmp_path):
    cache = tmp_path / "newgrad.json"
    data, fresh = simplify.fetch(_mock({simplify.LISTINGS: [_listing()]}), cache)
    assert fresh and len(data) == 1 and cache.exists()
    data, fresh = simplify.fetch(_mock({}), cache)  # GitHub fails: yesterday's copy is used
    assert not fresh and data[0]["company_name"] == "Acme"


def test_adzuna_is_off_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("ADZUNA_APP_ID", "id")
    monkeypatch.setenv("ADZUNA_APP_KEY", "key")
    seen = []
    run_search(load_settings(tmp_path), Tracker(tmp_path / "t.xlsx", tmp_path / "jobs"), client=_mock({}, seen), now=NOW)
    assert not any("adzuna" in u for u in seen)


WD_LIST = {"total": 3, "jobPostings": [
    {"title": "Software Engineer", "postedOn": "Posted Today", "locationsText": "2 Locations",
     "externalPath": "/job/Austin/Software-Engineer_R1"},
    {"title": "Data Analyst", "postedOn": "Posted Yesterday", "locationsText": "Bengaluru, India",
     "externalPath": "/job/Bengaluru/Data-Analyst_R2"},
    {"title": "Data Engineer", "postedOn": "Posted 30+ Days Ago", "locationsText": "Austin, TX",
     "externalPath": "/job/Austin/Data-Engineer_R3"}]}
WD_DETAIL = {"jobPostingInfo": {"location": "Austin, TX", "country": {"descriptor": "United States of America"},
                                "startDate": "2026-11-11", "jobDescription": "&lt;p&gt;Build APIs&lt;/p&gt;"}}


def test_workday_reads_newest_jobs_until_too_old():
    from jobagent.search.ats import fetch_workday, posted_days
    assert (posted_days("Posted Today"), posted_days("Posted Yesterday"), posted_days("Posted 30+ Days Ago")) == (0, 1, 30)
    c = _mock({"https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/careers/jobs": WD_LIST,
               "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/careers/job/Austin/Software": WD_DETAIL})
    jobs = fetch_workday(c, "acme.wd5.myworkdayjobs.com|acme|careers", since=NOW - timedelta(days=7))
    # India skipped from the list; the 30+ day job ends the scan; the US job gets its full description.
    assert [(j["title"], j["location"], j["description"]) for j in jobs] == [("Software Engineer", "Austin, TX", "Build APIs")]
    assert jobs[0]["apply_url"] == "https://acme.wd5.myworkdayjobs.com/careers/job/Austin/Software-Engineer_R1"


@pytest.mark.always_boards
def test_big_employers_are_checked_every_day(tmp_path):
    tracker = Tracker(tmp_path / "t.xlsx", tmp_path / "jobs")
    run_search(load_settings(tmp_path), tracker, client=_mock({}), now=NOW)
    assert ("amazon", "amazon") in tracker.boards and ("eightfold", "explore.jobs.netflix.net|netflix.com") in tracker.boards


def test_new_board_types_are_recognized_with_their_case():
    assert detect_board("https://jobs.smartrecruiters.com/Wise/744000161543629-x") == ("smartrecruiters", "Wise")
    assert detect_board("https://jobs.smartrecruiters.com/oneclick-ui/company/WellmarkInc/publication/x") == (
        "smartrecruiters", "WellmarkInc")
    assert detect_board("https://apply.workable.com/bask-health-1/j/472CF47F60/") == ("workable", "bask-health-1")
    assert detect_board("https://abc.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/15") == (
        "oracle", "abc.fa.ocs.oraclecloud.com|CX_1")
    t = Tracker(Path("/nonexistent/t.xlsx"), Path("/nonexistent/jobs"), db_path=Path("/nonexistent/x.db"))
    t.add_board("oracle", "abc.fa.ocs.oraclecloud.com|CX_1")
    assert ("oracle", "abc.fa.ocs.oraclecloud.com|CX_1") in t.boards  # not lowercased


def test_workday_422_is_retried_not_dropped(tmp_path):
    tracker = Tracker(tmp_path / "t.xlsx", tmp_path / "jobs")
    tracker.add_board("workday", "acme.wd5.myworkdayjobs.com|acme|careers")
    c = _mock({"https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/careers/jobs": httpx.Response(422, json={})})
    report = run_search(load_settings(tmp_path), tracker, client=c, now=NOW)
    board = tracker.boards[("workday", "acme.wd5.myworkdayjobs.com|acme|careers")]
    assert report.boards_gone == 0 and board["failures"] == 1  # tried again tomorrow; dropped after 3 failures


def _two_boards(tmp_path):
    t = Tracker(tmp_path / "t.xlsx", tmp_path / "jobs")
    t.add_board("lever", "blip")
    t.add_board("lever", "private")
    return t


def test_network_blips_dont_count_but_refusals_do(tmp_path):
    t = _two_boards(tmp_path)

    def handler(request):
        if "blip" in str(request.url):
            raise httpx.ConnectError("nodename nor servname provided")
        if "private" in str(request.url):
            return httpx.Response(403)
        return httpx.Response(404)

    report = run_search(load_settings(tmp_path), t, client=httpx.Client(transport=httpx.MockTransport(handler)), now=NOW)
    assert t.boards[("lever", "blip")]["failures"] == 0
    assert t.boards[("lever", "private")]["failures"] == 1
    assert report.hiccups == {"network": 1, "access refused": 1}


def test_slow_down_is_waited_out_and_retried(tmp_path, monkeypatch):
    monkeypatch.setattr("jobagent.search.runner.time.sleep", lambda s: None)
    t = Tracker(tmp_path / "t.xlsx", tmp_path / "jobs")
    t.add_board("lever", "busy")
    calls = []

    def handler(request):
        if "api.lever.co" not in str(request.url):
            return httpx.Response(404)
        calls.append(1)
        return httpx.Response(429, headers={"retry-after": "1"}) if len(calls) == 1 else httpx.Response(200, json=[])

    report = run_search(load_settings(tmp_path), t, client=httpx.Client(transport=httpx.MockTransport(handler)), now=NOW)
    assert len(calls) == 2 and report.boards_polled == 1 and not report.hiccups


def test_workday_slow_down_is_retried_per_request(monkeypatch):
    """A 429 on a list page or a job page waits and retries that request; the site isn't abandoned."""
    from jobagent.search import ats

    monkeypatch.setattr(ats.time, "sleep", lambda s: None)
    answers = {"https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/careers/jobs": WD_LIST,
               "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/careers/job/Austin/Software": WD_DETAIL}
    seen: dict[str, int] = {}

    def handler(request):
        url = str(request.url).split("?")[0]
        seen[url] = seen.get(url, 0) + 1
        if seen[url] == 1:  # every URL says "slow down" once
            return httpx.Response(429, headers={"retry-after": "1"}, text="Too Many Requests")
        for prefix, body in answers.items():
            if url.startswith(prefix):
                return httpx.Response(200, json=body)
        return httpx.Response(404)

    c = httpx.Client(transport=httpx.MockTransport(handler))
    jobs = ats.fetch_workday(c, "acme.wd5.myworkdayjobs.com|acme|careers", since=NOW - timedelta(days=7))
    assert [j["title"] for j in jobs] == ["Software Engineer"] and jobs[0]["description"] == "Build APIs"


def test_workday_detail_that_stays_throttled_is_a_slow_down_not_bad_data(monkeypatch):
    from jobagent.search import ats
    from jobagent.search.runner import error_kind

    monkeypatch.setattr(ats.time, "sleep", lambda s: None)

    def handler(request):
        if str(request.url).endswith("/jobs"):
            return httpx.Response(200, json=WD_LIST)
        return httpx.Response(429, text="Too Many Requests")

    c = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(httpx.HTTPStatusError) as e:
        ats.fetch_workday(c, "acme.wd5.myworkdayjobs.com|acme|careers", since=NOW - timedelta(days=7))
    assert error_kind(e.value) == "slow down"  # doesn't count against the site


def test_a_throttling_workday_server_is_left_for_the_next_run(tmp_path, monkeypatch):
    """After a few "slow down"s in a row from one Workday server, its other sites wait for tomorrow (no failure
    counted), while other servers carry on."""
    from jobagent.search import ats, runner

    monkeypatch.setattr(ats.time, "sleep", lambda s: None)
    tracker = Tracker(tmp_path / "t.xlsx", tmp_path / "jobs")
    for i in range(12):
        tracker.add_board("workday", f"co{i}.wd5.myworkdayjobs.com|co{i}|careers")
    tracker.add_board("workday", "ok.wd1.myworkdayjobs.com|ok|careers")
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if ".wd5." in str(request.url):
            return httpx.Response(429, text="Too Many Requests")
        if ".wd1." in str(request.url):
            return httpx.Response(200, json={"total": 0, "jobPostings": []})
        return httpx.Response(404)  # the job lists and other sources aren't part of this test

    c = httpx.Client(transport=httpx.MockTransport(handler))
    report = run_search(load_settings(tmp_path), tracker, client=c, now=NOW, workers=1)
    assert report.hiccups.get("left for the next run", 0) >= 12 - runner.THROTTLE_STRIKES  # the breaker tripped
    assert sum(".wd5." in u for u in calls) <= runner.THROTTLE_STRIKES * ats.WORKDAY_TRIES  # stopped asking
    assert all(b["failures"] == 0 for b in tracker.boards.values())  # never counted against a site
    assert tracker.boards[("workday", "ok.wd1.myworkdayjobs.com|ok|careers")].get("last_polled")


def test_search_stops_starting_sites_when_its_time_budget_is_used(tmp_path):
    tracker = Tracker(tmp_path / "t.xlsx", tmp_path / "jobs")
    tracker.add_board("workday", "acme.wd5.myworkdayjobs.com|acme|careers")
    c = _mock({})
    report = run_search(load_settings(tmp_path), tracker, client=c, now=NOW, budget_minutes=-1)  # already over
    assert report.hiccups == {"left for the next run": 1} and report.boards_polled == 0
    assert tracker.boards[("workday", "acme.wd5.myworkdayjobs.com|acme|careers")]["failures"] == 0
