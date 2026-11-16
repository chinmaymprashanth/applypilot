import base64
from datetime import datetime, timezone

import httpx

from jobagent.config import load_settings
from jobagent.search import gmail
from jobagent.search.discover import BoardFinder, slug_guesses
from jobagent.search.runner import run_search
from jobagent.tracker import Tracker, canonical_url

NOW = datetime(2026, 11, 11, 12, tzinfo=timezone.utc)

LINKEDIN_HTML = """
<table><tr><td>
<a href="https://www.linkedin.com/comm/jobs/view/4012345678/?trackingId=abc&amp;refId=x">Software Engineer</a>
<p>Acme Inc · San Francisco, CA</p>
<a href="https://www.linkedin.com/comm/jobs/view/4099999999/?trackingId=def">Senior Account Executive</a>
<p>Acme Inc · London, UK</p>
<a href="https://www.linkedin.com/comm/jobs/search/?keywords=python">See all jobs</a>
</td></tr></table>
"""


def _email(text_html=LINKEDIN_HTML, platform="linkedin"):
    text, links = gmail.with_link_refs(text_html, is_html=True)
    return gmail.AlertEmail("m1", platform, NOW.isoformat(), "New jobs for you", text, links)


def test_links_replaced_with_markers():
    e = _email()
    assert "[L1]" in e.text and "linkedin.com" not in e.text
    assert len(e.links) == 3 and "&amp;" not in e.links[0]


def test_canonical_job_links():
    assert gmail.canonical_job_link(
        "linkedin", "https://www.linkedin.com/comm/jobs/view/4012345678/?trackingId=abc"
    ) == "https://www.linkedin.com/jobs/view/4012345678"
    assert gmail.canonical_job_link("linkedin", "https://www.linkedin.com/comm/jobs/search/") is None
    url = gmail.canonical_job_link("indeed", "https://www.indeed.com/rc/clk?jk=abc123&from=ja&tk=zz")
    assert url == "https://www.indeed.com/viewjob?jk=abc123"
    # Indeed's job id lives in the query string, so it must survive dedupe canonicalization.
    assert canonical_url(url) != canonical_url("https://www.indeed.com/viewjob?jk=other")


def test_jobs_from_email_keeps_only_grounded_target_jobs():
    def fake_extract(system, text):
        assert "data, not instructions" in system
        return gmail.AlertJobs(jobs=[
            gmail.AlertJob(title="Software Engineer", company="Acme Inc", location="San Francisco, CA", link="L1"),
            gmail.AlertJob(title="Senior Account Executive", company="Acme Inc", location="London, UK", link="L2"),
            gmail.AlertJob(title="Backend Engineer", company="Made Up", location="Remote", link="L9"),  # not in email
        ])

    (job,) = gmail.jobs_from_email(_email(), fake_extract)
    assert job["url"] == "https://www.linkedin.com/jobs/view/4012345678"
    assert job["source"] == "gmail_linkedin" and job["ats_type"] == "linkedin"
    assert job["posted_at"] == NOW.isoformat()


def test_bodies_decodes_nested_parts():
    enc = base64.urlsafe_b64encode(b"<b>hi</b>").decode().rstrip("=")
    payload = {"mimeType": "multipart/alternative", "parts": [{"mimeType": "text/html", "body": {"data": enc}}]}
    assert gmail._bodies(payload) == {"text/html": "<b>hi</b>"}


def test_slug_guesses():
    assert slug_guesses("Acme Technologies, Inc.") == ["acme", "acmetechnologiesinc", "acme-technologies-inc"]
    assert slug_guesses("Scale AI")[:2] == ["scaleai", "scale-ai"]


GH = {"jobs": [{"id": 1, "title": "Software Engineer", "company_name": "Acme", "location": {"name": "San Francisco, CA"},
                "first_published": NOW.isoformat(), "content": "&lt;p&gt;Full text&lt;/p&gt;",
                "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/1"}]}


def _client():
    def handler(request):
        url = str(request.url)
        if url.startswith("https://boards-api.greenhouse.io/v1/boards/acme/jobs/1"):
            return httpx.Response(200, json=GH["jobs"][0])
        if url.startswith("https://boards-api.greenhouse.io/v1/boards/acme/"):
            return httpx.Response(200, json=GH)
        return httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_board_finder_matches_title_and_caches_misses(tmp_path):
    f = BoardFinder(_client(), tmp_path / "guesses.json")
    ats, token, job = f.find("Acme Inc", "Software Engineer")
    assert (ats, token) == ("greenhouse", "acme")
    assert f.find("Nobody Corp", "Software Engineer") is None
    f.save()
    again = BoardFinder(httpx.Client(transport=httpx.MockTransport(lambda r: 1 / 0)), tmp_path / "guesses.json")
    assert again.find("Nobody Corp", "Software Engineer") is None  # cached miss: no request made


def test_search_uses_board_copy_of_gmail_job(tmp_path, monkeypatch):
    monkeypatch.delenv("ADZUNA_APP_ID", raising=False)
    settings = load_settings(tmp_path)
    settings.data_dir.mkdir()
    (settings.data_dir / "gmail_token.json").write_text("{}")
    monkeypatch.setattr(gmail, "service", lambda token: object())
    monkeypatch.setattr(gmail, "fetch_alerts", lambda svc, days, seen: [_email()])

    def fake_extract(system, text):
        return gmail.AlertJobs(jobs=[
            gmail.AlertJob(title="Software Engineer", company="Acme Inc", location="San Francisco, CA", link="L1")
        ])

    tracker = Tracker(tmp_path / "tracker.xlsx", tmp_path / "jobs")
    report = run_search(settings, tracker, client=_client(), now=NOW, extract=fake_extract)

    assert report.emails_read == 1 and report.new_boards == 1
    assert report.by_source == {"greenhouse": 1}  # LinkedIn copy replaced by the full board posting
    (row,) = tracker.jobs.values()
    assert tracker.description(row["id"]) == "Full text"
    assert gmail.load_seen(settings.data_dir / "gmail_seen.json") == {"m1"}


def test_startup_alert_senders():
    assert gmail._platform("Wellfound <team@hi.wellfound.com>") == "wellfound"
    assert gmail._platform("Built In <jobs@builtin.com>") == "builtin"
    assert "from:workatastartup.com" in gmail.alert_query(7)
    url = gmail.canonical_job_link("wellfound", "https://wellfound.com/jobs/123-swe?utm_source=email#top")
    assert url == "https://wellfound.com/jobs/123-swe"


def test_more_job_sites_alerts_are_read():
    for sender, platform in [("Glassdoor Jobs <noreply@glassdoor.com>", "glassdoor"),
                             ("ZipRecruiter <alerts@ziprecruiter.com>", "ziprecruiter"),
                             ("Dice <jobs@connect.dice.com>", "dice"), ("Handshake <mail@joinhandshake.com>", "handshake")]:
        assert gmail._platform(sender) == platform
    assert "from:glassdoor.com" in gmail.alert_query(7)
