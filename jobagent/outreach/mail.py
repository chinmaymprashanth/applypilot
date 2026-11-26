"""Gmail for outreach: put drafts in your Drafts folder, and notice what you sent and who replied.

jobagent never sends. The only Gmail call here that writes anything is drafts().create; you open the draft in
Gmail, edit it if you like, and click Send yourself. What you sent is then read back (read-only) from your Sent
folder, and replies from the people you wrote to mark them "replied".
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import parseaddr

from .contacts import record_sent


def create_draft(svc, to: str, subject: str, body: str, thread_id: str | None = None,
                 in_reply_to: str | None = None) -> str:
    """Save one message in Gmail Drafts (never sends). Returns the draft id."""
    msg = EmailMessage()
    msg["To"] = to
    msg["Subject"] = subject
    if in_reply_to:  # a follow-up threads under your first message
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.set_content(body)
    payload: dict = {"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode()}
    if thread_id:
        payload["threadId"] = thread_id
    draft = svc.users().drafts().create(userId="me", body={"message": payload}).execute()
    return draft["id"]


AUTO_REPLY = ("automatic reply", "auto-reply", "autoreply", "out of office", "out of the office", "away from")


@dataclass
class Message:
    id: str
    thread_id: str
    sender: str
    to: str
    subject: str
    day: str
    message_id: str | None = None
    auto: bool = False


def _list(svc, query: str, limit: int = 200) -> list[Message]:
    out: list[Message] = []
    req = svc.users().messages().list(userId="me", q=query, maxResults=100)
    while req is not None and len(out) < limit:
        resp = req.execute()
        for m in resp.get("messages", []):
            msg = svc.users().messages().get(
                userId="me", id=m["id"], format="metadata",
                metadataHeaders=["From", "To", "Cc", "Subject", "Message-ID", "Auto-Submitted"]).execute()
            h = {x["name"].lower(): x["value"] for x in msg.get("payload", {}).get("headers", [])}
            day = datetime.fromtimestamp(int(msg["internalDate"]) / 1000, tz=timezone.utc).date().isoformat()
            subject = h.get("subject", "")
            auto = h.get("auto-submitted", "no").lower() != "no" or subject.lower().startswith(AUTO_REPLY)
            out.append(Message(m["id"], msg.get("threadId", ""), parseaddr(h.get("from", ""))[1].lower(),
                               f"{h.get('to', '')},{h.get('cc', '')}".lower(), subject, day, h.get("message-id"), auto))
        req = svc.users().messages().list_next(req, resp)
    return out


@dataclass
class ScanReport:
    sent: list[str] = field(default_factory=list)      # "Name (Company): first message" / "follow-up"
    replied: list[str] = field(default_factory=list)


def scan(svc, contacts: list[dict], days: int = 60) -> ScanReport:
    """Read-only: what you sent each email contact (from Sent) and whether they wrote back."""
    report = ScanReport()
    watch = [c for c in contacts if c.get("channel") == "email" and c.get("email")
             and c.get("status") in ("drafted", "sent", "no_reply")]
    by_email: dict[str, list[dict]] = {}
    for c in watch:
        by_email.setdefault(c["email"].lower(), []).append(c)
    emails = sorted(by_email)
    for i in range(0, len(emails), 20):  # Gmail queries stay short
        chunk = emails[i:i + 20]
        to_q = " OR ".join(f"to:{e}" for e in chunk)
        for m in sorted(_list(svc, f"in:sent newer_than:{days}d ({to_q})"), key=lambda m: m.day):
            for e in chunk:
                if e not in m.to:
                    continue
                for c in by_email[e]:
                    if c.get("created") and m.day < c["created"]:
                        continue  # an older conversation, before this outreach
                    first = not c.get("sent_dates")
                    if record_sent(c, m.day, m.id, m.thread_id):
                        if first:
                            c["first_message_id"] = m.message_id
                            c["subject"] = c.get("subject") or m.subject
                        report.sent.append(f"{c['name']} ({c['company']}): " + ("first message" if first else "follow-up"))
        from_q = " OR ".join(f"from:{e}" for e in chunk)
        for m in _list(svc, f"newer_than:{days}d -in:sent ({from_q})"):
            if m.auto:
                continue  # out-of-office isn't a reply
            for c in by_email.get(m.sender, []):
                if c.get("sent_dates") and m.day >= min(c["sent_dates"]) and c["status"] in ("sent", "no_reply"):
                    c["status"], c["replied_date"], c["follow_up_due"] = "replied", m.day, None
                    report.replied.append(f"{c['name']} ({c['company']}) replied on {m.day}")
    return report

