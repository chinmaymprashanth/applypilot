"""The outreach assistant: after you mark a job applied, two people to contact and a draft for each.

Nothing is ever sent. Emails go to Gmail Drafts (or your clipboard if drafts aren't enabled); LinkedIn notes go
to your clipboard while the LinkedIn search opens in your own Chrome. You click Send or Connect.

The daily pass (in `prepare`, so also in the AWS run) reads your Sent folder and replies, drafts the follow-ups
that are due, and adds the weekly funnel to the summary.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable

from ..profile import MasterResume, Profile
from .contacts import (due_follow_ups, funnel_advice, funnel_line, new_contact, record_sent)
from .draft import DraftRejected, OutreachWriter, job_context, kind_for
from .find import (People, Person, guess_emails, hunter_verify, linkedin_search_url, load_patterns, pattern_of,
                   save_pattern, warm_searches)
from .mail import create_draft, scan

log = logging.getLogger(__name__)

ROLE_KEYS = {"h": "hiring_manager", "e": "engineer", "a": "alum", "r": "recruiter"}
ROLE_NAMES = {"hiring_manager": "hiring manager", "engineer": "engineer", "alum": "alum", "recruiter": "recruiter"}


class Assistant:
    def __init__(self, tracker, writer: OutreachWriter, finder: Callable[[dict, str], People], profile: Profile,
                 master: MasterResume, *, drafts: Callable[[], object] | None = None, hunter_key: str | None = None,
                 patterns_path: Path | None = None, ask: Callable[[str], str] = input,
                 echo: Callable[[str], None] = print, open_url: Callable[[str], None] | None = None,
                 copy: Callable[[str], None] | None = None, http=None, today: date | None = None):
        self.tracker, self.writer, self.finder, self.profile, self.master = tracker, writer, finder, profile, master
        self.drafts, self.hunter_key, self.http = drafts, hunter_key, http
        self.patterns_path = patterns_path or tracker.jobs_dir.parent / "email_patterns.json"
        self.ask, self.echo = ask, echo
        self.open_url = open_url or (lambda url: None)
        self.copy = copy or (lambda text: None)
        self.today = today
        self._svc = None

    # ---- the step after "Submitted? y" ------------------------------------------------------------------------

    def offer(self, job: dict) -> None:
        c = self.ask("  Find 2 people to contact about this job (drafts only; you click Send)? [y]es / [n]o: ")
        if c.strip().lower()[:1] == "y":
            try:
                self.run_for_job(job)
            except Exception as e:  # noqa: BLE001 - outreach never costs you the application flow
                log.warning("outreach for %s: %s", job.get("company"), e)
                self.echo(f"  ! Outreach stopped ({str(e).splitlines()[0][:120]}). Your application is saved.")

    def run_for_job(self, job: dict) -> list[dict]:
        ctx = job_context(self.tracker, job, self.profile)
        have = self.tracker.contacts_for(job["id"])
        if have:
            self.echo("  Already contacting: " + ", ".join(f"{c['name']} ({c['status']})" for c in have))
        self.echo("  Looking for people (posting, company team pages, engineering blog, GitHub; never LinkedIn)...")
        try:
            people = self.finder(job, ctx.description)
        except Exception as e:  # noqa: BLE001 - the LinkedIn links and manual entry still work
            self.echo(f"  ! Couldn't search ({str(e).splitlines()[0][:100]}).")
            people = People(team=None, email_domain=None, people=[])
        if people.team:
            self.echo(f"  Team in the posting: {people.team}")
        for i, p in enumerate(people.people, 1):
            self.echo(f"   {i}. {p.name}, {p.title or 'title unknown'}  [{ROLE_NAMES[p.role]}]")
            self.echo(f"      {p.evidence}  ({p.source_url})")
        if not people.people:
            self.echo("   Nobody found on public pages. Try the LinkedIn searches below, then add someone with m.")
        searches = warm_searches(job["company"], self.master)
        for i, (label, _) in enumerate(searches, 1):
            self.echo(f"   l{i}. LinkedIn search in your Chrome: {label}")

        picked: list[Person] = []
        names = {c["name"].lower() for c in have}
        while len(picked) + len(have) < 2:
            c = self.ask("  Pick by number (e.g. 1,2), l1/l2 to open a LinkedIn search, m to add someone by name, "
                         "Enter when done: ").strip().lower()
            if not c:
                break
            if c.startswith("l") and c[1:].isdigit() and 1 <= int(c[1:]) <= len(searches):
                self.open_url(searches[int(c[1:]) - 1][1])
                self.echo("  Opened in your Chrome. Look, don't message from there yet; add the person here with m.")
                continue
            if c == "m":
                p = self._ask_person(job)
                if p:
                    picked.append(p)
                continue
            for part in c.replace(" ", ",").split(","):
                if part.isdigit() and 1 <= int(part) <= len(people.people):
                    p = people.people[int(part) - 1]
                    if p.name.lower() not in names and p not in picked:
                        picked.append(p)
        made = []
        for p in picked[:max(0, 2 - len(have))]:
            contact = self.reach(job, ctx, p, people.email_domain)
            if contact:
                made.append(contact)
        return made

    def _ask_person(self, job: dict) -> Person | None:
        name = self.ask("    Their full name: ").strip()
        if not name:
            return None
        title = self.ask("    Their title (optional): ").strip() or None
        role = ""
        while role not in ROLE_KEYS:
            role = self.ask("    Why them? [h]iring manager / [e]ngineer / [a]lum (shares your school or IBM) / "
                            "[r]ecruiter: ").strip().lower()[:1]
        evidence = self.ask("    One true, specific thing about them to mention (a post, talk, project), "
                            "or Enter: ").strip()
        return Person(name=name, title=title, role=ROLE_KEYS[role], evidence=evidence or "added by you",
                      source_url="added by you")

    def reach(self, job: dict, ctx, person: Person, domain: str | None) -> dict | None:
        ch = ""
        while ch not in ("e", "l", "s"):
            ch = self.ask(f"  {person.name}: [e]mail draft / [l]inkedIn note / [s]kip: ").strip().lower()[:1]
        if ch == "s":
            return None
        source = None if person.source_url == "added by you" else person.source_url
        contact = new_contact(job, person.name, person.role, "email" if ch == "e" else "linkedin", title=person.title,
                              source_url=source, evidence=person.evidence, today=self.today)
        if ch == "e":
            email, status = self.pick_email(person.name, domain)
            if not email:
                self.echo("  No address, so this one is a LinkedIn note instead.")
                contact["channel"] = "linkedin"
            contact["email"], contact["email_status"] = email, status
        try:
            d = self.writer.write(kind_for(contact), ctx, contact)
        except DraftRejected as e:
            self.echo(f"  ! The draft kept failing the fact check ({e}); nothing saved for {person.name}.")
            return None
        if contact["channel"] == "email":
            self._email(contact, d)
        else:
            self._linkedin(contact, d, job)
        self.tracker.add_contact(contact)
        return contact

    def pick_email(self, name: str, domain: str | None) -> tuple[str | None, str | None]:
        if not domain:
            domain = self.ask("    Company email domain (e.g. acme.com), or Enter to skip: ").strip().lower()
            domain = domain.removeprefix("@").removeprefix("https://").removeprefix("www.").split("/")[0]
        if not domain:
            return None, None
        guesses = guess_emails(name, domain, load_patterns(self.patterns_path).get(domain))
        i, status = 0, "guessed"
        while i < len(guesses):
            g = guesses[i]
            verify = " / [v]erify with Hunter (1 lookup)" if self.hunter_key else ""
            c = self.ask(f"    Email: {g} ({status})  [k]eep / [n]ext guess / [t]ype one{verify}: ").strip().lower()[:1]
            if c == "k":
                return g, status
            if c == "t":
                typed = self.ask("    Address: ").strip()
                return (typed, "typed by you") if "@" in typed else (None, None)
            if c == "v" and self.hunter_key:
                try:
                    status = hunter_verify(g, self.hunter_key, self.http) or "guessed"
                except Exception as e:  # noqa: BLE001 - keep the guess
                    self.echo(f"    Hunter didn't answer ({str(e)[:80]}).")
                    continue
                self.echo(f"    Hunter: {status}")
                if status == "verified":
                    pattern = pattern_of(g, name)
                    if pattern:
                        save_pattern(self.patterns_path, domain, pattern)
                    return g, status
                if status == "invalid":
                    i, status = i + 1, "guessed"
                continue
            if c == "n":
                i, status = i + 1, "guessed"
        self.echo("    No more guesses.")
        return None, None

    def _service(self):
        if self._svc is None and self.drafts is not None:
            self._svc = self.drafts()
        return self._svc

    def _email(self, contact: dict, d) -> None:
        body = d.body + self.writer.signature()
        contact["subject"], contact["first_body"] = d.subject, d.body
        where = "your clipboard"
        try:
            svc = self._service()
        except Exception as e:  # noqa: BLE001 - e.g. drafts not allowed yet: the clipboard still works
            self.echo(f"  ! {e}")
            svc = None
        if svc is not None:
            contact["draft_ids"].append(create_draft(svc, contact["email"], d.subject, body))
            where = "Gmail Drafts"
        else:
            self.copy(f"To: {contact['email']}\nSubject: {d.subject}\n\n{body}")
        self.echo(f"  Draft for {contact['name']} <{contact['email']}> ({contact['email_status']}) is in {where}:")
        self.echo(f"    Subject: {d.subject}")
        for line in body.splitlines():
            self.echo(f"    {line}")
        self.echo("  Read it, edit it, and click Send in Gmail yourself. jobagent notices it in your Sent folder.")

    def _linkedin(self, contact: dict, d, job: dict) -> None:
        url = linkedin_search_url(contact["name"], job["company"])
        contact["linkedin_url"], contact["first_body"], contact["after_accept"] = url, d.connect_note, d.body
        self.copy(d.connect_note or "")
        self.open_url(url)
        self.echo(f"  LinkedIn: a search for {contact['name']} at {job['company']} is open in your Chrome.")
        self.echo(f"  Connection note on your clipboard ({len(d.connect_note or '')} characters):")
        self.echo(f"    {d.connect_note}")
        self.echo("  After they accept, send this (saved in the Contacts sheet too):")
        for line in d.body.splitlines():
            self.echo(f"    {line}")
        if self.ask("  Did you send the invite? [y]es / [n]ot yet: ").strip().lower()[:1] == "y":
            record_sent(contact, self.today or date.today())


# ---- the daily pass ---------------------------------------------------------------------------------------------


@dataclass
class OutreachReport:
    funnel: str = ""
    advice: str | None = None
    sent: list[str] = field(default_factory=list)
    replied: list[str] = field(default_factory=list)
    due: list[str] = field(default_factory=list)                   # follow-ups due, one line each
    texts: list[tuple[str, str]] = field(default_factory=list)     # (who, follow-up text) not in Gmail Drafts
    errors: list[str] = field(default_factory=list)


def daily(tracker, *, read_svc=None, drafts_svc=None, writer: OutreachWriter | None = None,
          profile: Profile | None = None, today: date | None = None) -> OutreachReport:
    """Notice sends and replies, draft the follow-ups that are due (never send), and count the week."""
    today = today or date.today()
    report = OutreachReport()
    contacts = list(tracker.contacts.values())
    if read_svc is not None and contacts:
        try:
            s = scan(read_svc, contacts)
            report.sent, report.replied = s.sent, s.replied
        except Exception as e:  # noqa: BLE001 - the follow-up list still works from what's known
            report.errors.append(f"Gmail check: {str(e)[:120]}")
    for c, n in due_follow_ups(contacts, today):
        who = f"{c['name']} ({ROLE_NAMES.get(c['role'], c['role'])}, {c['company']}, {c['channel']})"
        since = (today - date.fromisoformat(min(c["sent_dates"]))).days
        line = f"{who}: follow-up {n} of 2, day {since} after your first message"
        if c.get("follow_ups_drafted", 0) >= n:
            report.due.append(line + " (already drafted)")
            continue
        if writer is None or profile is None:
            report.due.append(line)
            continue
        job = tracker.jobs.get(c["job_id"]) or {"id": c["job_id"], "company": c["company"],
                                                "title": c.get("job_title") or ""}
        try:
            d = writer.write("follow_up", job_context(tracker, job, profile), c, follow_up=n,
                             previous=c.get("first_body"))
        except Exception as e:  # noqa: BLE001 - listed without a draft
            report.due.append(f"{line} (no draft: {str(e)[:80]})")
            continue
        body = d.body + writer.signature()
        if c["channel"] == "email" and drafts_svc is not None and c.get("email"):
            subject = c.get("subject") or f"{c.get('job_title') or 'Following up'}"
            subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
            c.setdefault("draft_ids", []).append(create_draft(drafts_svc, c["email"], subject, body,
                                                              thread_id=c.get("thread_id"),
                                                              in_reply_to=c.get("first_message_id")))
            report.due.append(line + " (draft in Gmail Drafts)")
        else:
            report.due.append(line + " (text below)")
            report.texts.append((who, body))
        c["follow_ups_drafted"] = n
    report.funnel = funnel_line(list(tracker.contacts.values()), list(tracker.jobs.values()), today)
    report.advice = funnel_advice(list(tracker.contacts.values()))
    return report


def summary_lines(r: OutreachReport) -> list[str]:
    out = ["## Outreach", r.funnel + "."]
    if r.advice:
        out.append(f"- {r.advice}")
    out += [f"- You sent: {s}" for s in r.sent] + [f"- **Reply:** {s}" for s in r.replied]
    if r.due:
        out += ["", "**Follow-ups due** (you click Send; after the second, stop):"] + [f"- {d}" for d in r.due]
    for who, text in r.texts:
        out += ["", f"Follow-up for {who}:", ""] + [f"> {line}" if line else ">" for line in text.splitlines()]
    out += [f"- (couldn't check: {e})" for e in r.errors]
    return out + [""]
