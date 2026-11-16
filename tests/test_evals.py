import json

from jobagent.evals import label_jobs, load_labels, run_eval
from jobagent.score.funnel import Assessment
from jobagent.tracker import Tracker

LONG = " Python SQL data pipelines and APIs." * 40


def A(**over):
    base = dict(us_eligible="yes", role_family="software", is_tech_role=True, low_skill=False, required_years=None,
                preferred_years=None, expected_level="entry", requires_clearance=False, degree_required="bachelor",
                skills_match=70, matching_points=[], missing_requirements=[], red_flags=[], summary="ok")
    return Assessment(**{**base, **over})


def _tracker(tmp_path):
    t = Tracker(tmp_path / "t.xlsx", tmp_path / "jobs")
    for i, title in enumerate(["Software Engineer", "Data Analyst", "Backend Engineer", "Platform Engineer"]):
        t.add_job({"company": f"Co{i}", "title": title, "url": f"https://x.io/{i}", "source": "greenhouse",
                   "description": f"job{i}" + LONG})
    ids = list(t.jobs)
    t.update(ids[0], score=80)
    t.update(ids[1], score=70)
    t.update(ids[2], status="skipped", filtered_reason="3 experience: requires 5+ years")
    t.update(ids[3], status="skipped", filtered_reason="5 fit: weak")
    return t, ids


def test_labelling_mixes_passed_and_dropped_jobs_and_saves(tmp_path):
    t, ids = _tracker(tmp_path)
    path = tmp_path / "eval" / "jobs.jsonl"
    answers = iter(["y", "n", "s", "m"])
    assert label_jobs(t, path, 4, lambda p: next(answers), lambda line: None) == 3
    labels = load_labels(path)
    assert sorted(lab["label"] for lab in labels.values()) == ["maybe", "no", "yes"]
    # Already-labelled jobs aren't offered again.
    assert label_jobs(t, path, 4, lambda p: "q", lambda line: None) == 0


def test_eval_reports_missed_good_jobs_and_wrongly_kept_bad_ones(tmp_path):
    t, ids = _tracker(tmp_path)
    labels = {ids[0]: {"id": ids[0], "label": "yes"}, ids[1]: {"id": ids[1], "label": "yes"},
              ids[2]: {"id": ids[2], "label": "no"}, ids[3]: {"id": ids[3], "label": "maybe"}}
    screen = lambda job, d: A(skills_match=20) if d.startswith("job1") else A()  # noqa: E731
    assess = lambda job, d: A()  # noqa: E731 - the careful read keeps everything it sees
    r = run_eval(t, labels, assess, screen)
    assert (r.yes, r.kept_yes, r.no) == (2, 1, 1)
    assert r.screen_drops_of_yes == 1 and "Data Analyst" in r.missed[0]  # the cheap screen lost a good job
    assert r.let_through == ["Co2 - Backend Engineer"]
    assert t.jobs[ids[2]]["status"] == "skipped"  # tracker untouched
