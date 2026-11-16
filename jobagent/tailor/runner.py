"""The tailor stage: for each shortlisted job, write and check a resume and cover letter, then save PDFs.

Output goes to output/YYYY-MM-DD/{company}_{role}/ and the job's status becomes "prepared".
"""

from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable

from ..profile import MasterResume, Profile
from ..tracker import Tracker
from .render import Renderer, resume_data
from .writer import Generate, Patcher, keyword_coverage, tailor

log = logging.getLogger(__name__)


@dataclass
class TailorReport:
    prepared: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def slug(text: str, n: int = 40) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")[:n]


def short_slug(text: str, n: int) -> str:
    """Like slug(), but cut at a whole word: "Forward_Deployed_Engineer", never "Forward_Deployed_Engin"."""
    out = ""
    for word in slug(text, 200).split("_"):
        if out and len(out) + 1 + len(word) > n:
            break
        out = f"{out}_{word}" if out else word[:n]
    return out


@dataclass
class Docs:
    resume: str
    letter_pdf: str
    letter_txt: str


def doc_names(profile: Profile, job: dict) -> Docs:
    """File names that say whose document it is and which job it was made for.
    Recruiters see the name too, e.g. Alex_Jordan_Rivera_Resume_SharkNinja_Forward_Deployed_Engineer.pdf"""
    who = slug(profile.name, 60)
    tag = f"{short_slug(job['company'], 25)}_{short_slug(job['title'], 40)}"
    return Docs(f"{who}_Resume_{tag}.pdf", f"{who}_Cover_Letter_{tag}.pdf", f"{who}_Cover_Letter_{tag}.txt")


class Preparer:
    """Writes one job's resume and cover letter. write() is the slow Claude part and is safe to run in
    threads; save() renders the PDFs and records them, on the thread that owns the renderer."""

    def __init__(self, tracker: Tracker, master: MasterResume, profile: Profile, generate: Generate,
                 output_dir: Path, templates_dir: Path, research: Callable[[str, str], list[str]] | None = None,
                 patch: Patcher | None = None):
        self.tracker, self.master, self.profile, self.generate = tracker, master, profile, generate
        self.output_dir, self.templates_dir, self.research, self.patch = output_dir, templates_dir, research, patch

    def write(self, job: dict):
        facts = self.research(job["company"], job["title"]) if self.research else []
        t = tailor(job, self.tracker.description(job["id"]), self.master, self.generate, name=self.profile.name,
                   research=facts, patch=self.patch, kind=job.get("kind") or "full_time")
        return t, facts

    def save(self, job: dict, written, renderer: Renderer, day: date | None = None) -> dict:
        """Render and save; marks the job "prepared". Returns what the report shows about it."""
        t, facts = written
        tracker, master, profile = self.tracker, self.master, self.profile
        folder = self.output_dir / (day or date.today()).isoformat() / f"{slug(job['company'], 30)}_{slug(job['title'])}"
        folder.mkdir(parents=True, exist_ok=True)
        kind = job.get("kind") or "full_time"
        pdf, text, trimmed, pages = renderer.resume(resume_data(t, master, profile, kind))
        hit, miss = keyword_coverage(t, text)
        docs = doc_names(profile, job)
        (folder / docs.resume).write_bytes(pdf)
        (folder / docs.letter_txt).write_text(t.cover_letter)
        (folder / docs.letter_pdf).write_bytes(renderer.cover_letter(t.cover_letter, profile, kind))
        detail = json.loads((tracker.jobs_dir / f"{job['id']}.json").read_text())
        (folder / "job.json").write_text(json.dumps({
            "job": job, "description": detail.get("description"), "assessment": detail.get("assessment"),
            "tailored": t.model_dump(), "keywords_on_resume": hit, "keywords_missing": miss,
            "keywords_you_dont_have": t.unsupported_keywords, "company_research": facts,
            "bullets_trimmed_to_fit": trimmed,
        }, indent=2, ensure_ascii=False, default=str))
        tracker.save_detail(job["id"], output_dir=str(folder))
        coverage = round(100 * len(hit) / max(1, len(t.job_keywords)))
        unsupported = {k.lower() for k in t.unsupported_keywords}
        supported = [k for k in t.job_keywords if k.lower() not in unsupported]
        covered = round(100 * sum(k in hit for k in supported) / max(1, len(supported)))
        tracker.update(job["id"], status="prepared", resume_version=f"{folder.parent.name}/{folder.name}",
                       track=t.track, keyword_match=f"{coverage}% ({covered}% of yours)")
        log.info("prepared %s - %s (%s, keywords %s%%)", job["company"], job["title"], t.track, coverage)
        return {**job, "folder": folder, "track": t.track, "coverage": coverage, "covered": covered,
                "missing": [k for k in miss if k.lower() not in unsupported], "pages": pages,
                "unsupported": t.unsupported_keywords, "hook": bool(facts and t.company_hook)}


def run_tailor(
    tracker: Tracker, master: MasterResume, profile: Profile, generate: Generate, renderer: Renderer,
    output_dir: Path, *, limit: int | None = None, day: date | None = None, workers: int = 4,
    research: Callable[[str, str], list[str]] | None = None, jobs: list[dict] | None = None,
    patch: Patcher | None = None,
) -> TailorReport:
    """Tailor shortlisted jobs (or the given ones) in one go. apply does this per job, after you choose."""
    report = TailorReport()
    prep = Preparer(tracker, master, profile, generate, output_dir, renderer.templates_dir, research, patch)
    jobs = jobs if jobs is not None else tracker.with_status("shortlisted")[:limit]

    def write(job: dict):
        try:
            return job, prep.write(job), None
        except Exception as e:  # noqa: BLE001 - one bad job shouldn't stop the run
            return job, None, e

    # Claude writes several jobs at once (the slow part); PDFs are then rendered one at a time.
    with ThreadPoolExecutor(max_workers=workers) as pool:
        written = list(pool.map(write, jobs))
    for job, result, err in written:
        name = f"{job['company']} - {job['title']}"
        if err:
            report.errors.append(f"{name}: {err}")
            continue
        info = prep.save(job, result, renderer, day)
        if info["pages"] > 1:
            report.errors.append(f"{name}: resume is {info['pages']} pages even after trimming; check {info['folder']}")
        report.prepared.append(info)
    return report


def rerender(folder: Path, master: MasterResume, profile: Profile, renderer: Renderer) -> int:
    """Rebuild a job's PDFs from its saved job.json (after a template change; no Claude call). Returns pages."""
    from .writer import Tailored

    saved = json.loads((folder / "job.json").read_text())
    t = Tailored.model_validate(saved["tailored"])
    kind = saved["job"].get("kind") or "full_time"
    pdf, _, _, pages = renderer.resume(resume_data(t, master, profile, kind))
    docs = doc_names(profile, saved["job"])
    (folder / docs.resume).write_bytes(pdf)
    (folder / docs.letter_pdf).write_bytes(renderer.cover_letter(t.cover_letter, profile, kind))
    (folder / docs.letter_txt).write_text(t.cover_letter)
    return pages
