"""SimplifyJobs New-Grad-Positions: a community-kept list of entry-level tech jobs, updated daily.

Two uses:
  - recent_jobs(): roles posted in the last few days, each with the company's own apply link.
  - boards(): every Greenhouse/Lever/Ashby board that has ever appeared in the list. These are
    companies known to hire new grads; polling their boards daily catches roles before any list does.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import httpx

from .common import detect_board, is_us, iso_utc, looks_tech, make_job, work_mode

LISTINGS = "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json"
# The same community's internship list. Terms that end before the MS starts (Feb 2027) or are past are skipped.
INTERNSHIPS = "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/.github/scripts/listings.json"
INTERN_TERMS = {"Spring 2027", "Summer 2027", "Fall 2027", "Spring 2028", "Summer 2028", "N/A"}
# Categories outside the candidate's background; everything else goes to the funnel.
SKIP_CATEGORIES = {"Hardware"}


def fetch(client: httpx.Client, cache: Path | None = None, url: str = LISTINGS) -> tuple[list[dict], bool]:
    """(listings, fresh). The file is large (~10 MB), so it gets a long timeout and a retry; if GitHub is still
    slow, yesterday's saved copy is used (fresh=False) rather than skipping the source."""
    last: Exception | None = None
    for _ in range(2):
        try:
            r = client.get(url, timeout=120)
            r.raise_for_status()
            data = r.json()
            if cache:
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(r.text)
            return data, True
        except (httpx.HTTPError, ValueError) as e:
            last = e
    if cache and cache.exists():
        return json.loads(cache.read_text()), False
    raise last


def recent_jobs(listings: list[dict], since: datetime, source: str = "newgrad_list",
                kind: str = "full_time") -> list[dict]:
    """Recent US tech roles from a list. kind is the job type they become (internship lists: "intern_pt")."""
    out = []
    for item in listings:
        if not (item.get("active") and item.get("is_visible", True)) or item.get("category") in SKIP_CATEGORIES:
            continue
        if kind == "intern_pt" and not set(item.get("terms") or ["N/A"]) & INTERN_TERMS:
            continue
        posted = iso_utc((item.get("date_posted") or 0) * 1000)  # epoch seconds
        if not posted or datetime.fromisoformat(posted) < since or not looks_tech(item.get("title") or ""):
            continue
        us = [loc for loc in item.get("locations") or [] if is_us(loc)]
        if item.get("locations") and not us:
            continue
        location = "; ".join(us[:3]) or None
        board = detect_board(item["url"])
        out.append(make_job(
            company=item["company_name"], title=item["title"], location=location,
            work_mode=work_mode(location), posted_at=posted, description="",
            source=source, url=item["url"], apply_url=item["url"], kind=kind,
            ats_type=board[0] if board else "other",
        ))
    return out


def boards(listings: list[dict]) -> dict[tuple[str, str], str]:
    """{(ats, token): company name} for every board in the list's history."""
    out: dict[tuple[str, str], str] = {}
    for item in listings:
        board = detect_board(item.get("url") or "")
        if board and board not in out:
            out[board] = item.get("company_name")
    return out
