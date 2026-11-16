"""Convert the resume PDF in data/ into data/master_resume.yaml."""

from __future__ import annotations

import base64
from pathlib import Path

import anthropic
from pypdf import PdfReader

from .llm import parse
from .profile import MasterResume

SYSTEM = """You convert a resume into structured data.
Copy every employer, title, location, date, bullet, project, degree, GPA, skill and certification exactly as written.
Do not rephrase, merge, summarize, or add anything that is not in the resume.
Dates use YYYY-MM. Use null for an end date only if the resume says the role is current.
Group skills under the same category headings the resume uses."""


def find_resume_pdf(data_dir: Path) -> Path:
    pdfs = sorted(data_dir.glob("*.pdf"))
    if not pdfs:
        raise FileNotFoundError(f"No resume PDF found in {data_dir}/. Put your resume PDF there.")
    if len(pdfs) > 1:
        names = ", ".join(p.name for p in pdfs)
        raise ValueError(f"More than one PDF in {data_dir}/ ({names}); pass --pdf to pick one.")
    return pdfs[0]


def pdf_text(pdf: Path) -> str:
    return "\n".join(page.extract_text() or "" for page in PdfReader(pdf).pages)


def convert(client: anthropic.Anthropic, model: str, pdf: Path) -> MasterResume:
    data = base64.standard_b64encode(pdf.read_bytes()).decode()
    content = [
        {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": data}},
        {"type": "text", "text": "Convert this resume into the schema."},
    ]
    return parse(client, model=model, schema=MasterResume, system=SYSTEM, content=content, effort="high")


def unmatched_bullets(resume: MasterResume, source_text: str) -> list[str]:
    """Bullets whose words don't appear in the PDF text: a sign the model reworded something."""
    haystack = " ".join(source_text.lower().split())
    out = []
    bullets = [b for r in resume.experience for b in r.bullets] + [b for p in resume.projects for b in p.bullets]
    for b in bullets:
        probe = " ".join(b.lower().split())[:40]
        if probe and probe not in haystack:
            out.append(b)
    return out
