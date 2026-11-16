from datetime import date

import pytest
from openpyxl import load_workbook

from jobagent.tracker import Tracker, canonical_url, job_id


def _job(**over):
    base = {
        "company": "Acme",
        "title": "Software Engineer",
        "location": "Remote, US",
        "source": "greenhouse",
        "url": "https://boards.greenhouse.io/acme/jobs/123?gh_src=abc",
        "description": "Build things in Python.",
        "ats_type": "greenhouse",
    }
    return {**base, **over}


@pytest.fixture
def tracker(tmp_path):
    return Tracker(tmp_path / "tracker.xlsx", tmp_path / "jobs")


def test_canonical_url_drops_tracking():
    assert canonical_url("https://X.com/a/b/?utm=1#frag") == "https://x.com/a/b"
    assert job_id("https://x.com/a?x=1") == job_id("https://x.com/a/")


def test_dedupe_by_url_and_by_company_title(tracker):
    assert tracker.add_job(_job())
    assert not tracker.add_job(_job(url="https://boards.greenhouse.io/acme/jobs/123"))
    # Same role reposted under a different URL (e.g. from Adzuna) is still a duplicate.
    assert not tracker.add_job(_job(url="https://adzuna.com/x/999", source="adzuna", title="software  engineer"))
    # The same role listed in another city is one job, applied to once.
    assert not tracker.add_job(_job(url="https://adzuna.com/x/1000", source="adzuna", location="Boston, MA"))
    # Titles that differ only in punctuation ("–" vs "-") are the same job.
    assert tracker.add_job(_job(url="https://x.io/2", title="Software Engineer – Platform"))
    assert not tracker.add_job(_job(url="https://x.io/3", title="Software Engineer - Platform"))
    assert tracker.add_job(_job(url="https://boards.greenhouse.io/acme/jobs/124", title="Backend Engineer"))
    assert len(tracker.jobs) == 3


def test_round_trip_through_xlsx(tmp_path, tracker):
    tracker.add_job(_job())
    tracker.add_board("greenhouse", "Acme", "Acme")
    tracker.save()

    again = Tracker(tmp_path / "tracker.xlsx", tmp_path / "jobs")
    (row,) = again.jobs.values()
    assert row["company"] == "Acme" and row["status"] == "found"
    assert again.description(row["id"]) == "Build things in Python."
    assert again.active_boards()[0]["token"] == "acme"

    ws = load_workbook(tmp_path / "tracker.xlsx")["Jobs"]
    assert ws["A1"].value == "Date found"
    assert ws.data_validations.dataValidation, "status column should have a dropdown"


def test_applied_sets_follow_up(tracker):
    tracker.add_job(_job())
    (jid,) = tracker.jobs
    tracker.set_status(jid, "applied", on=date(2026, 11, 11))
    assert tracker.jobs[jid]["follow_up_date"] == "2026-11-18"
    assert tracker.already_applied("ACME", "software engineer")


def test_bad_status_rejected(tracker):
    tracker.add_job(_job())
    (jid,) = tracker.jobs
    with pytest.raises(ValueError):
        tracker.set_status(jid, "ghosted")


def test_saves_even_while_open_in_excel_and_sheet_catches_up(tmp_path, tracker):
    tracker.add_job(_job())
    tracker.save()
    lock = tmp_path / "~$tracker.xlsx"
    lock.write_text("")
    (jid,) = tracker.jobs
    tracker.set_status(jid, "applied")
    assert tracker.save() is False and tracker.excel_stale  # sheet untouched, nothing lost
    assert Tracker(tracker.path, tracker.jobs_dir).jobs[jid]["status"] == "applied"  # from the database
    lock.unlink()
    assert tracker.save() is True
    from openpyxl import load_workbook
    assert load_workbook(tracker.path)["My Applications"].max_row == 2


def test_first_run_imports_the_existing_spreadsheet(tmp_path, tracker):
    tracker.add_job(_job())
    tracker.save()
    tracker.db_path.unlink()  # as if the database didn't exist yet
    again = Tracker(tracker.path, tracker.jobs_dir)
    assert [r["company"] for r in again.jobs.values()] == ["Acme"]


def test_my_applications_sheet_lists_only_applied_and_keeps_edits(tmp_path, tracker):
    from openpyxl import load_workbook
    tracker.add_job(_job())
    tracker.add_job(_job(url="https://boards.greenhouse.io/acme/jobs/124", title="Backend Engineer"))
    applied = next(iter(tracker.jobs))
    tracker.set_status(applied, "applied")
    tracker.save()

    wb = load_workbook(tracker.path)
    assert wb.sheetnames[0] == "My Applications"
    ws = wb["My Applications"]
    rows = list(ws.iter_rows(min_row=2, values_only=True))
    assert len(rows) == 1 and rows[0][0] == "Acme" and rows[0][4] == "applied"
    assert ws.cell(row=2, column=8).hyperlink.target.startswith("https://")

    # Edit status and notes in Excel's short list; the next load keeps them.
    ws.cell(row=2, column=5, value="interview")
    ws.cell(row=2, column=7, value="Phone screen Monday")
    wb.save(tracker.path)
    again = Tracker(tracker.path, tracker.jobs_dir)
    assert again.jobs[applied]["status"] == "interview"
    assert again.jobs[applied]["notes"] == "Phone screen Monday"


def test_same_company_under_different_name_endings_is_one_job(tracker):
    assert tracker.add_job(_job(company="Jerry", title="Associate Data Scientist"))
    assert not tracker.add_job(_job(url="https://adzuna.com/x/7", company="Jerry.ai", title="Associate Data Scientist"))
    from jobagent.tracker import company_key
    assert company_key("Unity Technologies Corporation") == "unity" and company_key("Scale AI") == "scale"
