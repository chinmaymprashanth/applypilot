"""Hacker News "Ask HN: Who is hiring?": a monthly thread where startups post openings.

We don't read the posts as jobs. We collect the Greenhouse/Lever/Ashby boards they link to,
and the daily board poll picks up those companies' openings with full descriptions.
Uses the official HN search API (Algolia); no key needed.
"""

from __future__ import annotations

import html
import re

import httpx

from .common import detect_board, html_to_text

SEARCH = "https://hn.algolia.com/api/v1/search_by_date"
ITEM = "https://hn.algolia.com/api/v1/items/{id}"
URL = re.compile(r"https?://[^\s<>\"']+")


def latest_thread(client: httpx.Client) -> dict | None:
    r = client.get(SEARCH, params={"tags": "story,author_whoishiring", "hitsPerPage": 10})
    r.raise_for_status()
    return next((h for h in r.json()["hits"] if h["title"].startswith("Ask HN: Who is hiring?")), None)


def boards(client: httpx.Client) -> dict[tuple[str, str], str]:
    """{(ats, token): company name} linked from the latest thread's top-level posts."""
    thread = latest_thread(client)
    if not thread:
        return {}
    r = client.get(ITEM.format(id=thread["objectID"]))
    r.raise_for_status()
    out: dict[tuple[str, str], str] = {}
    for post in r.json().get("children") or []:
        text = html.unescape(post.get("text") or "")
        # Posts start "Company | Role | Location | ..."
        company = html_to_text(text).split("|")[0].strip()[:60] or None
        for url in URL.findall(text):
            board = detect_board(url)
            if board and board not in out:
                out[board] = company
    return out
