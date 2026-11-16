from datetime import datetime, timedelta, timezone

import pytest

from jobagent.profile import MasterResume
from jobagent.score.funnel import Assessment, candidate_brief, run_funnel
from jobagent.search.common import looks_tech
from jobagent.tracker import Tracker

NOW = datetime(2026, 11, 11, 12, tzinfo=timezone.utc)
FRESH = (NOW - timedelta(hours=3)).isoformat()
TWO_DAYS = (NOW - timedelta(days=2)).isoformat()


def A(**over) -> Assessment:
    base = dict(
        us_eligible="yes", role_family="software", is_tech_role=True, low_skill=False,
        required_years=None, preferred_years=None, expected_level="entry", requires_clearance=False, degree_required="bachelor",
        skills_match=70, matching_points=["Python APIs"], missing_requirements=[], red_flags=[],
        summary="Python backend work close to the FastAPI projects.",
    )
    return Assessment(**{**base, **over})


@pytest.mark.parametrize("title,keep", [
    ("SWE II", True), ("Member of Technical Staff", True), ("Engineer I", True),
    ("Business Analyst", True), ("Product Analyst", True), ("Data Entry Specialist", True),  # Claude decides
    ("Recruiter", False), ("Senior Account Executive", False), ("Warehouse Associate", False),
])
def test_search_net_is_wide(title, keep):
    assert looks_tech(title) is keep


FILLER = " The role involves Python, SQL and building data products with the team." * 30


def full(key: str) -> str:
    """A realistic-length posting whose first word is the key the fake assessor answers by."""
    return key + FILLER


def _tracker(tmp_path, jobs):
    t = Tracker(tmp_path / "t.xlsx", tmp_path / "jobs")
    for i, j in enumerate(jobs):
        t.add_job({"company": f"Co{i}", "location": "Remote - US", "source": "greenhouse", "ats_type": "greenhouse",
                   "url": f"https://job-boards.greenhouse.io/co{i}/jobs/{i}", "posted_at": FRESH,
                   "description": full(j.pop("desc", "desc")), **j})
    return t


def test_funnel_stages_in_order_and_top_n(tmp_path):
    t = _tracker(tmp_path, [
        {"title": "Software Engineer", "desc": "great"},
        {"title": "Software Engineer II", "desc": "ok", "posted_at": TWO_DAYS},
        {"title": "Engineer", "desc": "india"},
        {"title": "Engineer, Platform", "desc": "hr"},
        {"title": "Backend Engineer", "desc": "5yrs"},
        {"title": "Data Analyst", "desc": "entry"},
        {"title": "Research Engineer", "desc": "research"},
        {"title": "Solutions Engineer", "desc": "weak"},
        {"title": "Senior Software Engineer", "desc": "never read"},
        {"title": "Software Engineer Intern", "desc": "unpaidintern"},
        {"title": "Python Developer", "location": "London, UK", "desc": "never read"},
    ])
    answers = {
        "great": A(skills_match=90),
        "ok": A(skills_match=60, preferred_years=3),
        "india": A(us_eligible="no"),
        "hr": A(is_tech_role=False, role_family="non_tech", summary="HR coordinator"),
        "5yrs": A(required_years=5),
        "entry": A(role_family="data_analyst", low_skill=True, summary="Typing invoices into a spreadsheet"),
        "research": A(expected_level="senior", summary="Needs a publication record"),
        "weak": A(skills_match=30, role_family="support_solutions"),
        "unpaidintern": A(red_flags=["Unpaid internship"]),  # internships are read now; unpaid ones dropped
    }
    read = []

    def fake_assess(job, desc):
        assert not desc.startswith("never read"), "precheck should drop this before paying Claude"
        key = desc.split()[0]
        read.append(key)
        return answers[key]

    r = run_funnel(t, fake_assess, top_n=1, max_age_days=7, fresh_hours=24, now=NOW, workers=1)

    assert sorted(read) == sorted(answers)
    assert r.dropped == {"1 us": 2, "2 tech": 1, "3 experience": 3, "4 career": 2, "5 fit": 1}
    assert r.passed == 2
    (top,) = r.shortlisted
    assert top["title"] == "Software Engineer"
    # fit 35*0.9 + level 20 + recency 20 (<24h) + company 15*0.5 + odds 10*0.5
    assert top["score"] == 84.0
    second = next(j for j in t.jobs.values() if j["title"] == "Software Engineer II")
    assert second["status"] == "found" and second["stretch"] == "yes"
    assert second["score"] == 35 * 0.6 + 20 + 0 + 7.5 + 5  # no recency points after 24h
    dropped = next(j for j in t.jobs.values() if j["title"] == "Backend Engineer")
    assert dropped["status"] == "skipped" and "5+ years" in dropped["filtered_reason"]
    assert "assessment" in (tmp_path / "jobs" / f"{dropped['id']}.json").read_text()


def test_two_years_required_is_allowed_but_scores_lower(tmp_path):
    t = _tracker(tmp_path, [{"title": "Software Engineer", "desc": "a"}, {"title": "Backend Engineer", "desc": "b"}])
    r = run_funnel(t, lambda j, d: A(required_years=2 if d.startswith("b ") else 0), top_n=5, max_age_days=7,
                   fresh_hours=24, now=NOW, workers=1)
    scores = {j["title"]: j["score"] for j in r.shortlisted}
    assert scores["Software Engineer"] - scores["Backend Engineer"] == 10


def test_failed_read_is_reported_and_retried_next_run(tmp_path):
    t = _tracker(tmp_path, [{"title": "Software Engineer"}])

    def boom(job, desc):
        raise RuntimeError("api down")

    r = run_funnel(t, boom, top_n=5, max_age_days=7, fresh_hours=24, now=NOW, workers=1)
    assert r.errors and not r.shortlisted
    (row,) = t.jobs.values()
    assert row["status"] == "found" and row.get("score") is None


def test_candidate_brief_leaves_out_unverified():
    r = MasterResume.model_validate({
        "experience": [{"company": "IBM", "title": "Dev", "start": "2025-09", "end": "2026-10", "bullets": ["x"]}],
        "projects": [{"name": "Secret", "bullets": ["y"], "verified": False}],
        "education": [{"school": "NMIT", "degree": "BE"}],
        "skills": [{"category": "L", "items": ["Python"]}],
    })
    brief = candidate_brief(r)
    assert "IBM" in brief and "Secret" not in brief and "Python" in brief


def test_daily_limit_reads_entry_level_first(tmp_path):
    t = _tracker(tmp_path, [{"title": "Software Engineer"}, {"title": "Software Engineer, New Grad"},
                            {"title": "Platform Engineer"}, {"title": "Software Engineer I"}])
    read = []
    r = run_funnel(t, lambda j, d: read.append(j["title"]) or A(), top_n=5, max_age_days=7,
                   fresh_hours=24, now=NOW, workers=1, limit=2)
    assert sorted(read) == ["Software Engineer I", "Software Engineer, New Grad"]
    assert "the rest tomorrow" in r.errors[0]


def _adzuna(t):
    t.add_job({"company": "Rokt", "title": "Junior Software Engineer", "location": "New York, NY", "source": "adzuna",
               "url": "https://www.adzuna.com/land/ad/1", "apply_url": "https://www.adzuna.com/land/ad/1",
               "posted_at": FRESH, "description": "snippet"})


def test_job_board_links_are_traced_and_reread_before_shortlisting(tmp_path):
    t = _tracker(tmp_path, [])
    _adzuna(t)
    reads = []

    def assess(job, desc):
        reads.append(desc[:7])
        return A(skills_match=50 if desc == "snippet" else 95, company_outlook=80)

    def enrich(job, desc):
        return {"apply_url": "https://rokt.wd5.myworkdayjobs.com/Careers/job/1", "description": full("fullpost")}

    r = run_funnel(t, assess, top_n=5, max_age_days=7, fresh_hours=24, now=NOW, workers=1, enrich=enrich)
    (top,) = r.shortlisted
    assert reads == ["snippet", "fullpos"]  # read again with the full posting
    assert top["apply_url"].startswith("https://rokt.wd5") and top["ats_type"] == "workday"
    assert top["score"] == round(35 * 0.95 + 20 + 20 + 15 * 0.8 + 5, 1) and r.enriched == 1
    assert t.description(top["id"]).startswith("fullpost")


def test_untraceable_job_board_links_are_dropped(tmp_path):
    t = _tracker(tmp_path, [{"title": "Software Engineer"}])
    _adzuna(t)
    r = run_funnel(t, lambda j, d: A(), top_n=5, max_age_days=7, fresh_hours=24, now=NOW, workers=1,
                   enrich=lambda j, d: None)
    assert [j["company"] for j in r.shortlisted] == ["Co0"]
    assert r.not_found == 1 and r.dropped == {"link": 1}


def test_without_enrichment_snippet_jobs_wait_for_tomorrow(tmp_path):
    t = _tracker(tmp_path, [])
    _adzuna(t)
    r = run_funnel(t, lambda j, d: A(), top_n=5, max_age_days=7, fresh_hours=24, now=NOW, workers=1)
    assert r.shortlisted == [] and next(iter(t.jobs.values()))["status"] == "found"


@pytest.mark.parametrize("title,keep", [("Salesforce Administrator", True), ("Junior Salesforce Developer", True),
                                        ("CRM Analyst", True), ("Salesforce Account Executive", False)])
def test_salesforce_titles_are_searched(title, keep):
    assert looks_tech(title) is keep


def test_lenient_first_screen_then_careful_read(tmp_path):
    t = _tracker(tmp_path, [{"title": "Software Engineer", "desc": "clear"}, {"title": "Data Analyst", "desc": "border"},
                            {"title": "Backend Engineer", "desc": "fiveyrs"}])
    screened, careful = [], []

    def screen(pairs):
        screened.extend(d.split()[0] for _, d in pairs)
        answers = {"clear": A(skills_match=80), "border": A(skills_match=35), "fiveyrs": A(required_years=5)}
        return {j["id"]: answers[d.split()[0]] for j, d in pairs}

    def assess_many(pairs):
        careful.extend(d.split()[0] for _, d in pairs)
        return {j["id"]: A(skills_match=80 if d.startswith("clear") else 30) for j, d in pairs}

    r = run_funnel(t, None, top_n=5, max_age_days=7, fresh_hours=24, now=NOW, workers=1,
                   screen=screen, assess_many=assess_many)
    assert sorted(screened) == ["border", "clear", "fiveyrs"]
    assert sorted(careful) == ["border", "clear"]  # 35 passes the lenient bar (25); 5 years clearly fails
    assert r.screened_out == 1 and r.second_read == 2
    assert [j["title"] for j in r.shortlisted] == ["Software Engineer"]  # 30 < 35 at the careful read
    dropped = next(j for j in t.jobs.values() if j["title"] == "Backend Engineer")
    assert dropped["filtered_reason"].startswith("3 experience: first screen:")


def test_per_company_cap_and_other_fields_are_free_checks(tmp_path):
    jobs = [{"title": f"Software Engineer {n}", "company": "Bigco"} for n in range(5)]
    jobs.append({"title": "Mechanical Engineer", "company": "Other"})
    t = _tracker(tmp_path, jobs)
    read = []
    r = run_funnel(t, lambda j, d: read.append(j["title"]) or A(), top_n=10, max_age_days=7, fresh_hours=24,
                   now=NOW, workers=1)
    assert len(read) == 3 and "Mechanical Engineer" not in read
    assert any("wait for tomorrow" in e for e in r.errors)
    left = [j for j in t.jobs.values() if j["company"] == "Bigco" and j["status"] == "found" and j.get("score") is None]
    assert len(left) == 2  # read on a later day


def test_eval_driven_rules(tmp_path):
    t = _tracker(tmp_path, [{"title": "Regulatory Policy Analyst", "desc": "policy"},
                            {"title": "Mid-Level Engineer", "desc": "mid"}, {"title": "Data Intern", "desc": "intern"}])
    answers = {"policy": A(is_tech_role=False, role_family="business_analyst"), "mid": A(expected_level="mid"),
               "intern": A()}
    r = run_funnel(t, lambda j, d: answers[d.split()[0]], top_n=5, max_age_days=7, fresh_hours=24, now=NOW, workers=1)
    assert sorted(j["title"] for j in r.shortlisted) == ["Data Intern", "Regulatory Policy Analyst"]
    assert r.dropped == {"3 experience": 1}  # "Mid-Level" in the title is a free check


def test_mid_level_duties_rank_below_entry_level(tmp_path):
    t = _tracker(tmp_path, [{"title": "Associate Analyst", "desc": "mid"}, {"title": "Data Analyst", "desc": "entry"}])
    r = run_funnel(t, lambda j, d: A(expected_level="mid" if d.startswith("mid") else "entry"), top_n=5,
                   max_age_days=7, fresh_hours=24, now=NOW, workers=1)
    assert [j["title"] for j in r.shortlisted] == ["Data Analyst", "Associate Analyst"]  # kept, but ranked lower
    assert r.shortlisted[0]["score"] - r.shortlisted[1]["score"] == 20


@pytest.mark.parametrize("title", ["Technical Consultant", "Implementation Consultant", "Associate Consultant, Technology",
                                   "Implementation Specialist", "Solutions Consultant", "Technical Program Associate",
                                   "Rotational Program - Technology"])
def test_similar_titles_reach_claude(title):
    assert looks_tech(title)  # Claude then judges the actual work from the description


def test_careful_read_takes_the_best_screen_survivors(tmp_path):
    t = _tracker(tmp_path, [{"title": f"Software Engineer {i}", "company": f"Co{i}", "desc": f"j{i}"} for i in range(5)])
    careful = []

    def screen(pairs):
        return {j["id"]: A(skills_match=50 + 10 * int(d.split()[0][1:])) for j, d in pairs}

    def assess_many(pairs):
        careful.extend(d.split()[0] for _, d in pairs)
        return {j["id"]: A() for j, _ in pairs}

    r = run_funnel(t, None, top_n=10, max_age_days=7, fresh_hours=24, now=NOW, workers=1, screen=screen,
                   assess_many=assess_many, read_limit=2)
    assert careful == ["j4", "j3"]  # highest quick scores first
    assert any("wait for tomorrow's careful read" in e for e in r.errors)
    waiting = [j for j in t.jobs.values() if j["status"] == "found" and j.get("score") is None]
    assert len(waiting) == 3
