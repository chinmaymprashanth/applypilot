"""Paths and settings. Values come from environment variables (.env) with sane defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    root: Path = ROOT
    data_dir: Path = field(default_factory=lambda: ROOT / "data")
    output_dir: Path = field(default_factory=lambda: ROOT / "output")
    logs_dir: Path = field(default_factory=lambda: ROOT / "logs")
    templates_dir: Path = field(default_factory=lambda: ROOT / "templates")
    browser_profile_dir: Path = field(default_factory=lambda: ROOT / "browser_profile")

    model: str = "claude-sonnet-5-5"
    # The cheap, lenient first screen of new jobs (Sonnet then reads what passes).
    screen_model: str = "claude-haiku-5-5"
    # Web searches (tracing a job to the employer's site, company research for letters). Haiku reads the search
    # results at 1/20th of Sonnet's price; code still checks every link and fact against what the searches found.
    search_model: str = "claude-haiku-5-5"
    # Score through the Message Batches API: half price, takes minutes to an hour instead of seconds.
    use_batch: bool = True
    # Adzuna listings were mostly reposts or closed jobs and blocked from India, so it's off by default.
    use_adzuna: bool = False
    top_n: int = 10
    # Internship & part-time shortlist size (a separate list).
    top_n_intern: int = 5
    # Jobs older than this are never applied to.
    max_age_days: int = 7
    # Recency points are awarded only to jobs posted within this window.
    fresh_hours: int = 24
    # Most jobs per run that get traced to the employer's site / read in full before shortlisting.
    enrich_limit: int = 6
    # Jobs the cheap first screen (Haiku) reads per run, and how many of those Sonnet then reads carefully.
    screen_limit: int = 800
    read_limit: int = 120

    anthropic_api_key: str | None = None
    adzuna_app_id: str | None = None
    adzuna_app_key: str | None = None

    @property
    def tracker_path(self) -> Path:
        return self.root / "tracker.xlsx"

    @property
    def jobs_dir(self) -> Path:
        """Full job descriptions, one JSON file per job (too long for spreadsheet cells)."""
        return self.data_dir / "jobs"

    @property
    def profile_path(self) -> Path:
        return self.data_dir / "profile.yaml"

    @property
    def master_resume_path(self) -> Path:
        return self.data_dir / "master_resume.yaml"

    @property
    def answers_path(self) -> Path:
        return self.data_dir / "answers.yaml"


def load_settings(root: Path | None = None) -> Settings:
    base = root or ROOT
    load_dotenv(base / ".env")
    return Settings(
        root=base,
        data_dir=base / "data",
        output_dir=base / "output",
        logs_dir=base / "logs",
        templates_dir=base / "templates",
        browser_profile_dir=base / "browser_profile",
        model=os.getenv("JOBAGENT_MODEL", "claude-sonnet-5-5"),
        top_n=int(os.getenv("TOP_N", "10")),
        top_n_intern=int(os.getenv("TOP_N_INTERN", "5")),
        read_limit=int(os.getenv("READ_LIMIT", "120")),
        max_age_days=int(os.getenv("MAX_AGE_DAYS", "7")),
        fresh_hours=int(os.getenv("FRESH_HOURS", "24")),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY") or None,
        adzuna_app_id=os.getenv("ADZUNA_APP_ID") or None,
        adzuna_app_key=os.getenv("ADZUNA_APP_KEY") or None,
    )
