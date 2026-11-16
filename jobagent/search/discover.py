"""Find a company's Greenhouse/Lever/Ashby board from its name alone.

Used for jobs seen only in LinkedIn/Indeed alerts: we guess the board token from the
company name and accept a guess only if that board lists a job with the same title.
Misses are cached so each company is tried at most once a month.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx

from .ats import FETCHERS, GUESSABLE, BoardNotFound

SUFFIXES = r"\b(inc|incorporated|llc|ltd|limited|corp|corporation|co|company|technologies|technology|labs|hq)\b"


def slug_guesses(company: str) -> list[str]:
    base = re.sub(r"[^\w\s-]", "", company.lower())
    short = re.sub(SUFFIXES, "", base).strip()
    out = []
    for name in (short, base):
        words = name.split()
        if not words:
            continue
        for s in ("".join(words), "-".join(words)):
            if s not in out:
                out.append(s)
    return out[:3]


def _norm_title(t: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", t.lower()).split())


class BoardFinder:
    def __init__(self, client: httpx.Client, cache_path: Path, retry_days: int = 30,
                 since: datetime | None = None):
        self.client = client
        self.since = since  # only recent jobs matter, and older ones aren't worth downloading
        self.cache_path = cache_path
        self.retry_days = retry_days
        self.cache: dict[str, dict] = json.loads(cache_path.read_text()) if cache_path.exists() else {}
        self.boards: dict[tuple[str, str], list[dict]] = {}  # fetched this run

    def save(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(self.cache, indent=1))

    def _jobs(self, ats: str, token: str) -> list[dict] | None:
        key = (ats, token)
        if key not in self.boards:
            try:
                self.boards[key] = FETCHERS[ats](self.client, token, since=self.since)
            except (BoardNotFound, httpx.HTTPError, ValueError):
                self.boards[key] = None
        return self.boards[key]

    def find(self, company: str, title: str) -> tuple[str, str, dict] | None:
        """(ats, token, matching job) or None."""
        name = company.strip().lower()
        hit = self.cache.get(name)
        if hit and hit.get("ats"):
            boards = [(hit["ats"], hit["token"])]
        elif hit and date.fromisoformat(hit["checked"]) > date.today() - timedelta(days=self.retry_days):
            return None
        else:
            boards = [(ats, s) for s in slug_guesses(company) for ats in GUESSABLE]
        want = _norm_title(title)
        for ats, token in boards:
            for job in self._jobs(ats, token) or []:
                if _norm_title(job["title"]) == want:
                    self.cache[name] = {"ats": ats, "token": token, "checked": date.today().isoformat()}
                    return ats, token, job
            if self.boards.get((ats, token)) is not None and hit and hit.get("ats"):
                return None  # known board, this title just isn't on it
        self.cache[name] = {"ats": None, "checked": date.today().isoformat()}
        return None
