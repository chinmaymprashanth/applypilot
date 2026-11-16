"""Adzuna job search API (free tier): searches across all US employers.

Docs: https://developer.adzuna.com/docs/search
Free tier is rate limited, so each run makes a fixed, small number of calls.
Adzuna's job links are for people: they answer automated clients with 403, so we don't follow
them. Search traces the company to its own board by name instead (see discover.py).
"""

from __future__ import annotations

import logging
import re
import time

import httpx

from .common import iso_utc, looks_tech, make_job, work_mode

log = logging.getLogger(__name__)

SEARCH = "https://api.adzuna.com/v1/api/jobs/us/search/{page}"

# (search filters, max pages of 50). Newest first; jobagent runs daily, so the newest pages are
# what's new since yesterday. The IT category alone is ~29,000 US jobs a day and partly mislabelled,
# so we search inside it by title words, which also catches odd titles like "SWE II" or
# "Member of Technical Staff". Code and Claude still do the real filtering afterwards.
SEARCHES: list[tuple[dict, int]] = [
    ({"category": "it-jobs", "title_only": "junior"}, 2),
    ({"category": "it-jobs", "title_only": "entry"}, 1),
    ({"category": "it-jobs", "title_only": "associate"}, 2),
    ({"category": "graduate-jobs"}, 5),
    ({"category": "it-jobs", "title_only": "engineer"}, 6),
    ({"category": "it-jobs", "title_only": "developer"}, 4),
    ({"category": "it-jobs", "title_only": "analyst"}, 4),
    ({"category": "it-jobs", "title_only": "scientist"}, 2),
    # Salesforce administrator/developer roles (any category; often filed under IT or consultancy).
    ({"title_only": "salesforce"}, 3),
    # Analyst roles are often filed under finance or consulting, not IT.
    ({"what_phrase": "data analyst"}, 2),
    ({"what_phrase": "business analyst"}, 2),
    ({"what_phrase": "product analyst"}, 1),
]

def search(
    client: httpx.Client, app_id: str, app_key: str, *,
    max_days_old: int = 2, per_page: int = 50, pause: float = 2.5,
) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for filters, pages in SEARCHES:
        for page in range(1, pages + 1):
            params = {
                "app_id": app_id, "app_key": app_key, **filters,
                "max_days_old": max_days_old, "sort_by": "date",
                "results_per_page": per_page, "content-type": "application/json",
            }
            r = client.get(SEARCH.format(page=page), params=params)
            time.sleep(pause)  # stay well under the free-tier per-minute limit
            if r.status_code == 429:
                log.warning("adzuna rate limit hit; stopping search early")
                return out
            r.raise_for_status()
            results = r.json().get("results", [])
            for j in results:
                if j["id"] in seen:
                    continue
                seen.add(j["id"])
                loc = (j.get("location") or {}).get("display_name")
                title = j.get("title") or ""
                # The /us/ endpoint only returns US jobs, and its locations ("Hyde Park, Hamilton County")
                # often lack a state code, so no location check here.
                if not looks_tech(title):
                    continue
                out.append(make_job(
                    company=(j.get("company") or {}).get("display_name") or "Unknown",
                    title=re.sub(r"<[^>]+>", "", title),
                    location=loc,
                    work_mode=work_mode(title, j.get("description")),
                    posted_at=iso_utc(j.get("created")),
                    description=j.get("description") or "",  # a snippet; full text comes from the board
                    source="adzuna",
                    url=j["redirect_url"],
                    apply_url=j["redirect_url"],
                    ats_type="other",
                ))
            if len(results) < per_page:
                break
    return out

