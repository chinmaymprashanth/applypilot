from jobagent.inbox import Email, Update, apply_emails, inbox_query, mentioned
from jobagent.tracker import Tracker


def _tracker(tmp_path):
    t = Tracker(tmp_path / "t.xlsx", tmp_path / "jobs")
    for company, title in [("SharkNinja Operating LLC", "Forward Deployed Engineer"), ("Handshake", "ML Engineer"),
                           ("Handshake", "Data Analyst"), ("Acme", "Backend Engineer")]:
        t.add_job({"company": company, "title": title, "url": f"https://x.io/{company}/{title}", "source": "ashby"})
    for jid, r in t.jobs.items():
        if r["company"] != "Acme":
            t.set_status(jid, "applied")
    return t


def email(subject, text, sender="no-reply@ashbyhq.com", day="2026-11-12", i="1"):
    return Email(i, sender, subject, day, text)


def test_query_covers_hiring_systems_and_subjects():
    q = inbox_query(30)
    assert q.startswith("newer_than:30d") and "from:greenhouse-mail.io" in q and "interview" in q


def test_only_emails_about_your_applications_are_read(tmp_path):
    t = _tracker(tmp_path)
    apps = [r for r in t.jobs.values() if r["status"] == "applied"]
    assert [a["company"] for a in mentioned(email("Thanks for applying to SharkNinja", ""), apps)] == [
        "SharkNinja Operating LLC"]
    assert mentioned(email("Your Acme application", "Acme"), apps) == []  # not applied there
    assert mentioned(email("Weekly newsletter", "Tips"), apps) == []


def test_updates_status_and_notes_newest_last(tmp_path):
    t = _tracker(tmp_path)
    answers = {
        "a": Update(kind="confirmation", company="Handshake", role="ML Engineer", summary="Application received.",
                    action_needed=None),
        "b": Update(kind="interview", company="Handshake", role="Machine Learning Engineer",
                    summary="Invited to a recruiter call.", action_needed="Pick a time by Friday"),
        "c": Update(kind="rejection", company="SharkNinja", role=None, summary="Not moving forward.",
                    action_needed=None),
        "d": Update(kind="other", company="Handshake", role=None, summary="Newsletter", action_needed=None),
    }
    emails = [email("Interview with Handshake", "Handshake", day="2026-11-14", i="b"),
              email("Handshake: we got your application", "Handshake", day="2026-11-12", i="a"),
              email("Update from SharkNinja", "SharkNinja", day="2026-11-13", i="c"),
              email("Handshake news", "Handshake", day="2026-11-15", i="d")]
    r = apply_emails(t, emails, lambda e: answers[e.id])

    rows = {(x["company"], x["title"]): x for x in t.jobs.values()}
    ml = rows[("Handshake", "ML Engineer")]
    assert ml["status"] == "interview"
    assert ml["notes"] == ("2026-11-12: confirmation - Application received.; "
                           "2026-11-14: interview - Invited to a recruiter call.")
    assert rows[("Handshake", "Data Analyst")]["status"] == "applied"  # the other Handshake application
    assert rows[("SharkNinja Operating LLC", "Forward Deployed Engineer")]["status"] == "rejected"
    assert r.actions == ["Handshake: Pick a time by Friday"] and r.read == 4 and len(r.updates) == 3


def test_offer_is_never_overwritten(tmp_path):
    t = _tracker(tmp_path)
    jid = next(j for j, r in t.jobs.items() if r["company"] == "Acme")
    t.set_status(jid, "offer")
    apply_emails(t, [email("Acme", "Acme")], lambda e: Update(kind="rejection", company="Acme", role=None,
                                                              summary="Other role closed.", action_needed=None))
    assert t.jobs[jid]["status"] == "offer" and "rejection" in t.jobs[jid]["notes"]
