"""The internship & part-time track: its own list, the UMass Amherst MS on resumes and forms."""
from datetime import timedelta

from jobagent.apply.answers import answer_for
from jobagent.apply.runner import for_job
from jobagent.profile import MasterResume
from jobagent.score.funnel import run_funnel
from jobagent.search import simplify
from jobagent.tailor.render import resume_data
from jobagent.tailor.writer import validate

from test_funnel import NOW, A, _tracker
from test_tailor import MASTER, PROFILE, good

STUDENT = MasterResume.model_validate({**MASTER.model_dump(), "education": [
    {"school": "University of Massachusetts Amherst", "degree": "Master of Science in Computer Science",
     "location": "Amherst, MA", "start": "2027-02", "end": "2028-12", "expected": True, "only_for": ["intern_pt"]},
    *[e.model_dump() for e in MASTER.education]]})
PROFILE_S = PROFILE.model_copy(update={"intern_location": "Amherst, MA"})


def test_internship_list_keeps_2027_terms_only():
    base = {"source": "Simplify", "category": "Software", "company_name": "Acme", "active": True, "is_visible": True,
            "date_posted": int((NOW - timedelta(days=1)).timestamp()), "locations": ["Boston, MA"]}
    listings = [{**base, "title": "Software Engineer Intern", "terms": ["Summer 2027"], "url": "https://x.io/1"},
                {**base, "title": "Data Intern", "terms": ["Summer 2026"], "url": "https://x.io/2"},
                {**base, "title": "ML Intern", "terms": ["Winter 2027"], "url": "https://x.io/3"}]
    jobs = simplify.recent_jobs(listings, NOW - timedelta(days=7), source="intern_list", kind="intern_pt")
    assert [(j["title"], j["kind"]) for j in jobs] == [("Software Engineer Intern", "intern_pt")]


def test_two_shortlists_filled_separately(tmp_path):
    t = _tracker(tmp_path, [{"title": f"Software Engineer {i}", "desc": "ft"} for i in range(4)]
                 + [{"title": f"Data Intern {i}", "desc": "intern"} for i in range(4)])
    r = run_funnel(t, lambda j, d: A(employment_type="internship" if d.startswith("intern") else "full_time"),
                   top_n=2, top_n_intern=3, max_age_days=7, fresh_hours=24, now=NOW, workers=1)
    kinds = sorted(j["kind"] for j in r.shortlisted)
    assert kinds == ["full_time", "full_time", "intern_pt", "intern_pt", "intern_pt"]


def test_internship_resume_shows_ms_first_and_study_location_fulltime_does_not():
    intern = resume_data(good(), STUDENT, PROFILE_S, "intern_pt")
    assert intern["sections"][0] == "education" and intern["p"].location == "Amherst, MA"
    assert intern["education"][0]["dates"] == "Feb 2027 – Dec 2028 (expected)"
    full = resume_data(good(), STUDENT, PROFILE_S, "full_time")
    assert all("Massachusetts" not in e["school"] for e in full["education"]) and full["p"].location == "Los Angeles, CA"


def test_forms_use_completed_school_for_full_time_and_student_answers_for_internships():
    answers = {"intern_pt": {"school": "University of Massachusetts Amherst", "graduation": "December 2028",
                             "degree_pursuing": "Master's degree (MS in Computer Science)"}}
    assert answer_for("School", "text", PROFILE_S, answers, STUDENT, "X").value == "NMIT"
    p, a = for_job({"kind": "intern_pt"}, PROFILE_S, answers)
    assert p.location == "Amherst, MA"
    assert answer_for("University", "text", p, a, STUDENT, "X").value == "University of Massachusetts Amherst"
    assert answer_for("Expected graduation date", "text", p, a, STUDENT, "X").value == "December 2028"
    assert answer_for("What degree are you currently pursuing?", "text", p, a, STUDENT, "X").value.startswith("Master")


def test_only_internship_applications_may_mention_the_ms():
    summary = good(summary="Incoming MS in Computer Science student starting 2027 with Python and SQL experience.")
    assert validate(summary, STUDENT, kind="intern_pt") == []
    assert any("2027" in p for p in validate(summary, STUDENT, kind="full_time"))
