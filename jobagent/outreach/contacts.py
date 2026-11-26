"""Contact records, the follow-up schedule and the weekly funnel.

A contact is one person you reach out to about one job. Its life:

    drafted -> sent -> replied -> call -> referral
                  \\-> no_reply  (no answer three weeks after the first message: stop)

"sent" is never set by jobagent sending anything: it comes from your Gmail Sent folder (you clicked Send in
Gmail) or from you saying so (LinkedIn, where only you click).

Follow-ups: the first is due 5 days after your first message (the 5-7 day window), an optional second at
day 14, and then it stops.
"""

from __future__ import annotations

import hashlib
from datetime import date, timedelta
from typing import Any

ROLES = ("hiring_manager", "engineer", "alum", "recruiter")
CHANNELS = ("email", "linkedin")
STATUSES = ("drafted", "sent", "replied", "call", "referral", "no_reply")
# Status order for the funnel: a contact at "call" has also replied.
_RANK = {s: i for i, s in enumerate(("drafted", "sent", "replied", "call", "referral"))}

FIRST_FOLLOW_UP_DAYS = 5   # due from day 5 (aim for day 5-7)
SECOND_FOLLOW_UP_DAYS = 14  # one optional second nudge
STOP_DAYS = 21             # no reply three weeks after the first message: stop
MAX_FOLLOW_UPS = 2


def contact_id(job_id: str, name: str, company: str) -> str:
    return hashlib.sha256(f"{job_id}|{company.lower()}|{name.lower().strip()}".encode()).hexdigest()[:14]


def new_contact(job: dict, name: str, role: str, channel: str, *, title: str | None = None,
                source_url: str | None = None, evidence: str | None = None, today: date | None = None) -> dict[str, Any]:
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")
    if channel not in CHANNELS:
        raise ValueError(f"channel must be one of {CHANNELS}")
    return {
        "id": contact_id(job["id"], name, job["company"]), "job_id": job["id"], "company": job["company"],
        "job_title": job["title"], "name": name.strip(), "title": title, "role": role, "channel": channel,
        "source_url": source_url, "evidence": evidence, "email": None, "email_status": None,
        "linkedin_url": None, "status": "drafted", "created": (today or date.today()).isoformat(),
        "sent_dates": [], "sent_ids": [], "thread_id": None, "subject": None, "draft_ids": [],
        "follow_ups_drafted": 0, "follow_up_due": None, "replied_date": None, "call_date": None,
        "referral_date": None, "notes": None,
    }


def _d(value) -> date:
    return date.fromisoformat(str(value)[:10])


def first_sent(c: dict) -> date | None:
    dates = c.get("sent_dates") or []
    return _d(min(dates)) if dates else None


def next_follow_up(c: dict) -> tuple[int, date] | None:
    """(which follow-up, due date) for a contact still waiting on a reply; None when nothing more is due."""
    start = first_sent(c)
    if c.get("status") != "sent" or start is None:
        return None
    sent_follow_ups = len(c.get("sent_dates") or []) - 1
    if sent_follow_ups >= MAX_FOLLOW_UPS:
        return None
    days = FIRST_FOLLOW_UP_DAYS if sent_follow_ups == 0 else SECOND_FOLLOW_UP_DAYS
    return sent_follow_ups + 1, start + timedelta(days=days)


def refresh(c: dict, today: date | None = None) -> None:
    """Recompute the follow-up date, and stop (no_reply) three weeks after the first message."""
    today = today or date.today()
    start = first_sent(c)
    if c.get("status") == "sent" and start and today >= start + timedelta(days=STOP_DAYS):
        c["status"] = "no_reply"
    nxt = next_follow_up(c)
    c["follow_up_due"] = nxt[1].isoformat() if nxt else None


def due_follow_ups(contacts: list[dict], today: date | None = None) -> list[tuple[dict, int]]:
    """Contacts whose next follow-up is due today or earlier, with which follow-up it is (1 or 2)."""
    today = today or date.today()
    out = []
    for c in contacts:
        refresh(c, today)
        nxt = next_follow_up(c)
        if nxt and nxt[1] <= today:
            out.append((c, nxt[0]))
    return sorted(out, key=lambda x: x[0]["follow_up_due"])


def record_sent(c: dict, day: date | str, message_id: str | None = None, thread_id: str | None = None) -> bool:
    """Note one message you sent to this person (first message or a follow-up). Returns False if already known."""
    day = str(day)[:10]
    if message_id and message_id in (c.get("sent_ids") or []):
        return False
    if not message_id and day in (c.get("sent_dates") or []):
        return False
    c.setdefault("sent_dates", []).append(day)
    c["sent_dates"].sort()
    if message_id:
        c.setdefault("sent_ids", []).append(message_id)
    if thread_id and not c.get("thread_id"):
        c["thread_id"] = thread_id
    if c.get("status") == "drafted":
        c["status"] = "sent"
    refresh(c, _d(day))
    return True


def set_status(c: dict, status: str, today: date | None = None) -> None:
    """Move a contact on by hand (e.g. after a LinkedIn reply or a call). Dates are filled in for the funnel."""
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    day = (today or date.today()).isoformat()
    if status == "sent" and not c.get("sent_dates"):
        c["sent_dates"] = [day]
    for later, field in (("replied", "replied_date"), ("call", "call_date"), ("referral", "referral_date")):
        if _RANK.get(status, -1) >= _RANK[later] and not c.get(field):
            c[field] = day
    c["status"] = status
    refresh(c, today)


# ---- the weekly funnel ----------------------------------------------------------------------------------------


def funnel(contacts: list[dict], jobs: list[dict], today: date | None = None, days: int = 7) -> dict[str, int]:
    """Counts for the last `days` days: first messages sent, replies, calls, referrals, interviews."""
    today = today or date.today()
    since = today - timedelta(days=days)
    in_window = lambda v: bool(v) and since < _d(v) <= today  # noqa: E731
    return {
        "messages": sum(in_window(first_sent(c) and first_sent(c).isoformat()) for c in contacts),
        "replies": sum(in_window(c.get("replied_date")) for c in contacts),
        "calls": sum(in_window(c.get("call_date")) for c in contacts),
        "referrals": sum(in_window(c.get("referral_date")) for c in contacts),
        "interviews": sum(in_window(j.get("interview_date")) for j in jobs),
    }


def funnel_line(contacts: list[dict], jobs: list[dict], today: date | None = None) -> str:
    week = funnel(contacts, jobs, today)
    sent = [c for c in contacts if c.get("sent_dates")]
    replied = [c for c in sent if c.get("replied_date")]
    line = (f"Outreach this week: {week['messages']} messages, {week['replies']} replies, {week['calls']} calls, "
            f"{week['referrals']} referrals, {week['interviews']} interviews")
    if sent:
        line += f" (all time: {len(sent)} messages, {len(replied)} replies, {round(100 * len(replied) / len(sent))}%)"
    return line


def funnel_advice(contacts: list[dict]) -> str | None:
    """The plan's check: replies under ~10% after a fair number of messages means the targeting or opener needs work."""
    sent = [c for c in contacts if c.get("sent_dates")]
    if len(sent) < 20:
        return None
    rate = sum(bool(c.get("replied_date")) for c in sent) / len(sent)
    if rate < 0.10:
        return (f"Only {round(100 * rate)}% of your messages got a reply. Try warmer contacts (UMass alumni, "
                "ex-IBM) and a more specific first sentence.")
    return None
