"""Outreach: drafting, the fact check, the follow-up schedule, LinkedIn links only, and that nothing is sent."""

import json
import re
from datetime import date
from pathlib import Path

import pytest

from jobagent.outreach.assistant import Assistant, daily, summary_lines
from jobagent.outreach.contacts import (due_follow_ups, funnel, funnel_line, new_contact, next_follow_up,
                                        record_sent, refresh, set_status)
from jobagent.outreach.draft import DraftRejected, JobContext, OutreachDraft, OutreachWriter, check
from jobagent.outreach.find import (People, Person, guess_emails, hunter_verify, is_linkedin, keep_sourced,
                                    linkedin_search_url, pattern_of, warm_searches)
from jobagent.outreach.mail import create_draft, scan
from jobagent.profile import MasterResume, Profile
from jobagent.summary import build_summary
from jobagent.tracker import Tracker

MASTER = MasterResume.model_validate({
    "experience": [{"company": "IBM", "title": "Application Developer", "start": "2025-09", "end": "2026-10",
                    "bullets": ["Resolved 100+ production incidents with 100% SLA compliance using SQL."]}],
    "projects": [{"name": "Job Agent", "tech": ["Python", "Docker", "AWS"],
                  "bullets": ["Built an LLM agent that finds and ranks jobs, deployed on AWS with Docker."]}],
    "education": [{"school": "University of Massachusetts Amherst", "degree": "MS Computer Science",
                   "start": "2027-02", "end": "2028-12", "expected": True}],
    "skills": [{"category": "Programming", "items": ["Python", "SQL"]},
               {"category": "Tools", "items": ["Docker", "AWS"]}],
})
PROFILE = Profile(name="Alex Rivera", location="Boston, MA", phone="+1 555 010 0123", email="a@example.com",
                  github="https://github.com/a", linkedin="https://www.linkedin.com/in/a")

HM_BODY = ("Hi Sam, at IBM I resolved 100+ production incidents with 100% SLA compliance, most of them by "
           "tracing data issues in SQL. I recently built an LLM agent that finds and ranks jobs, deployed on AWS "
           "with Docker. I saw Acme is hiring a Backend Engineer on your payments team, and the way your team "
           "writes about reliability is the kind of work I want to do more of. I've applied, and I'd value 15 "
           "minutes this week or next to learn what the team needs most from a new hire.")
PEER_BODY = ("Hi Pat, your post on cutting deploy times for the payments service was useful; the part about "
             "small, reversible changes matched what I saw supporting production at IBM. I'm an application "
             "developer moving toward data and AI work with Python and SQL, and I'm looking at Acme's payments "
             "team. Would you be open to a 15-minute chat in the next couple of weeks?")
NOTE = "Hi Pat, incoming UMass Amherst MS CS student and IBM app dev here. I'd love to connect and hear how you moved into payments at Acme."
FOLLOW = ("Hi Sam, following up on my note about the Backend Engineer role. Since then I added job ranking to "
          "my LLM agent on AWS. If a 15-minute chat in the next week or two works, I'd be glad to talk.")


def job():
    return {"id": "j1", "company": "Acme", "title": "Backend Engineer", "apply_url": "https://jobs.acme.com/1"}


def ctx():
    return JobContext(job=job(), description="Payments team. Python, SQL.", research=["C1: Acme raised a Series B."],
                      cover_letter="Dear team...")


def writer(*drafts):
    """A writer whose 'Claude' returns these drafts in turn, recording what it was asked."""
    queue, seen = list(drafts), []

    def call(system, content):
        seen.append(json.loads(content))
        return queue.pop(0) if len(queue) > 1 else queue[0]

    w = OutreachWriter(call, MASTER, PROFILE)
    w.seen = seen
    return w


def contact(role="hiring_manager", channel="email", name="Sam Diaz"):
    c = new_contact(job(), name, role, channel, title="Engineering Manager", evidence="Wrote about payments reliability.",
                    today=date(2026, 11, 20))
    c["email"] = "sam.diaz@acme.com"
    return c


# ---- drafting and the fact check -----------------------------------------------------------------------------


def test_hiring_manager_email_draft_passes():
    w = writer(OutreachDraft(subject="IBM developer -> Backend Engineer on your team", body=HM_BODY))
    d = w.write("hiring_manager", ctx(), contact())
    assert d.body == HM_BODY and len(w.seen) == 1
    assert w.seen[0]["person"]["name"] == "Sam Diaz" and "Backend Engineer at Acme" == w.seen[0]["job"]
    assert w.seen[0]["cover_letter_already_sent"] and w.seen[0]["company_research"]
    assert "U.S. citizen" in w.signature() and "github.com/a" in w.signature()


def test_invented_skill_is_sent_back_once_then_fixed():
    bad = OutreachDraft(subject="Hi", body=HM_BODY.replace("SQL", "Kafka"))
    good = OutreachDraft(subject="IBM developer -> Backend Engineer", body=HM_BODY)
    w = writer(bad, good)
    assert w.write("hiring_manager", ctx(), contact()).body == HM_BODY
    assert any("kafka" in p for p in w.seen[1]["fix_these"])


def test_a_draft_that_keeps_failing_gives_nothing():
    bad = OutreachDraft(subject="Hi", body=HM_BODY.replace("100+ production", "5 years of production"))
    with pytest.raises(DraftRejected) as e:
        writer(bad).write("hiring_manager", ctx(), contact())
    assert "numbers not in the record: 5" in str(e.value)


@pytest.mark.parametrize("draft,kind,role,problem", [
    (OutreachDraft(subject="s", body="Hi Sam, quick question about the role. " * 2), "hiring_manager",
     "hiring_manager", "words; it must be 75-150"),
    (OutreachDraft(subject="s", body="I hope this finds you well. " + HM_BODY), "hiring_manager", "hiring_manager",
     "boilerplate"),
    (OutreachDraft(subject="s", body=HM_BODY.replace("Hi Sam", "Hi [First name]")), "hiring_manager",
     "hiring_manager", "placeholder"),
    (OutreachDraft(subject="s", body="Hi Pat, fellow IBM person here. " + PEER_BODY), "peer", "engineer",
     "shared ground"),
    (OutreachDraft(body=PEER_BODY, connect_note=NOTE + " " + NOTE), "linkedin", "engineer", "under 200"),
    (OutreachDraft(subject=None, body=HM_BODY), "peer", "engineer", "needs a subject"),
])
def test_fact_check_rules(draft, kind, role, problem):
    w = writer(draft)
    c = contact(role=role)
    problems = check(draft, kind, w.record_nums | {"15"}, w.allowed_tech, c)
    assert any(problem in p for p in problems), problems


def test_linkedin_note_under_200_characters_passes():
    w = writer(OutreachDraft(body=PEER_BODY, connect_note=NOTE))
    d = w.write("linkedin", ctx(), contact(role="alum", channel="linkedin"))
    assert len(d.connect_note) < 200


def test_alum_may_say_fellow():
    d = OutreachDraft(subject="s", body="Hi Pat, fellow IBM person here. " + PEER_BODY)
    w = writer(d)
    assert check(d, "peer", w.record_nums | {"15"}, w.allowed_tech, contact(role="alum")) == []


# ---- the follow-up schedule --------------------------------------------------------------------------------------


def test_follow_up_day_5_then_day_14_then_stop():
    c = contact()
    assert next_follow_up(c) is None                      # drafted, not sent: nothing to follow up
    record_sent(c, "2026-11-20", "m1", "t1")
    assert c["status"] == "sent" and c["follow_up_due"] == "2026-11-25"
    assert due_follow_ups([c], date(2026, 11, 24)) == []  # day 4
    assert due_follow_ups([c], date(2026, 11, 25)) == [(c, 1)]  # day 5
    record_sent(c, "2026-11-26", "m2")                    # you sent the first follow-up
    assert next_follow_up(c) == (2, date(2026, 12, 4))    # optional second at day 14
    assert due_follow_ups([c], date(2026, 12, 3)) == []
    assert due_follow_ups([c], date(2026, 12, 4)) == [(c, 2)]
    record_sent(c, "2026-12-04", "m3")
    assert next_follow_up(c) is None                      # two follow-ups: stop
    refresh(c, date(2026, 12, 11))
    assert c["status"] == "no_reply" and c["follow_up_due"] is None


def test_a_reply_stops_follow_ups():
    c = contact()
    record_sent(c, "2026-11-20", "m1")
    set_status(c, "replied", date(2026, 11, 22))
    assert due_follow_ups([c], date(2026, 11, 30)) == [] and c["replied_date"] == "2026-11-22"


def test_unanswered_contacts_stop_after_three_weeks_even_without_follow_ups():
    c = contact()
    record_sent(c, "2026-11-01", "m1")
    assert due_follow_ups([c], date(2026, 11, 22)) == [] and c["status"] == "no_reply"


def test_same_sent_message_is_counted_once():
    c = contact()
    assert record_sent(c, "2026-11-20", "m1") and not record_sent(c, "2026-11-20", "m1")
    assert c["sent_dates"] == ["2026-11-20"]


# ---- the weekly funnel --------------------------------------------------------------------------------------------


def test_weekly_funnel_counts_the_last_seven_days():
    a, b, old = contact(name="A B"), contact(name="C D"), contact(name="E F")
    record_sent(a, "2026-11-21")
    record_sent(b, "2026-11-24")
    set_status(b, "call", date(2026, 11, 25))
    record_sent(old, "2026-11-01")
    jobs = [{"interview_date": "2026-11-23"}, {"interview_date": "2026-10-01"}, {}]
    week = funnel([a, b, old], jobs, date(2026, 11, 26))
    assert week == {"messages": 2, "replies": 1, "calls": 1, "referrals": 0, "interviews": 1}
    line = funnel_line([a, b, old], jobs, date(2026, 11, 26))
    assert line.startswith("Outreach this week: 2 messages, 1 replies, 1 calls, 0 referrals, 1 interviews")
    assert "all time: 3 messages, 1 replies, 33%" in line


# ---- finding people: public sources, LinkedIn links only ---------------------------------------------------------


def test_people_must_come_from_search_results_and_never_linkedin():
    people = People(team="Payments", email_domain="acme.com", people=[
        Person(name="Sam Diaz", title="EM", role="hiring_manager", evidence="Team page.",
               source_url="https://acme.com/team"),
        Person(name="Pat Lee", title="SWE", role="engineer", evidence="Profile.",
               source_url="https://www.linkedin.com/in/patlee"),
        Person(name="Ghost", title=None, role="engineer", evidence="Made up.", source_url="https://nowhere.example"),
        Person(name="Kim Ro", title="SWE", role="engineer", evidence="Blog byline.",
               source_url="https://engineering.acme.com/post-1"),
    ])
    results = [{"url": "https://acme.com/team"}, {"url": "https://engineering.acme.com/post-1"},
               {"url": "https://www.linkedin.com/in/patlee"}]
    kept = keep_sourced(people, results, job())
    assert [p.name for p in kept.people] == ["Sam Diaz", "Kim Ro"] and kept.email_domain == "acme.com"
    assert keep_sourced(people.model_copy(update={"email_domain": "acme.io"}), results, job()).email_domain is None
    assert is_linkedin("https://linkedin.com/x") and is_linkedin("https://uk.linkedin.com/x")


def test_linkedin_links_use_your_school_and_past_employer():
    searches = warm_searches("Acme", MASTER)
    labels = [label for label, _ in searches]
    assert labels == ["University of Massachusetts Amherst people at Acme", "IBM people at Acme"]
    url = searches[0][1]
    assert url.startswith("https://www.linkedin.com/search/results/people/?keywords=Acme+%22University+of+Massachusetts")
    assert warm_searches("IBM", MASTER)[-1][0].startswith("University")  # no "IBM people at IBM"
    assert "Sam+Diaz" in linkedin_search_url("Sam Diaz", "Acme")


def test_finder_blocks_linkedin_in_the_web_search(monkeypatch, tmp_path):
    from jobagent.outreach import find

    asked = {}

    def fake_search(client, **kw):
        asked.update(kw)
        return People(team=None, email_domain=None, people=[]), []

    monkeypatch.setattr("jobagent.llm.parse_with_search", fake_search)
    find.claude_finder(None, "m", MASTER, tmp_path)(job(), "desc")
    assert asked["blocked_domains"][0] == "linkedin.com" and "rocketreach.co" in asked["blocked_domains"]
    assert "Never use LinkedIn" in asked["system"]


class FakeGitHub:
    def __init__(self):
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(url)
        users = {"ann": {"name": "Ann Smith", "bio": "Platform engineer", "html_url": "https://github.com/ann"},
                 "bot": {"name": None}, "kim": {"name": "Kim Ro", "html_url": "https://github.com/kim"}}

        class R:
            status_code = 200

            def json(self_inner):
                if url.endswith("/public_members"):
                    return [{"login": k} for k in users]
                return users[url.rsplit("/", 1)[1]]

        return R()


def test_github_org_members_are_an_extra_source(monkeypatch, tmp_path):
    from jobagent.outreach import find

    def fake_search(client, **kw):
        return People(team=None, github_org="acme", email_domain=None, people=[
            Person(name="Kim Ro", title="SWE", role="engineer", evidence="Blog.",
                   source_url="https://engineering.acme.com/p")]), [{"url": "https://engineering.acme.com/p"}]

    monkeypatch.setattr("jobagent.llm.parse_with_search", fake_search)
    gh = FakeGitHub()
    people = find.claude_finder(None, "m", MASTER, tmp_path, http=gh)(job(), "desc")
    assert [p.name for p in people.people] == ["Kim Ro", "Ann Smith"]   # no duplicate Kim, no nameless bot
    assert "Platform engineer" in people.people[1].evidence
    assert all(u.startswith("https://api.github.com/") for u in gh.calls)


def test_data_broker_sources_are_dropped():
    people = People(team=None, email_domain=None, people=[
        Person(name="Rob B", title=None, role="engineer", evidence="x", source_url="https://rocketreach.co/rob")])
    assert keep_sourced(people, [{"url": "https://rocketreach.co/rob"}], job()).people == []


# ---- email addresses and Hunter -----------------------------------------------------------------------------------


def test_email_guesses_start_with_the_known_pattern():
    assert guess_emails("Sam Diaz", "acme.com")[:3] == ["sam.diaz@acme.com", "sam@acme.com", "sdiaz@acme.com"]
    assert guess_emails("Dr. José Núñez", "acme.com", "{f}{last}")[0] == "jnunez@acme.com"
    assert guess_emails("Cher", "acme.com") == ["cher@acme.com"]
    assert pattern_of("sdiaz@acme.com", "Sam Diaz") == "{f}{last}"


class FakeHTTP:
    def __init__(self, status):
        self.status, self.calls = status, []

    def get(self, url, params, headers, timeout):
        self.calls.append((url, params, headers))
        status = self.status

        class R:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {"data": {"status": status}}

        return R()


def test_hunter_accept_all_is_unverified_and_no_key_sends_nothing():
    http = FakeHTTP("accept_all")
    assert hunter_verify("sam.diaz@acme.com", "KEY", http) == "unverified (accept-all)"
    url, params, headers = http.calls[0]
    assert params == {"email": "sam.diaz@acme.com"} and headers == {"X-API-KEY": "KEY"}  # key not in the URL
    assert hunter_verify("sam.diaz@acme.com", "KEY", FakeHTTP("valid")) == "verified"
    none = FakeHTTP("valid")
    assert hunter_verify("sam.diaz@acme.com", None, none) is None and none.calls == []


# ---- Gmail: drafts only, never send ------------------------------------------------------------------------------


class Req:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


class FakeGmail:
    """Records every call. Any attempt to send raises, so a test fails if jobagent ever sends."""

    def __init__(self, sent=(), inbox=()):
        self.calls, self.drafts_made = [], []
        self.sent, self.inbox = list(sent), list(inbox)

    def users(self):
        return self

    def drafts(self):
        outer = self

        class Drafts:
            def create(self, userId, body):
                outer.calls.append("drafts.create")
                outer.drafts_made.append(body)
                return Req({"id": f"d{len(outer.drafts_made)}"})

            def send(self, *a, **k):
                raise AssertionError("jobagent tried to send a draft")

        return Drafts()

    def messages(self):
        outer = self

        class Messages:
            def list(self, userId, q, maxResults):
                outer.calls.append(f"messages.list {q}")
                box = outer.sent if "in:sent" in q and "-in:sent" not in q else outer.inbox
                return Req({"messages": [{"id": m["id"]} for m in box]})

            def list_next(self, req, resp):
                return None

            def get(self, userId, id, format, metadataHeaders):
                assert format == "metadata"  # headers only
                m = next(m for m in outer.sent + outer.inbox if m["id"] == id)
                headers = [{"name": k, "value": v} for k, v in m["headers"].items()]
                return Req({"id": id, "threadId": m.get("thread", "t1"), "internalDate": m["ms"],
                            "payload": {"headers": headers}})

            def send(self, *a, **k):
                raise AssertionError("jobagent tried to send an email")

        return Messages()




def ms(day: str) -> str:
    from datetime import datetime, timezone
    return str(int(datetime.fromisoformat(day + "T12:00:00").replace(tzinfo=timezone.utc).timestamp() * 1000))


def test_create_draft_only_creates_a_draft():
    g = FakeGmail()
    assert create_draft(g, "sam@acme.com", "Hi", "Body", thread_id="t9", in_reply_to="<m1@x>") == "d1"
    body = g.drafts_made[0]["message"]
    assert body["threadId"] == "t9" and g.calls == ["drafts.create"]
    import base64
    raw = base64.urlsafe_b64decode(body["raw"]).decode()
    assert "To: sam@acme.com" in raw and "In-Reply-To: <m1@x>" in raw


def test_no_outreach_code_can_send():
    """Belt and braces: the outreach code contains no call that sends mail or touches LinkedIn."""
    root = Path(__file__).resolve().parent.parent / "jobagent"
    for path in [*(root / "outreach").glob("*.py"), root / "search" / "gmail.py"]:
        text = path.read_text()
        assert not re.search(r"\.send\(", text), path
        assert "sendmail" not in text and "smtplib" not in text, path
    for path in (root / "outreach").glob("*.py"):
        assert not re.search(r"(httpx|requests)\.\w+\([^)]*linkedin", path.read_text()), path


def test_scan_notices_your_sends_and_real_replies():
    c = contact()
    c["created"] = "2026-11-19"
    g = FakeGmail(
        sent=[{"id": "s1", "thread": "t1", "ms": ms("2026-11-20"),
               "headers": {"To": "Sam Diaz <sam.diaz@acme.com>", "Subject": "IBM developer", "Message-ID": "<m1@x>"}}],
        inbox=[{"id": "r0", "ms": ms("2026-11-21"), "headers": {"From": "Sam Diaz <sam.diaz@acme.com>",
                                                              "Subject": "Automatic reply: out of office"}},
               {"id": "r1", "ms": ms("2026-11-22"), "headers": {"From": "Sam Diaz <SAM.DIAZ@acme.com>",
                                                              "Subject": "Re: IBM developer"}}])
    r = scan(g, [c])
    assert c["sent_dates"] == ["2026-11-20"] and c["thread_id"] == "t1" and c["first_message_id"] == "<m1@x>"
    assert c["status"] == "replied" and c["replied_date"] == "2026-11-22"
    assert r.sent == ["Sam Diaz (Acme): first message"] and len(r.replied) == 1
    assert not any(x.startswith("drafts") for x in g.calls)  # reading only


def test_out_of_office_is_not_a_reply():
    c = contact()
    record_sent(c, "2026-11-20", "s1")
    g = FakeGmail(inbox=[{"id": "r0", "ms": ms("2026-11-21"), "headers": {
        "From": "sam.diaz@acme.com", "Subject": "Out of office", "Auto-Submitted": "auto-replied"}}])
    scan(g, [c])
    assert c["status"] == "sent"


# ---- the assistant after "Submitted? y" ------------------------------------------------------------------------


def _tracker(tmp_path):
    t = Tracker(tmp_path / "t.xlsx", tmp_path / "data" / "jobs")
    t.add_job({"company": "Acme", "title": "Backend Engineer", "url": "https://jobs.acme.com/1", "source": "ashby",
               "description": "Payments team."})
    jid = next(iter(t.jobs))
    t.set_status(jid, "applied", date(2026, 11, 20))
    return t, t.jobs[jid]


def _people():
    return People(team="Payments", email_domain="acme.com", people=[
        Person(name="Sam Diaz", title="Engineering Manager", role="hiring_manager",
               evidence="Leads the payments team per the team page.", source_url="https://acme.com/team"),
        Person(name="Pat Lee", title="Software Engineer", role="engineer",
               evidence="Wrote a post on cutting deploy times.", source_url="https://engineering.acme.com/p")])


def _answers(*replies):
    queue = list(replies)
    return lambda prompt: queue.pop(0)


def test_assistant_drafts_an_email_and_a_linkedin_note_and_sends_nothing(tmp_path):
    t, j = _tracker(tmp_path)
    g, opened, clip, out = FakeGmail(), [], [], []
    w = writer(OutreachDraft(subject="IBM developer -> Backend Engineer", body=HM_BODY),
               OutreachDraft(body=PEER_BODY, connect_note=NOTE))
    a = Assistant(t, w, lambda job, desc: _people(), PROFILE, MASTER, drafts=lambda: g, ask=_answers(
        "y",          # find people?
        "1,2",        # pick both
        "e", "k",     # Sam: email, keep sam.diaz@acme.com
        "l", "n",     # Pat: LinkedIn note, haven't sent yet
    ), echo=out.append, open_url=opened.append, copy=clip.append, today=date(2026, 11, 20))
    a.offer(j)
    sam, pat = sorted(t.contacts_for(j["id"]), key=lambda c: c["name"], reverse=True)
    assert sam["email"] == "sam.diaz@acme.com" and sam["status"] == "drafted" and sam["draft_ids"] == ["d1"]
    assert g.calls == ["drafts.create"]                           # one draft; nothing sent
    raw = g.drafts_made[0]["message"]["raw"]
    import base64
    assert "15 minutes" in base64.urlsafe_b64decode(raw).decode()
    assert pat["channel"] == "linkedin" and pat["status"] == "drafted"
    assert clip == [NOTE]                                         # the note waits on your clipboard
    assert opened == [linkedin_search_url("Pat Lee", "Acme")]     # a search link in your own Chrome, nothing else
    assert all(u.startswith("https://www.linkedin.com/search/results/people/") for u in opened)
    assert any("click Send in Gmail yourself" in line for line in out)


def test_linkedin_invite_you_sent_starts_the_follow_up_clock(tmp_path):
    t, j = _tracker(tmp_path)
    w = writer(OutreachDraft(body=PEER_BODY, connect_note=NOTE))
    a = Assistant(t, w, lambda job, desc: _people(), PROFILE, MASTER, ask=_answers("y", "2", "", "l", "y"),
                  echo=lambda s: None, today=date(2026, 11, 20))
    a.offer(j)
    (pat,) = t.contacts_for(j["id"])
    assert pat["status"] == "sent" and pat["follow_up_due"] == "2026-11-25"


def test_without_gmail_drafts_the_email_goes_to_the_clipboard(tmp_path):
    t, j = _tracker(tmp_path)
    clip = []
    w = writer(OutreachDraft(subject="IBM developer -> Backend Engineer", body=HM_BODY))
    a = Assistant(t, w, lambda job, desc: _people(), PROFILE, MASTER, ask=_answers("y", "1", "", "e", "n", "k"),
                  echo=lambda s: None, copy=clip.append)
    a.offer(j)
    (sam,) = t.contacts_for(j["id"])
    assert sam["email"] == "sam@acme.com" and clip[0].startswith("To: sam@acme.com\nSubject: IBM developer")


def test_hunter_check_runs_only_when_you_ask(tmp_path):
    t, j = _tracker(tmp_path)
    http = FakeHTTP("valid")
    w = writer(OutreachDraft(subject="IBM developer -> Backend Engineer", body=HM_BODY))
    a = Assistant(t, w, lambda job, desc: _people(), PROFILE, MASTER, ask=_answers("y", "1", "", "e", "v"),
                  echo=lambda s: None, hunter_key="KEY", http=http)
    a.offer(j)
    (sam,) = t.contacts_for(j["id"])
    assert len(http.calls) == 1 and sam["email_status"] == "verified"
    assert json.loads(a.patterns_path.read_text()) == {"acme.com": "{first}.{last}"}


def test_declining_outreach_does_nothing(tmp_path):
    t, j = _tracker(tmp_path)
    found = []
    a = Assistant(t, writer(OutreachDraft(body="x")), lambda job, desc: found.append(1), PROFILE, MASTER,
                  ask=_answers("n"), echo=lambda s: None)
    a.offer(j)
    assert found == [] and t.contacts == {}


def test_a_failing_draft_saves_no_contact(tmp_path):
    t, j = _tracker(tmp_path)
    out = []
    bad = OutreachDraft(subject="s", body=HM_BODY.replace("SQL", "Kafka"))
    a = Assistant(t, writer(bad), lambda job, desc: _people(), PROFILE, MASTER,
                  ask=_answers("y", "1", "", "e", "k"), echo=out.append)
    a.offer(j)
    assert t.contacts == {} and any("fact check" in line for line in out)


# ---- the daily pass and the summary --------------------------------------------------------------------------


def test_daily_pass_drafts_due_follow_ups_once_and_never_sends(tmp_path):
    t, j = _tracker(tmp_path)
    c = t.add_contact(contact())
    c["job_id"] = j["id"]
    c["subject"], c["first_body"] = "IBM developer -> Backend Engineer", HM_BODY
    record_sent(c, "2026-11-20", "s1", "t1")
    c["first_message_id"] = "<m1@x>"
    g = FakeGmail()
    w = writer(OutreachDraft(body=FOLLOW))
    r = daily(t, drafts_svc=g, writer=w, profile=PROFILE, today=date(2026, 11, 25))
    assert g.calls == ["drafts.create"] and g.drafts_made[0]["message"]["threadId"] == "t1"
    assert r.due == ["Sam Diaz (hiring manager, Acme, email): follow-up 1 of 2, day 5 after your first message "
                     "(draft in Gmail Drafts)"]
    assert w.seen[0]["your_first_message"] == HM_BODY
    again = daily(t, drafts_svc=g, writer=w, profile=PROFILE, today=date(2026, 11, 26))
    assert g.calls == ["drafts.create"] and again.due[0].endswith("(already drafted)")  # no second draft


def test_follow_up_text_goes_in_the_summary_without_gmail_drafts(tmp_path):
    t, j = _tracker(tmp_path)
    c = t.add_contact(contact())
    record_sent(c, "2026-11-20", "s1")
    r = daily(t, writer=writer(OutreachDraft(body=FOLLOW)), profile=PROFILE, today=date(2026, 11, 26))
    text = build_summary(t, outreach=r, today=date(2026, 11, 26))
    assert "## Outreach" in text and "Outreach this week: 1 messages" in text
    assert "Follow-ups due" in text and "> Hi Sam, following up" in text


def test_summary_lists_due_follow_ups_even_without_claude(tmp_path):
    t, j = _tracker(tmp_path)
    c = t.add_contact(contact())
    record_sent(c, "2026-11-20", "s1")
    lines = summary_lines(daily(t, today=date(2026, 11, 27)))
    assert any("follow-up 1 of 2, day 7" in line for line in lines)


# ---- the store ------------------------------------------------------------------------------------------------


def test_contacts_are_saved_with_the_tracker_and_shown_in_excel(tmp_path):
    from openpyxl import load_workbook

    t, j = _tracker(tmp_path)
    c = t.add_contact(contact())
    record_sent(c, "2026-11-20")
    t.save()
    again = Tracker(tmp_path / "t.xlsx", tmp_path / "data" / "jobs")
    assert again.contacts[c["id"]]["sent_dates"] == ["2026-11-20"]
    wb = load_workbook(tmp_path / "t.xlsx")
    assert wb.sheetnames[:2] == ["My Applications", "Contacts"]
    rows = list(wb["Contacts"].iter_rows(values_only=True))
    assert rows[1][:2] == ("Acme", "Sam Diaz") and "sent" in rows[1]


def test_status_edited_in_the_contacts_sheet_is_kept(tmp_path):
    import os
    import time

    from openpyxl import load_workbook

    t, j = _tracker(tmp_path)
    c = t.add_contact(contact())
    record_sent(c, "2026-11-20")
    t.save()
    wb = load_workbook(tmp_path / "t.xlsx")
    ws = wb["Contacts"]
    headers = [cell.value for cell in ws[1]]
    ws.cell(row=2, column=headers.index("Status") + 1).value = "call"
    wb.save(tmp_path / "t.xlsx")
    later = time.time() + 5
    os.utime(tmp_path / "t.xlsx", (later, later))
    again = Tracker(tmp_path / "t.xlsx", tmp_path / "data" / "jobs")
    got = again.contacts[c["id"]]
    assert got["status"] == "call" and got["call_date"] and got["replied_date"]


def test_inbox_records_the_interview_date_for_the_funnel(tmp_path):
    from jobagent.inbox import Email, Update, apply_emails

    t, j = _tracker(tmp_path)
    u = Update(kind="interview", company="Acme", role="Backend Engineer", summary="Call.", action_needed=None)
    apply_emails(t, [Email("1", "x@ashbyhq.com", "Interview with Acme", "2026-11-24", "Acme")], lambda e: u)
    assert t.jobs[j["id"]]["interview_date"] == "2026-11-24"


def test_warm_ties_skip_high_school_and_internships():
    from jobagent.outreach.find import warm_ties

    m = MASTER.model_copy(deep=True)
    m.education.append(m.education[0].model_copy(update={"school": "Ganga Central School"}))
    m.experience.append(m.experience[0].model_copy(update={"company": "Chimple", "title": "Data Analyst Intern"}))
    assert warm_ties(m) == ["University of Massachusetts Amherst", "IBM"]
