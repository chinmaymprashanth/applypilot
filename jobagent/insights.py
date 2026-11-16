"""Learn from results: which kinds of applications lead to interviews.

Outcomes come from tracker.xlsx, which the inbox step updates from employer emails (and you can edit).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from .search.common import ats_of
from .tracker import Tracker

SENT = ("applied", "interview", "rejected", "offer")
# With fewer applications than this, differences are noise, so no advice is given.
MIN_FOR_ADVICE = 10
NO_REPLY_DAYS = 14


def outcome(r: dict, today: date) -> str:
    if r["status"] in ("interview", "offer"):
        return "interview"
    if r["status"] == "rejected":
        return "rejected"
    applied = r.get("applied_date")
    if applied and date.fromisoformat(str(applied)[:10]) <= today - timedelta(days=NO_REPLY_DAYS):
        return "no reply"
    return "waiting"


def score_band(score) -> str:
    if score is None:
        return "unscored"
    return "85+" if score >= 85 else "75-85" if score >= 75 else "under 75"


def insights(tracker: Tracker, today: date | None = None) -> list[str]:
    today = today or date.today()
    apps = [r for r in tracker.jobs.values() if r.get("status") in SENT]
    if not apps:
        return ["No applications yet."]
    totals = defaultdict(int)
    for r in apps:
        totals[outcome(r, today)] += 1
    lines = [f"{len(apps)} applications: {totals['interview']} interviews/offers, {totals['rejected']} rejections, "
             f"{totals['no reply']} no reply after {NO_REPLY_DAYS} days, {totals['waiting']} still waiting."]
    best = []
    for label, key in (("Track", lambda r: r.get("track") or "unknown"), ("Source", lambda r: r.get("source")),
                       ("Hiring site", lambda r: ats_of(r.get("apply_url"))),
                       ("Score", lambda r: score_band(r.get("score")))):
        groups: dict[str, list[dict]] = defaultdict(list)
        for r in apps:
            groups[str(key(r))].append(r)
        parts = []
        for name, rows in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            decided = [r for r in rows if outcome(r, today) != "waiting"]
            wins = sum(outcome(r, today) == "interview" for r in rows)
            rate = f"{round(100 * wins / len(decided))}%" if decided else "-"
            parts.append(f"{name} {len(rows)} sent, {wins} interviews ({rate} of answered)")
            if len(decided) >= 3:
                best.append((wins / len(decided), label, name, len(decided)))
        lines.append(f"{label}: " + "; ".join(parts))
    if len(apps) >= MIN_FOR_ADVICE and best:
        rate, label, name, n = max(best)
        if rate > 0:
            lines.append(f"Best so far: {label.lower()} '{name}' ({round(100 * rate)}% interviews of {n} answered). "
                         "Consider favouring it.")
    elif len(apps) < MIN_FOR_ADVICE:
        lines.append(f"Advice starts after {MIN_FOR_ADVICE} applications; patterns before that are mostly chance.")
    return lines
