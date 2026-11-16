from datetime import date, timedelta

from jobagent.insights import insights
from jobagent.score.funnel import FunnelReport
from jobagent.search.runner import SearchReport
from jobagent.summary import build_summary
from jobagent.tailor.runner import TailorReport
from jobagent.tracker import Tracker

TODAY = date(2026, 11, 20)


def _tracker(tmp_path, n=12):
    t = Tracker(tmp_path / "t.xlsx", tmp_path / "jobs")
    for i in range(n):
        t.add_job({"company": f"Co{i}", "title": "Software Engineer", "url": f"https://x.io/{i}",
                   "apply_url": f"https://job-boards.greenhouse.io/co{i}/jobs/{i}", "source": "greenhouse"})
    for i, jid in enumerate(t.jobs):
        t.set_status(jid, "applied", on=TODAY - timedelta(days=20))
        t.update(jid, track="software" if i < 6 else "data_support", score=88 if i < 6 else 70)
        if i < 3:
            t.update(jid, status="interview")
        elif i >= 9:
            t.update(jid, status="rejected")
    return t


def test_insights_counts_outcomes_and_gives_advice_after_enough_data(tmp_path):
    lines = insights(_tracker(tmp_path), TODAY)
    assert lines[0].startswith("12 applications: 3 interviews/offers, 3 rejections, 6 no reply")
    assert any(l.startswith("Track: software 6 sent, 3 interviews (50% of answered)") for l in lines)
    assert "Best so far" in lines[-1]
    assert "Advice starts after" in insights(_tracker(tmp_path / "small", n=4), TODAY)[-1]


def test_summary_has_sources_funnel_top_jobs_and_links(tmp_path):
    t = _tracker(tmp_path, n=2)
    jid = next(iter(t.jobs))
    t.save_detail(jid, score_parts={"fit": 30, "level": 20, "recency": 20, "company": 12, "odds": 7})
    job = {**t.jobs[jid], "score": 89, "track": "software", "covered": 95, "hook": True}
    search = SearchReport(by_source={"greenhouse": 40, "adzuna": 12}, added=52, boards_polled=1250)
    funnel = FunnelReport(considered=52, dropped={"3 experience": 20}, passed=18, enriched=4, not_found=2)
    text = build_summary(t, search, funnel, TailorReport(prepared=[job]), today=TODAY)
    for want in ("Company boards (Greenhouse) | 40", "Adzuna search | 12", "dropped 20: needs 3+ years",
                 "**4 confirmed**", "| 1 | **89**", "researched letter", "95% of yours",
                 "[open](https://job-boards.greenhouse.io/", "## 5. What's working so far"):
        assert want in text, want
