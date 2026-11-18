"""The job tracker. Records live in a small SQLite database (data/jobagent.db); tracker.xlsx is the view
you read, re-exported after every change.

The Excel file has three sheets: "My Applications" (only jobs you applied to; Status, Follow up by and
Latest update can be edited there and are read back), "Jobs" (everything found) and "Boards" (company job
boards that searches poll). If Excel has the file open, the database is still saved and the sheet catches up
on the next save. Full job descriptions live in data/jobs/<id>.json because they are too long for cells.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
from contextlib import closing
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation

log = logging.getLogger(__name__)

STATUSES = (
    "found", "shortlisted", "prepared", "filled", "applied",
    "skipped", "interview", "rejected", "offer",
)

# (key, header, width)
JOB_COLUMNS: list[tuple[str, str, int]] = [
    ("date_found", "Date found", 12),
    ("company", "Company", 22),
    ("title", "Title", 34),
    ("location", "Location", 20),
    ("work_mode", "Work mode", 10),
    ("kind", "Type", 12),
    ("source", "Source", 12),
    ("posted_at", "Posted", 18),
    ("score", "Score", 7),
    ("tier", "Tier", 5),
    ("stretch", "Stretch", 8),
    ("status", "Status", 12),
    ("fit", "Why it fits", 45),
    ("track", "Track", 12),
    ("keyword_match", "Keyword match", 16),
    ("resume_version", "Resume version", 16),
    ("applied_date", "Applied date", 12),
    ("follow_up_date", "Follow-up date", 14),
    ("apply_url", "Apply URL", 40),
    ("notes", "Notes", 30),
    ("id", "ID", 18),
    ("ats_type", "ATS", 10),
    ("filtered_reason", "Filtered reason", 24),
    ("dedupe_key", "Dedupe key", 10),
]
JOB_KEYS = [k for k, _, _ in JOB_COLUMNS]

# The short list you track by hand: only jobs you applied to. Status, follow-up and notes edited here are kept.
APPLIED_SHEET = "My Applications"
APPLIED_COLUMNS: list[tuple[str, str, int]] = [
    ("company", "Company", 24),
    ("title", "Role", 40),
    ("location", "Location", 22),
    ("applied_date", "Applied", 12),
    ("status", "Status", 12),
    ("follow_up_date", "Follow up by", 13),
    ("notes", "Latest update", 60),
    ("apply_url", "Job link", 12),
    ("id", "ID", 18),
]
APPLIED_KEYS = [k for k, _, _ in APPLIED_COLUMNS]
APPLIED_EDITABLE = ("status", "follow_up_date", "notes")

BOARD_COLUMNS: list[tuple[str, str, int]] = [
    ("ats_type", "ATS", 12),
    ("token", "Board token", 28),
    ("company", "Company", 24),
    ("first_seen", "First seen", 22),
    ("last_polled", "Last polled", 22),
    ("failures", "Failures", 9),
]
BOARD_KEYS = [k for k, _, _ in BOARD_COLUMNS]

CLOSED_STATUSES = {"applied", "interview", "rejected", "offer"}


class TrackerLockedError(RuntimeError):
    pass


# Query parameters that identify the job itself (e.g. Indeed's jk) rather than tracking.
ID_PARAMS = ("jk", "gh_jid")


def canonical_url(url: str) -> str:
    """Strip tracking params, fragment and trailing slash so they don't defeat dedupe."""
    parts = urlsplit(url.strip())
    path = parts.path.rstrip("/") or "/"
    keep = [(k, v) for k, v in parse_qsl(parts.query) if k in ID_PARAMS]
    return urlunsplit((parts.scheme.lower() or "https", parts.netloc.lower(), path, urlencode(keep), ""))


def job_id(url: str) -> str:
    return hashlib.sha256(canonical_url(url).encode()).hexdigest()[:16]


def _norm(text: str | None) -> str:
    """Lowercase, punctuation-free: "Analyst – Marketing" and "Analyst - Marketing" are the same title."""
    return " ".join(re.sub(r"[^\w\s]", " ", (text or "").lower()).split())


# Name endings that differ between sources ("Jerry" vs "Jerry.ai", "Unity Technologies Corporation").
COMPANY_SUFFIXES = re.compile(r"\b(inc|llc|ltd|corp|corporation|co|company|technologies|technology|labs|hq|ai|io|"
                              r"com|group|holdings|plc|gmbh|n a)\b")


def company_key(company: str | None) -> str:
    return " ".join(COMPANY_SUFFIXES.sub(" ", _norm(company)).split()) or _norm(company)


def dedupe_key(company: str, title: str) -> str:
    """The same role posted in several cities, or by two sources, is one job: you apply to it once."""
    return f"{company_key(company)}|{_norm(title)}"


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Tracker:
    """Loads tracker.xlsx into memory; call save() to write it back."""

    def __init__(self, path: Path, jobs_dir: Path, db_path: Path | None = None):
        self.path = path
        self.jobs_dir = jobs_dir
        self.db_path = db_path or jobs_dir.parent / "jobagent.db"
        self.jobs: dict[str, dict[str, Any]] = {}
        self.boards: dict[tuple[str, str], dict[str, Any]] = {}
        self.excel_stale = False  # True when the last save couldn't update tracker.xlsx (open in Excel)
        if self.db_path.exists():
            self._load_db()
            self._import_sheet_edits()
        elif path.exists():
            self._load()  # first run after the switch to SQLite: import the spreadsheet
        self._keys = {dedupe_key(r["company"], r["title"]) for r in self.jobs.values()}

    # ---- file io -------------------------------------------------------

    def _lock_file(self) -> Path:
        return self.path.with_name(f"~${self.path.name}")

    def _load(self) -> None:
        wb = load_workbook(self.path)
        # Columns are matched by header, so files saved before a column was added still load correctly.
        by_header = {h: k for k, h, _ in JOB_COLUMNS}
        rows = wb["Jobs"].iter_rows(values_only=True)
        keys = [by_header.get(h) for h in next(rows, ())]
        for row in rows:
            rec = {k: None for k in JOB_KEYS}
            rec.update({k: v for k, v in zip(keys, row) if k})
            if rec.get("id"):
                self.jobs[rec["id"]] = rec
        self._apply_sheet_edits(wb)
        if "Boards" in wb.sheetnames:
            for row in wb["Boards"].iter_rows(min_row=2, values_only=True):
                rec = dict(zip(BOARD_KEYS, row))
                if rec.get("token"):
                    rec["failures"] = int(rec.get("failures") or 0)
                    self.boards[(rec["ats_type"], rec["token"])] = rec

    def _apply_sheet_edits(self, wb) -> None:
        """Edits made in "My Applications" (status, follow-up date, latest update) win."""
        if APPLIED_SHEET not in wb.sheetnames:
            return
        for row in wb[APPLIED_SHEET].iter_rows(min_row=2, values_only=True):
            rec = dict(zip(APPLIED_KEYS, row))
            if rec.get("id") in self.jobs:
                job = self.jobs[rec["id"]]
                job.update({k: rec[k] for k in APPLIED_EDITABLE if rec.get(k) != job.get(k)
                            and not (k == "status" and rec[k] not in STATUSES)})

    # ---- database ------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.db_path)
        con.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
        con.execute("CREATE TABLE IF NOT EXISTS boards (ats TEXT, token TEXT, data TEXT NOT NULL, PRIMARY KEY (ats, token))")
        con.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        return con

    def _load_db(self) -> None:
        with closing(self._connect()) as con:
            for jid, data in con.execute("SELECT id, data FROM jobs"):
                self.jobs[jid] = {k: None for k in JOB_KEYS} | json.loads(data)
            for ats, token, data in con.execute("SELECT ats, token, data FROM boards"):
                self.boards[(ats, token)] = json.loads(data)
            row = con.execute("SELECT value FROM meta WHERE key = 'excel_exported'").fetchone()
            self._exported_mtime = float(row[0]) if row else 0.0

    def _import_sheet_edits(self) -> None:
        """Pick up what you changed in tracker.xlsx since jobagent last wrote it."""
        if not self.path.exists() or self.path.stat().st_mtime <= getattr(self, "_exported_mtime", 0.0):
            return
        try:
            self._apply_sheet_edits(load_workbook(self.path, read_only=True))
        except Exception as e:  # noqa: BLE001 - a half-saved or damaged sheet shouldn't stop jobagent
            log.warning("couldn't read your edits from %s: %s", self.path.name, e)

    def _save_db(self) -> None:
        with closing(self._connect()) as con, con:
            con.execute("DELETE FROM jobs")
            con.executemany("INSERT INTO jobs VALUES (?, ?)",
                            [(jid, json.dumps(r, default=str, ensure_ascii=False)) for jid, r in self.jobs.items()])
            con.execute("DELETE FROM boards")
            con.executemany("INSERT INTO boards VALUES (?, ?, ?)",
                            [(a, t, json.dumps(b, default=str)) for (a, t), b in self.boards.items()])

    def save(self) -> bool:
        """Save the records, then export tracker.xlsx. Returns False if Excel had the file open (the records are
        still saved; the sheet catches up on the next save)."""
        self._save_db()
        from .cloud import mark_dirty_if_synced

        mark_dirty_if_synced(self.jobs_dir.parent)  # so the next download from AWS merges instead of overwriting
        if self._lock_file().exists():
            self.excel_stale = True
            log.warning("%s is open in Excel; your data is saved and the sheet will update next time", self.path.name)
            return False
        self._export_excel()
        self.excel_stale = False
        with closing(self._connect()) as con, con:
            con.execute("INSERT OR REPLACE INTO meta VALUES ('excel_exported', ?)", (str(self.path.stat().st_mtime),))
        self._exported_mtime = self.path.stat().st_mtime
        return True

    def _export_excel(self) -> None:
        wb = Workbook()
        ws = wb.active
        ws.title = "Jobs"
        self._write_sheet(ws, JOB_COLUMNS, self._sorted_jobs())
        dv = DataValidation(type="list", formula1='"' + ",".join(STATUSES) + '"', allow_blank=False)
        ws.add_data_validation(dv)
        status_col = ws.cell(row=1, column=JOB_KEYS.index("status") + 1).column_letter
        dv.add(f"{status_col}2:{status_col}5000")
        dedupe_col = ws.cell(row=1, column=JOB_KEYS.index("dedupe_key") + 1).column_letter
        ws.column_dimensions[dedupe_col].hidden = True
        self._write_sheet(wb.create_sheet("Boards"), BOARD_COLUMNS, list(self.boards.values()))
        self._write_applications(wb.create_sheet(APPLIED_SHEET, 0), dv_formula=dv.formula1)
        wb.active = 0

        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp.xlsx")
        wb.save(tmp)
        os.replace(tmp, self.path)

    def _write_applications(self, ws, dv_formula: str) -> None:
        rows = sorted((r for r in self.jobs.values() if r.get("status") in CLOSED_STATUSES),
                      key=lambda r: r.get("applied_date") or "", reverse=True)
        self._write_sheet(ws, APPLIED_COLUMNS, rows)
        link_col = APPLIED_KEYS.index("apply_url") + 1
        for i, rec in enumerate(rows, start=2):
            cell = ws.cell(row=i, column=link_col)
            if rec.get("apply_url"):
                cell.value, cell.hyperlink, cell.style = "Open", rec["apply_url"], "Hyperlink"
        for col in ("notes", "title"):
            for i in range(2, len(rows) + 2):
                ws.cell(row=i, column=APPLIED_KEYS.index(col) + 1).alignment = Alignment(wrap_text=True, vertical="top")
        dv = DataValidation(type="list", formula1=dv_formula, allow_blank=False)
        ws.add_data_validation(dv)
        status_col = ws.cell(row=1, column=APPLIED_KEYS.index("status") + 1).column_letter
        dv.add(f"{status_col}2:{status_col}2000")
        ws.column_dimensions[ws.cell(row=1, column=APPLIED_KEYS.index("id") + 1).column_letter].hidden = True

    @staticmethod
    def _write_sheet(ws, columns, rows) -> None:
        ws.append([h for _, h, _ in columns])
        for cell in ws[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="305496")
        for i, (_, _, width) in enumerate(columns, start=1):
            ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = width
        ws.freeze_panes = "A2"
        keys = [k for k, _, _ in columns]
        for rec in rows:
            ws.append([rec.get(k) for k in keys])
        ws.auto_filter.ref = ws.dimensions

    def _sorted_jobs(self) -> list[dict[str, Any]]:
        return sorted(
            self.jobs.values(),
            key=lambda r: (r.get("date_found") or "", r.get("score") or 0),
            reverse=True,
        )

    # ---- jobs ----------------------------------------------------------

    def add_job(self, job: dict[str, Any]) -> bool:
        """Add a job unless its URL or its company+title is already tracked.

        Returns True when the job is new. The description is written to data/jobs/<id>.json.
        """
        jid = job_id(job["url"])
        key = dedupe_key(job["company"], job["title"])
        if jid in self.jobs or key in self._keys:
            return False
        self.jobs[jid] = {
            "id": jid,
            "date_found": date.today().isoformat(),
            "company": job["company"],
            "title": job["title"],
            "location": job.get("location"),
            "work_mode": job.get("work_mode") or "unknown",
            "kind": job.get("kind"),
            "source": job["source"],
            "posted_at": job.get("posted_at"),
            "status": "found",
            "apply_url": job.get("apply_url") or job["url"],
            "ats_type": job.get("ats_type") or "other",
            "dedupe_key": key,
        }
        self._keys.add(key)
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        detail = {**job, "id": jid, "first_seen": now_iso()}
        (self.jobs_dir / f"{jid}.json").write_text(json.dumps(detail, indent=2, ensure_ascii=False))
        return True

    def save_detail(self, jid: str, **fields: Any) -> None:
        """Merge extra data (e.g. Claude's assessment) into data/jobs/<id>.json."""
        path = self.jobs_dir / f"{jid}.json"
        detail = json.loads(path.read_text()) if path.exists() else {"id": jid}
        detail.update(fields)
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(detail, indent=2, ensure_ascii=False))

    def dedupe_keys(self) -> set[str]:
        return set(self._keys)

    def description(self, jid: str) -> str:
        path = self.jobs_dir / f"{jid}.json"
        if not path.exists():
            return ""
        return json.loads(path.read_text()).get("description") or ""

    def with_status(self, *statuses: str) -> list[dict[str, Any]]:
        rows = [r for r in self.jobs.values() if r.get("status") in statuses]
        return sorted(rows, key=lambda r: r.get("score") or 0, reverse=True)

    def update(self, jid: str, **fields: Any) -> None:
        if "status" in fields and fields["status"] not in STATUSES:
            raise ValueError(f"unknown status {fields['status']!r}; expected one of {STATUSES}")
        self.jobs[jid].update(fields)

    def set_status(self, jid: str, status: str, on: date | None = None) -> None:
        fields: dict[str, Any] = {"status": status}
        if status == "applied":
            day = on or date.today()
            fields["applied_date"] = day.isoformat()
            fields["follow_up_date"] = (day + timedelta(days=7)).isoformat()
        self.update(jid, **fields)

    def already_applied(self, company: str, title: str) -> bool:
        c, t = company_key(company), _norm(title)
        return any(
            r.get("status") in CLOSED_STATUSES and company_key(r.get("company")) == c and _norm(r.get("title")) == t
            for r in self.jobs.values()
        )

    # ---- boards --------------------------------------------------------

    def add_board(self, ats_type: str, token: str, company: str | None = None) -> bool:
        # Greenhouse/Lever/Ashby/Workday names ignore case; others (e.g. Oracle sites like "CX_1") are kept as written.
        if ats_type in ("greenhouse", "lever", "ashby", "workday"):
            token = token.lower()
        key = (ats_type, token)
        if key in self.boards:
            return False
        self.boards[key] = {
            "ats_type": ats_type, "token": token, "company": company,
            "first_seen": now_iso(), "last_polled": None, "failures": 0,
        }
        return True

    def active_boards(self, max_failures: int = 3) -> list[dict[str, Any]]:
        rows = [b for b in self.boards.values() if b["failures"] < max_failures]
        return sorted(rows, key=lambda b: b.get("last_polled") or "")
