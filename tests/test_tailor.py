import io

import pytest
from pypdf import PdfReader

from jobagent.config import ROOT
from jobagent.profile import MasterResume, Profile
from jobagent.tailor.render import Renderer, resume_data
from jobagent.tailor.runner import run_tailor
from jobagent.tailor.writer import TailorError, Tailored, tailor, validate
from jobagent.tracker import Tracker

MASTER = MasterResume.model_validate({
    "experience": [
        {"company": "IBM", "title": "Application Developer", "start": "2025-09", "end": "2026-10",
         "bullets": ["Resolved 100+ production incidents with 100% SLA compliance.",
                     "Developed 50+ SOQL queries to troubleshoot production issues."],
         "facts": ["Used SQL for data retrieval and analytics."]},
        {"company": "Chimple Learning", "title": "Data Analyst Intern", "start": "2025-07", "end": "2025-08",
         "bullets": ["Analyzed product data using PostgreSQL and BigQuery."]},
    ],
    "projects": [
        {"name": "Fraud Detection API", "tech": ["Python", "FastAPI", "Docker"],
         "bullets": ["Built a fraud detection API trained on 284K+ transactions."], "facts": ["AUC-ROC of 0.953."]},
        {"name": "Secret Project", "tech": ["Apache Spark"], "bullets": ["Parallelized an algorithm."], "verified": False},
        {"name": "Earth & Thread", "bullets": ["Founded a sustainability venture."], "hide_for": ["software"]},
    ],
    "education": [{"school": "NMIT", "degree": "BE Information Science", "start": "2020-12", "end": "2024-07",
                   "gpa": "8.69/10", "notes": ["Exchange semester at NDSU"]}],
    "skills": [{"category": "Programming", "items": ["Python", "SQL", "SOQL"]},
               {"category": "Tools", "items": ["FastAPI", "Docker", "PostgreSQL", "BigQuery"]}],
})
PROFILE = Profile(name="Alex Jordan Rivera", location="Los Angeles, CA", phone="+1 (555) 010-0123",
                  email="c@example.com", github="https://github.com/c", linkedin="https://www.linkedin.com/in/c")
LETTER = ("Dear Hiring Team,\n\n" + "I built a fraud detection API in Python and FastAPI that scored transactions. "
          * 12 + "\n\nAlex Jordan Rivera")


def good(**over) -> Tailored:
    base = {
        "track": "software",
        "summary": "Python developer with production support and API experience.",
        "experience": [
            {"ref": "E1", "bullets": [{"text": "Resolved 100+ production incidents at 100% SLA compliance.",
                                       "sources": ["E1.1"]},
                                      {"text": "Wrote 50+ SOQL and SQL queries to debug production data.",
                                       "sources": ["E1.2", "E1.F1"]}]},
            {"ref": "E2", "bullets": [{"text": "Analyzed product data in PostgreSQL and BigQuery.",
                                       "sources": ["E2.1"]}]},
        ],
        "projects": [{"ref": "P1", "bullets": [{"text": "Built a Python/FastAPI fraud API on 284K+ transactions "
                                                         "with AUC-ROC of 0.953.", "sources": ["P1.1", "P1.F1"]}]}],
        "skills": [{"category": "Languages", "items": ["Python", "SQL"]}],
        "job_keywords": ["Python", "FastAPI", "Kubernetes"],
        "cover_letter": LETTER,
    }
    return Tailored.model_validate({**base, **over})


def test_good_output_passes():
    assert validate(good(), MASTER) == []


def test_rejects_fake_skill_in_skills_list():
    t = good(skills=[{"category": "Languages", "items": ["Python", "Kubernetes"]}])
    assert any("Kubernetes" in p for p in validate(t, MASTER))


def test_rejects_invented_metric_and_tech_in_bullet():
    exp = good().model_dump()["experience"]
    exp[0]["bullets"][0] = {"text": "Resolved 300+ incidents using Kubernetes.", "sources": ["E1.1"]}
    problems = validate(good(experience=exp), MASTER)
    assert any("300" in p for p in problems) and any("kubernetes" in p for p in problems)


def test_rejects_bad_citation_hidden_or_unverified_entries_and_missing_role():
    exp = good().model_dump()["experience"][:1]  # Chimple dropped
    exp[0]["bullets"][0]["sources"] = ["E9.1"]
    projects = [{"ref": "P2", "bullets": [{"text": "Parallelized an algorithm.", "sources": ["P2.1"]}]},
                {"ref": "P3", "bullets": [{"text": "Founded a venture.", "sources": ["P3.1"]}]}]
    problems = validate(good(experience=exp, projects=projects), MASTER)
    assert any("E9.1" in p for p in problems)
    assert any("E2" in p and "missing" in p for p in problems)
    assert any(p.startswith("P2 ") for p in problems)  # unverified
    assert any(p.startswith("P3 ") for p in problems)  # hidden on software resumes


def test_cover_letter_must_be_signed_by_candidate():
    assert validate(good(), MASTER, "Alex Jordan Rivera") == []
    unsigned = good(cover_letter=LETTER.replace("Alex Jordan Rivera", "The Candidate"))
    assert any("signed" in p for p in validate(unsigned, MASTER, "Alex Jordan Rivera"))


def test_cover_letter_rules():
    short = good(cover_letter="Dear Hiring Team, I am passionate about this. Alex")
    problems = validate(short, MASTER)
    assert any("words" in p for p in problems) and any("passionate" in p for p in problems)
    claims = good(cover_letter=LETTER.replace("Python and FastAPI", "Java with 5 years"))
    assert any("java" in p for p in validate(claims, MASTER)) and any("5" in p for p in validate(claims, MASTER))


def test_retries_with_feedback_then_gives_up():
    calls = []

    def gen(system, content):
        calls.append(content)
        return good() if len(calls) == 2 else good(skills=[{"category": "X", "items": ["Rust"]}])

    assert tailor({"company": "A", "title": "SWE"}, "desc", MASTER, gen).skills[0].items == ["Python", "SQL"]
    assert "Rust" in calls[1] and "rejected" in calls[1]
    with pytest.raises(TailorError):
        tailor({"company": "A", "title": "SWE"}, "desc", MASTER, lambda s, c: good(skills=[{"category": "X",
                                                                                          "items": ["Rust"]}]))


def test_renders_one_page_pdfs_with_master_facts(tmp_path):
    tracker = Tracker(tmp_path / "t.xlsx", tmp_path / "jobs")
    tracker.add_job({"company": "Acme", "title": "Software Engineer", "url": "https://x.io/1", "source": "lever",
                     "description": "Python, FastAPI, Kubernetes"})
    (jid,) = tracker.jobs
    tracker.update(jid, status="shortlisted")
    with Renderer(ROOT / "templates") as r:
        report = run_tailor(tracker, MASTER, PROFILE, lambda s, c: good(), r, tmp_path / "out")
    assert not report.errors
    (job,) = report.prepared
    assert tracker.jobs[jid]["status"] == "prepared"
    assert job["coverage"] == 67 and job["missing"] == ["Kubernetes"]  # Python, FastAPI found
    folder = job["folder"]
    pdf = PdfReader(folder / "Alex_Jordan_Rivera_Resume_Acme_Software_Engineer.pdf")
    text = pdf.pages[0].extract_text()
    assert len(pdf.pages) == 1
    assert "Application Developer" in text and "Sep 2025" in text and "Oct 2026" in text  # dates from master
    assert (folder / "Alex_Jordan_Rivera_Cover_Letter_Acme_Software_Engineer.txt").read_text() == LETTER
    assert len(PdfReader(folder / "Alex_Jordan_Rivera_Cover_Letter_Acme_Software_Engineer.pdf").pages) == 1


def test_overlong_resume_is_trimmed_to_one_page():
    long_bullets = [{"text": "Resolved 100+ production incidents at 100% SLA compliance. " * 5, "sources": ["E1.1"]}] * 6
    t = good(experience=[{"ref": "E1", "bullets": long_bullets}, {"ref": "E2", "bullets": long_bullets}],
             projects=[{"ref": "P1", "bullets": [{"text": "Built a fraud detection API trained on 284K+ "
                                                          "transactions. " * 4, "sources": ["P1.1"]}] * 3}])
    with Renderer(ROOT / "templates") as r:
        pdf, _, trimmed, pages = r.resume(resume_data(t, MASTER, PROFILE))
    assert pages == 1 and trimmed > 0 and len(PdfReader(io.BytesIO(pdf)).pages) == 1


def test_keyword_matching_handles_word_forms():
    from jobagent.tailor.writer import _stems, has_keyword
    stems = _stems("Automated statistical evaluation via a FastAPI API; relational data in PostgreSQL")
    assert has_keyword("APIs", stems) and has_keyword("automation", stems)
    assert not has_keyword("Kubernetes", stems)


def test_keyword_passes_until_only_unsupported_keywords_are_missing():
    calls = []
    low = good(job_keywords=["Python", "Kubernetes", "Rust", "Go"])
    honest = good(job_keywords=["Python", "Kubernetes", "Rust", "Go"], unsupported_keywords=["Kubernetes", "Rust", "Go"])

    def gen(system, content):
        calls.append(content)
        return low if len(calls) == 1 else honest

    assert tailor({"company": "A", "title": "SWE"}, "desc", MASTER, gen) is honest
    assert len(calls) == 2 and "Kubernetes, Rust, Go" in calls[1]


def test_cover_letter_must_open_with_researched_fact_and_may_use_its_numbers():
    research = ["C1: Acme launched an AI fraud-review tool used by 300 banks (https://acme.com/news)"]
    letter = LETTER.replace("Dear Hiring Team,\n\n", "Dear Hiring Team,\n\nYour fraud-review tool now serves 300 banks. ")
    assert any("company_hook" in p for p in validate(good(cover_letter=letter), MASTER, research=research))
    assert validate(good(cover_letter=letter, company_hook="C1"), MASTER, research=research) == []
    # Research numbers are allowed in the letter, but never in the resume summary.
    assert any("300" in p for p in validate(good(summary="Built tools for 300 banks."), MASTER, research=research))


def test_summary_cannot_claim_unheld_title():
    assert any("title" in p for p in validate(good(summary="Machine learning engineer with Python skills."), MASTER))
    assert validate(good(summary="Application Developer with Python and SQL experience."), MASTER) == []


def test_file_names_say_which_job():
    from jobagent.tailor.runner import doc_names
    d = doc_names(PROFILE, {"company": "SharkNinja", "title": "Forward Deployed Engineer - Entry Level, Platform Team"})
    assert d.resume == "Alex_Jordan_Rivera_Resume_SharkNinja_Forward_Deployed_Engineer_Entry_Level.pdf"
    assert d.letter_pdf.startswith("Alex_Jordan_Rivera_Cover_Letter_SharkNinja_")
    assert d.letter_txt.endswith("_Entry_Level.txt")


SF_MASTER = MasterResume.model_validate({
    **MASTER.model_dump(),
    "experience": [
        {**MASTER.experience[0].model_dump(),
         "track_facts": {"salesforce": ["The four enterprise applications were Salesforce orgs.",
                                        "Built and debugged Salesforce Flows."]}},
        MASTER.experience[1].model_dump(),
    ],
    "skills": [*(g.model_dump() for g in MASTER.skills),
               {"category": "Salesforce", "only_for": ["salesforce"], "items": ["Salesforce Flows", "Apex (training)"]}],
    "achievements": [{"title": "Salesforce Certified Administrator", "category": "Certification",
                      "organization": "Salesforce", "date": "2025", "hide_for": ["software", "ai_ml", "data_support"]}],
})


def sf(**over) -> Tailored:
    exp = good().model_dump()["experience"]
    exp[0]["bullets"].append({"text": "Built and debugged Salesforce Flows across four Salesforce orgs.",
                              "sources": ["E1.T1", "E1.T2"]})
    return good(**{"track": "salesforce", "experience": exp,
                   "skills": [{"category": "Salesforce", "items": ["Salesforce Flows", "Apex (training)"]}], **over})


def test_salesforce_facts_and_skills_allowed_on_salesforce_resumes():
    assert validate(sf(), SF_MASTER) == []


def test_other_resumes_never_mention_salesforce():
    problems = validate(sf(track="software"), SF_MASTER)
    assert any("E1.T1" in p for p in problems)  # Salesforce-only line cited on a software resume
    assert any("Salesforce Flows" in p for p in problems)  # Salesforce-only skill
    assert any("only Salesforce-track" in p for p in problems)
    sneaky = good(summary="Application Developer who supported Salesforce applications.")
    assert any("only Salesforce-track" in p for p in validate(sneaky, SF_MASTER))


def test_salesforce_resume_shows_certification_and_others_dont():
    with Renderer(ROOT / "templates") as r:
        _, sf_text, _, _ = r.resume(resume_data(sf(), SF_MASTER, PROFILE))
        _, sw_text, _, _ = r.resume(resume_data(good(), SF_MASTER, PROFILE))
    assert "CERTIFICATIONS" in sf_text and "Salesforce Certified Administrator, Salesforce (2025)" in sf_text
    assert "Salesforce" not in sw_text and "U.S. Citizen" in sw_text


def test_constrained_form_only_accepts_real_ids_and_skills():
    import anthropic
    from pydantic import ValidationError

    from jobagent.tailor.writer import constrained_models

    CTailored, _ = constrained_models(MASTER)
    assert CTailored.model_validate(good().model_dump()).skills[0].items == ["Python", "SQL"]
    with pytest.raises(ValidationError):  # a skill that isn't in the master list can't even be expressed
        CTailored.model_validate(good(skills=[{"category": "X", "items": ["Kubernetes"]}]).model_dump())
    bad = good().model_dump()
    bad["experience"][0]["bullets"][0]["sources"] = ["E9.9"]
    with pytest.raises(ValidationError):
        CTailored.model_validate(bad)
    schema = str(anthropic.transform_schema(CTailored.model_json_schema()))
    assert "'E1.1'" in schema and "'Python'" in schema  # sent to Claude as fixed choices


def test_apply_patch_changes_only_what_it_names():
    from jobagent.tailor.writer import Patch, apply_patch

    t = good()
    p = Patch.model_validate({"bullet_edits": [
        {"ref": "E1", "index": 0, "bullet": {"text": "Resolved 100+ incidents at 100% SLA compliance.", "sources": ["E1.1"]}},
        {"ref": "E1", "index": 1, "bullet": None},
        {"ref": "P1", "index": 5, "bullet": {"text": "Deployed the API with Docker.", "sources": ["P1.1"]}},
    ], "summary": "Application Developer with Python and SQL experience."})
    out = apply_patch(t, p)
    assert [b.text for b in out.experience[0].bullets] == ["Resolved 100+ incidents at 100% SLA compliance."]
    assert out.projects[0].bullets[-1].text == "Deployed the API with Docker."
    assert out.summary.startswith("Application Developer") and out.cover_letter == t.cover_letter  # untouched


def test_fixes_use_small_patches_not_full_rewrites():
    from jobagent.tailor.writer import Patch

    full_calls, patch_calls = [], []
    broken = good(skills=[{"category": "X", "items": ["Rust"]}])

    def gen(system, content):
        full_calls.append(content)
        return broken

    def patch(system, content):
        patch_calls.append(content)
        return Patch.model_validate({"skills": [{"category": "Languages", "items": ["Python", "SQL"]}],
                                     "unsupported_keywords": ["Kubernetes"]})

    t = tailor({"company": "A", "title": "SWE"}, "desc", MASTER, gen, patch=patch)
    assert len(full_calls) == 1 and len(patch_calls) == 1
    assert "patch" in patch_calls[0] and "Rust" in patch_calls[0]
    assert t.skills[0].items == ["Python", "SQL"] and validate(t, MASTER) == []


def test_search_urls_are_found_in_any_tool_result_shape():
    from jobagent.llm import _urls

    nested = {"type": "code_execution_tool_result", "content": {"stdout": 'pages: "https://acme.com/news/1", '
                                                                          "and https://acme.com/blog/ai)."},
              "extra": [{"url": "https://sec.gov/x"}]}
    assert sorted(_urls(nested)) == ["https://acme.com/blog/ai", "https://acme.com/news/1", "https://sec.gov/x"]


def test_trimming_protects_the_most_recent_role():
    from jobagent.tailor.render import trim_once

    data = {"achievements": [], "education": [{"notes": ["x"]}],
            "experience": [{"bullets": ["i1", "i2", "i3", "i4"]}, {"bullets": ["c1", "c2"]}, {"bullets": ["g1"]}],
            "projects": [{"bullets": ["a1", "a2", "a3"]}, {"bullets": ["b1"]}]}
    order = []
    while trim_once(data):
        order.append((len(data["experience"][0]["bullets"]), len(data["experience"][1]["bullets"]),
                      len(data["projects"])))
    # Older role first (to 1 bullet), then the extra project, then the main project (to 2), and the latest
    # role last, never below 3.
    assert order[:2] == [(4, 1, 2), (4, 1, 1)]
    assert data["projects"][0]["bullets"] == ["a1", "a2"] and data["experience"][0]["bullets"] == ["i1", "i2", "i3"]
    assert order.index((3, 1, 1)) > order.index((4, 1, 1))
