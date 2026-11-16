"""Candidate profile and master resume: the only source of facts the tool may use."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator


class Profile(BaseModel):
    name: str
    location: str
    phone: str
    email: str
    github: str
    linkedin: str
    website: str | None = None
    us_citizen: bool = True
    needs_sponsorship: bool = False
    work_modes: list[str] = Field(default_factory=lambda: ["remote", "hybrid", "onsite"])
    open_to_relocate: bool = True
    available_from: str = "immediately"
    # Location shown on internship and part-time applications (e.g. where you'll study).
    intern_location: str | None = None
    fixed_dates: list["FixedDates"] = Field(default_factory=list)


class FixedDates(BaseModel):
    """Role dates that override what the resume PDF says (matched by company name)."""

    company: str
    start: str
    end: str | None = None


TRACKS = ("software", "ai_ml", "data_support", "salesforce")


class Selectable(BaseModel):
    """An entry the tailoring step may pick for a resume or cover letter.

    verified=False keeps it out of every application until you confirm it.
    hide_for lists resume tracks (any of TRACKS) it is left off.
    facts are extra true details, beyond the resume bullets, the tailor may draw on.
    track_facts are true details used ONLY on that track's resumes (e.g. salesforce: [...]).
    """

    verified: bool = True
    hide_for: list[str] = Field(default_factory=list)
    facts: list[str] = Field(default_factory=list)
    track_facts: dict[str, list[str]] = Field(default_factory=dict)

    @field_validator("track_facts")
    @classmethod
    def _known_fact_tracks(cls, v: dict[str, list[str]]) -> dict[str, list[str]]:
        unknown = set(v) - set(TRACKS)
        if unknown:
            raise ValueError(f"unknown track(s) {sorted(unknown)}; use {list(TRACKS)}")
        return v

    @field_validator("hide_for")
    @classmethod
    def _known_tracks(cls, v: list[str]) -> list[str]:
        unknown = set(v) - set(TRACKS)
        if unknown:
            raise ValueError(f"unknown track(s) {sorted(unknown)}; use {list(TRACKS)}")
        return v

    def shown_for(self, track: str) -> bool:
        return self.verified and track not in self.hide_for


class Role(Selectable):
    company: str
    title: str
    location: str | None = None
    start: str = Field(description="YYYY-MM")
    end: str | None = Field(default=None, description="YYYY-MM, or null if current")
    bullets: list[str]


class Project(Selectable):
    name: str
    link: str | None = None
    tech: list[str] = Field(default_factory=list)
    bullets: list[str]


class Achievement(Selectable):
    """Leadership, competitions, awards, speaking: anything that isn't a job or project."""

    title: str
    category: str
    organization: str | None = None
    date: str | None = None
    # What still needs checking before verified can be set to true.
    to_confirm: str | None = None


class Education(BaseModel):
    school: str
    degree: str
    location: str | None = None
    start: str | None = None
    end: str | None = None
    gpa: str | None = None
    notes: list[str] = Field(default_factory=list)
    # Job types this entry is limited to, e.g. ["intern_pt"] for a degree that hasn't started; empty = every resume.
    only_for: list[str] = Field(default_factory=list)
    expected: bool = False  # the end date is expected, not completed

    def shown_for(self, kind: str | None) -> bool:
        return not self.only_for or kind in self.only_for


class SkillGroup(BaseModel):
    category: str
    items: list[str]
    only_for: list[str] = Field(default_factory=list)  # tracks this group is limited to; empty = every resume

    def shown_for(self, track: str | None) -> bool:
        return not self.only_for or track is None or track in self.only_for


class MasterResume(BaseModel):
    summary: str | None = None
    experience: list[Role]
    projects: list[Project] = Field(default_factory=list)
    education: list[Education]
    skills: list[SkillGroup]
    certifications: list[str] = Field(default_factory=list)
    achievements: list[Achievement] = Field(default_factory=list)
    spoken_languages: list[str] = Field(default_factory=list)

    def all_skills(self, track: str | None = None) -> set[str]:
        """Skills that may appear on a resume for this track (all of them when track is None)."""
        return {s.strip().lower() for g in self.skills if g.shown_for(track) for s in g.items}

    def certifications_for(self, track: str) -> list[Achievement]:
        return [a for a in self.achievements if a.category.lower() == "certification" and a.shown_for(track)]

    def apply_fixed_dates(self, fixed: list[FixedDates]) -> list[str]:
        """Overwrite role dates from profile.yaml. Returns the companies that had no matching role."""
        missing = []
        for f in fixed:
            roles = [r for r in self.experience if f.company.lower() in r.company.lower()]
            if not roles:
                missing.append(f.company)
            for r in roles:
                r.start, r.end = f.start, f.end
        return missing


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"{path} not found")
    with path.open() as f:
        return yaml.safe_load(f) or {}


def load_profile(path: Path) -> Profile:
    return Profile.model_validate(_read_yaml(path))


def load_master_resume(path: Path) -> MasterResume:
    return MasterResume.model_validate(_read_yaml(path))


def save_master_resume(resume: MasterResume, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        yaml.safe_dump(resume.model_dump(), f, sort_keys=False, allow_unicode=True, width=100)


Profile.model_rebuild()
