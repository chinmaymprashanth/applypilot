from pathlib import Path
from types import SimpleNamespace

import pytest

from jobagent import resume_import
from jobagent.llm import LLMError, parse
from jobagent.profile import (
    MasterResume, load_master_resume, load_profile, save_master_resume,
)

ROOT = Path(__file__).resolve().parent.parent

RESUME = {
    "experience": [
        {"company": "IBM", "title": "Software Engineer", "start": "2025-01", "end": None,
         "bullets": ["Built a Python service that cut report time by 40%"]},
    ],
    "projects": [{"name": "F1 dashboard", "bullets": ["Streamlit dashboard for race telemetry"]}],
    "education": [{"school": "NMIT", "degree": "BE Information Science"}],
    "skills": [{"category": "Languages", "items": ["Python", "SQL"]}],
}


def test_shipped_profile_loads_with_ibm_dates():
    p = load_profile(ROOT / "data" / "profile.yaml")
    assert p.us_citizen and not p.needs_sponsorship
    (ibm,) = p.fixed_dates
    assert (ibm.company, ibm.start, ibm.end) == ("IBM", "2025-09", "2026-10")


def test_fixed_dates_override_pdf_dates():
    r = MasterResume.model_validate(RESUME)
    p = load_profile(ROOT / "data" / "profile.yaml")
    assert r.apply_fixed_dates(p.fixed_dates) == []
    assert (r.experience[0].start, r.experience[0].end) == ("2025-09", "2026-10")


def test_master_resume_round_trip(tmp_path):
    r = MasterResume.model_validate(RESUME)
    save_master_resume(r, tmp_path / "m.yaml")
    assert load_master_resume(tmp_path / "m.yaml") == r
    assert r.all_skills() == {"python", "sql"}


def test_project_hidden_per_track():
    r = MasterResume.model_validate(
        {**RESUME, "projects": [{"name": "Shop", "bullets": ["x"], "hide_for": ["software"]}]}
    )
    assert not r.projects[0].shown_for("software")
    assert r.projects[0].shown_for("ai_ml")
    with pytest.raises(ValueError):
        MasterResume.model_validate({**RESUME, "projects": [{"name": "S", "bullets": ["x"], "hide_for": ["swe"]}]})


def test_unverified_entries_never_shown():
    r = MasterResume.model_validate({
        **RESUME,
        "achievements": [
            {"title": "Rank 1", "category": "Competition", "verified": False, "to_confirm": "rank"},
            {"title": "Sponsorship Head", "category": "Leadership", "hide_for": ["software"]},
        ],
    })
    unverified, sponsor = r.achievements
    assert not any(unverified.shown_for(t) for t in ("software", "ai_ml", "data_support"))
    assert not sponsor.shown_for("software") and sponsor.shown_for("data_support")


def test_shipped_master_resume_loads():
    path = ROOT / "data" / "master_resume.yaml"
    if not path.exists():
        pytest.skip("no master resume yet")
    r = load_master_resume(path)
    ibm = next(x for x in r.experience if x.company == "IBM")
    assert (ibm.start, ibm.end) == ("2025-09", "2026-10")


def test_unmatched_bullets_flags_rewording():
    r = MasterResume.model_validate(RESUME)
    text = "Built a Python service that cut report time by 40%\nStreamlit dashboard for race telemetry"
    assert resume_import.unmatched_bullets(r, text) == []
    r.experience[0].bullets.append("Led a team of 12 engineers")
    assert resume_import.unmatched_bullets(r, text) == ["Led a team of 12 engineers"]


def test_find_resume_pdf(tmp_path):
    with pytest.raises(FileNotFoundError):
        resume_import.find_resume_pdf(tmp_path)
    (tmp_path / "resume.pdf").write_bytes(b"%PDF")
    assert resume_import.find_resume_pdf(tmp_path).name == "resume.pdf"


class _FakeMessages:
    def __init__(self, response):
        self.response, self.kwargs = response, None

    def parse(self, **kwargs):
        self.kwargs = kwargs
        return self.response


def _client(response):
    msgs = _FakeMessages(response)
    return SimpleNamespace(beta=SimpleNamespace(messages=msgs)), msgs


def test_parse_returns_model_and_enables_fallback():
    parsed = MasterResume.model_validate(RESUME)
    client, msgs = _client(SimpleNamespace(stop_reason="end_turn", parsed_output=parsed, usage=None))
    out = parse(client, model="claude-sonnet-5-5", schema=MasterResume, system="s", content="c")
    assert out is parsed
    assert msgs.kwargs["fallbacks"] == "default"
    assert msgs.kwargs["output_format"] is MasterResume


@pytest.mark.parametrize("stop", ["refusal", "max_tokens"])
def test_parse_raises_on_bad_stop(stop):
    client, _ = _client(SimpleNamespace(stop_reason=stop, parsed_output=None, usage=None))
    with pytest.raises(LLMError):
        parse(client, model="m", schema=MasterResume, system="s", content="c")
