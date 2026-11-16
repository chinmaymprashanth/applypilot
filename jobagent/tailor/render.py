"""Turn a validated Tailored result into resume and cover-letter PDFs (Playwright print-to-PDF)."""

from __future__ import annotations

import base64
import io
import re
from datetime import date
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup
from pypdf import PdfReader

from ..profile import MasterResume, Profile
from .writer import Tailored, entries

# The three base templates differ in section order.
SECTIONS = {
    "software": ["experience", "projects", "skills", "education"],
    "ai_ml": ["experience", "projects", "skills", "education", "leadership"],
    "data_support": ["experience", "skills", "projects", "education", "leadership"],
    "salesforce": ["experience", "certifications", "skills", "projects", "education"],
}
MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()


def month(value: str | None) -> str:
    if not value:
        return "Present"
    y, _, m = str(value).partition("-")
    return f"{MONTHS[int(m) - 1]} {y}" if m else y


def resume_data(t: Tailored, master: MasterResume, profile: Profile, kind: str = "full_time") -> dict:
    """Everything the template shows. Names, titles and dates come from the master file, never from Claude.
    kind "intern_pt" (internships, part-time): student details first and the study location."""
    allowed = entries(master, t.track)
    experience = []
    for te in t.experience:
        r = allowed[te.ref].obj
        experience.append({"company": r.company, "title": r.title, "location": r.location,
                           "dates": f"{month(r.start)} – {month(r.end)}", "bullets": [b.text for b in te.bullets]})
    projects = []
    for te in t.projects:
        pr = allowed[te.ref].obj
        projects.append({"name": pr.name.replace("Founder — ", ""), "link": pr.link, "tech": pr.tech,
                         "bullets": [b.text for b in te.bullets]})
    education = [{"degree": ed.degree, "school": ed.school, "location": ed.location, "gpa": ed.gpa,
                  "dates": f"{month(ed.start)} – {month(ed.end)}" + (" (expected)" if ed.expected else ""),
                  "notes": ed.notes[:1]} for ed in master.education if ed.shown_for(kind)]
    sections = SECTIONS[t.track]
    if kind == "intern_pt":
        sections = ["education"] + [x for x in sections if x != "education"]
        if profile.intern_location:
            profile = profile.model_copy(update={"location": profile.intern_location})
    achievements = [a.title + (f": {a.facts[0]}" if a.facts else "")
                    for a in master.achievements if a.shown_for(t.track) and a.category.lower() != "certification"]
    certifications = [f"{a.title}, {a.organization} ({a.date})" if a.organization else f"{a.title} ({a.date})"
                      for a in master.certifications_for(t.track)]
    return {"p": profile, "summary": t.summary, "sections": sections, "experience": experience,
            "projects": projects, "skills": [s.model_dump() for s in t.skills], "education": education,
            "achievements": achievements, "certifications": certifications}


def trim_once(data: dict) -> bool:
    """Drop the least relevant content to save space. False when nothing sensible is left to drop."""
    if data["achievements"]:
        data["achievements"] = []
        return True
    projects = data["projects"]
    if projects:
        last = projects[-1]
        if len(last["bullets"]) > 1:
            last["bullets"].pop()
            return True
        if len(projects) > 2:
            projects.pop()
            return True
    for e in reversed(data["experience"]):  # oldest roles first
        if len(e["bullets"]) > 2:
            e["bullets"].pop()
            return True
    for ed in data["education"]:
        if ed["notes"]:
            ed["notes"] = []
            return True
    return False


def font_faces(fonts_dir: Path) -> Markup:
    """@font-face rules with the fonts inlined, so the page needs no file or network access.
    Files are named like source-sans-3-latin-700-normal.woff2."""
    rules = []
    for f in sorted(fonts_dir.glob("*.woff2")):
        m = re.search(r"latin-(\d+)-(normal|italic)", f.name)
        if not m:
            continue
        data = base64.b64encode(f.read_bytes()).decode()
        rules.append(f'@font-face {{ font-family: "Source Sans 3"; font-weight: {m.group(1)}; '
                     f'font-style: {m.group(2)}; src: url(data:font/woff2;base64,{data}) format("woff2"); }}')
    return Markup("\n".join(rules))


class Renderer:
    """Keeps one headless Chromium open while rendering many PDFs."""

    def __init__(self, templates_dir: Path, pw=None):
        """pw: an already-running Playwright to reuse (one Playwright per thread is allowed)."""
        self.templates_dir = templates_dir
        self._shared_pw = pw
        self.env = Environment(loader=FileSystemLoader(templates_dir), autoescape=select_autoescape(["html", "j2"]))
        self.fonts_css = font_faces(templates_dir / "fonts")

    def __enter__(self):
        from playwright.sync_api import sync_playwright

        self._pw = self._shared_pw or sync_playwright().start()
        self._browser = self._pw.chromium.launch()
        self._page = self._browser.new_page()
        return self

    def __exit__(self, *exc):
        self._browser.close()
        if not self._shared_pw:
            self._pw.stop()

    def pdf(self, template: str, data: dict) -> bytes:
        self._page.set_content(self.env.get_template(template).render(**data, fonts_css=self.fonts_css))
        self._page.evaluate("document.fonts.ready")
        return self._page.pdf(prefer_css_page_size=True, print_background=True)

    def resume(self, data: dict) -> tuple[bytes, str, int, int]:
        """(pdf bytes, plain text, items trimmed, pages). Trims until it fits one page."""
        trimmed = 0
        while True:
            pdf = self.pdf("resume.html.j2", data)
            reader = PdfReader(io.BytesIO(pdf))
            if len(reader.pages) == 1 or not trim_once(data):
                text = "\n".join(page.extract_text() or "" for page in reader.pages)
                return pdf, text, trimmed, len(reader.pages)
            trimmed += 1

    def cover_letter(self, text: str, profile: Profile, kind: str = "full_time") -> bytes:
        if kind == "intern_pt" and profile.intern_location:
            profile = profile.model_copy(update={"location": profile.intern_location})
        paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
        return self.pdf("cover_letter.html.j2", {"p": profile, "paragraphs": paragraphs,
                                                 "today": date.today().strftime("%B %-d, %Y")})
