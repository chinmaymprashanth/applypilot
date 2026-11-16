"""Command line: python -m jobagent <command>."""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from datetime import date
from pathlib import Path

import typer

from .config import load_settings
from .profile import load_master_resume, load_profile, save_master_resume
from .tracker import STATUSES, Tracker

app = typer.Typer(no_args_is_help=True, add_completion=False)


def _setup_logging(logs_dir: Path) -> None:
    logs_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(logs_dir / f"{date.today().isoformat()}.log"), logging.StreamHandler()],
    )
    # httpx logs full request URLs, and Adzuna's key is a URL parameter.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _save_costs(s) -> dict[str, float]:
    """Add this command's Claude costs to data/costs.json; returns today's totals by step."""
    from .llm import METER

    return METER.flush(s.data_dir / "costs.json")


def _tracker() -> Tracker:
    s = load_settings()
    return Tracker(s.tracker_path, s.jobs_dir)


@app.command("import-resume")
def import_resume(pdf: Path = typer.Option(None, help="Resume PDF (default: the only PDF in data/)")) -> None:
    """Convert your resume PDF into data/master_resume.yaml for review."""
    from . import resume_import
    from .llm import make_client

    s = load_settings()
    _setup_logging(s.logs_dir)
    pdf = pdf or resume_import.find_resume_pdf(s.data_dir)
    profile = load_profile(s.profile_path)

    resume = resume_import.convert(make_client(s.anthropic_api_key), s.model, pdf)
    missing = resume.apply_fixed_dates(profile.fixed_dates)
    save_master_resume(resume, s.master_resume_path)

    typer.echo(f"Wrote {s.master_resume_path}")
    typer.echo(f"  {len(resume.experience)} roles, {len(resume.projects)} projects, {len(resume.all_skills())} skills")
    for company in missing:
        typer.secho(f"  No role matching '{company}' for fixed_dates in profile.yaml", fg="yellow")
    reworded = resume_import.unmatched_bullets(resume, resume_import.pdf_text(pdf))
    if reworded:
        typer.secho(f"  {len(reworded)} bullet(s) don't match the PDF text word for word; check them:", fg="yellow")
        for b in reworded:
            typer.echo(f"    - {b}")
    typer.echo("Review the file, fix anything wrong, then run: python -m jobagent check")


@app.command()
def check() -> None:
    """Validate profile.yaml, master_resume.yaml and the tracker."""
    s = load_settings()
    ok = True
    for label, fn, path in (
        ("profile", load_profile, s.profile_path),
        ("master resume", load_master_resume, s.master_resume_path),
    ):
        try:
            fn(path)
            typer.secho(f"OK   {label}: {path.name}", fg="green")
        except Exception as e:  # noqa: BLE001 - report every problem, not just the first
            ok = False
            typer.secho(f"FAIL {label}: {e}", fg="red")
    try:
        resume = load_master_resume(s.master_resume_path)
        pending = [p.name for p in resume.projects if not p.verified] + [
            f"{a.title}" + (f" ({a.to_confirm})" if a.to_confirm else "")
            for a in resume.achievements if not a.verified
        ]
        for item in pending:
            typer.secho(f"TODO unconfirmed, not used in applications: {item}", fg="yellow")
    except Exception:  # noqa: BLE001 - already reported above
        pass
    t = _tracker()
    typer.secho(f"OK   tracker: {len(t.jobs)} jobs, {len(t.boards)} boards", fg="green")
    if not s.anthropic_api_key:
        ok = False
        typer.secho("FAIL ANTHROPIC_API_KEY missing from .env", fg="red")
    if not (s.adzuna_app_id and s.adzuna_app_key):
        typer.secho("INFO Adzuna keys not set (optional extra source)", fg="cyan")
    raise typer.Exit(0 if ok else 1)


@app.command()
def status(
    job: str = typer.Argument(..., help="Job ID (from the tracker's ID column)"),
    new_status: str = typer.Argument(..., help=" | ".join(STATUSES)),
    note: str = typer.Option(None, help="Append a note"),
) -> None:
    """Update a job's status in tracker.xlsx."""
    t = _tracker()
    matches = [jid for jid in t.jobs if jid.startswith(job)]
    if len(matches) != 1:
        typer.secho(f"{'No' if not matches else 'More than one'} job matches '{job}'", fg="red")
        raise typer.Exit(1)
    jid = matches[0]
    t.set_status(jid, new_status)
    if note:
        old = t.jobs[jid].get("notes")
        t.update(jid, notes=f"{old}; {note}" if old else note)
    t.save()
    r = t.jobs[jid]
    typer.echo(f"{r['company']} - {r['title']}: {new_status}")


@app.command()
def search() -> None:
    """Find new jobs and add them to the tracker."""
    from .search.runner import run_search

    s = load_settings()
    _setup_logging(s.logs_dir)
    t = _tracker()
    report = run_search(s, t)
    t.save()
    _save_costs(s)
    sources = ", ".join(f"{k} {v}" for k, v in sorted(report.by_source.items())) or "none"
    typer.echo(f"Added {report.added} new jobs ({sources}).")
    typer.echo(
        f"Company boards: {report.boards_polled} checked, {report.new_boards} new, "
        f"{report.boards_gone} gone (dropped)."
    )
    typer.echo(
        f"New-grad list: {report.newgrad_listings} recent US tech roles. Alert emails read: {report.emails_read}. "
        f"Skipped {report.stale} older than {s.max_age_days} days."
    )
    for e in report.errors:
        typer.secho(f"  {e}", fg="yellow")
    return report


@app.command("gmail-login")
def gmail_login() -> None:
    """Sign in to Gmail (read-only) so search can read your LinkedIn and Indeed alerts."""
    from .search import gmail

    s = load_settings()
    try:
        gmail.login(s.data_dir / "gmail_credentials.json", s.data_dir / "gmail_token.json")
    except gmail.GmailAuthError as e:
        typer.secho(str(e), fg="red")
        raise typer.Exit(1)
    from googleapiclient.errors import HttpError

    svc = gmail.service(s.data_dir / "gmail_token.json")
    try:
        found = svc.users().messages().list(userId="me", q=gmail.alert_query(30), maxResults=50).execute()
    except HttpError as e:
        typer.secho(f"Signed in, but Gmail refused: {gmail.explain_http_error(e)}", fg="red")
        raise typer.Exit(1)
    typer.echo(f"Signed in. {len(found.get('messages', []))} job alert emails in the last 30 days.")


@app.command("add-board")
def add_board(url: str = typer.Argument(..., help="Any job link on Greenhouse, Lever or Ashby")) -> None:
    """Optional: add a company's job board by pasting one of its job links."""
    from .search.common import detect_board

    board = detect_board(url)
    if not board:
        typer.secho("Not a Greenhouse, Lever or Ashby job link.", fg="red")
        raise typer.Exit(1)
    t = _tracker()
    added = t.add_board(*board)
    t.save()
    typer.echo(f"{'Added' if added else 'Already tracking'} {board[0]} board '{board[1]}'.")


STAGE_LABELS = {
    "age": "older than the age limit",
    "link": "no usable apply link (Adzuna only)",
    "1 us": "1. not in the US",
    "2 tech": "2. not a tech role",
    "3 experience": "3. needs 3+ years / senior / clearance",
    "4 career": "4. data entry, internship or no growth",
    "5 fit": "5. weak match with your skills",
    "applied": "already applied",
}


@contextmanager
def _enricher(s, t: Tracker, client):
    """An Enricher with an invisible browser, a web client, and every job board already known by company."""
    import httpx
    from playwright.sync_api import sync_playwright

    from .enrich import Enricher
    from .tracker import company_key

    boards: dict[str, list[tuple[str, str]]] = {}
    for (ats, token), b in t.boards.items():
        if b.get("failures", 0) < 3:
            boards.setdefault(company_key(b.get("company") or token), []).append((ats, token))
    with sync_playwright() as pw, httpx.Client(timeout=30, follow_redirects=True,
                                               headers={"User-Agent": "jobagent/0.1"}) as http:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.set_default_timeout(20000)
        e = Enricher(client, s.model, page, http, boards, s.data_dir / "company_careers.json")
        try:
            yield e
        finally:
            e.save()
            for ats, token, company in e.new_boards:
                if ats in ("greenhouse", "lever", "ashby"):
                    t.add_board(ats, token, company=company)
            browser.close()


@app.command()
def score() -> None:
    """Run the funnel (US, tech, <3 years, real career role, fit) and shortlist the top jobs."""
    from .llm import make_client
    from .score.funnel import claude_assessor, claude_batch_assessor, run_funnel

    s = load_settings()
    _setup_logging(s.logs_dir)
    t = _tracker()
    client = make_client(s.anthropic_api_key)
    resume = load_master_resume(s.master_resume_path)
    assess = claude_assessor(client, s.model, resume)
    progress = lambda m: typer.echo(m)  # noqa: E731
    # Haiku screens leniently, Sonnet reads what passes; both through the half-price Batch API when enabled.
    screen = claude_batch_assessor(client, s.screen_model, resume, lenient=True, progress=progress) \
        if s.use_batch else None
    assess_many = claude_batch_assessor(client, s.model, resume, progress=progress) if s.use_batch else None
    if s.use_batch:
        typer.echo("Scoring through the Batch API (half price); this can take a while...")
    with _enricher(s, t, client) as enricher:
        r = run_funnel(t, assess, top_n=s.top_n, max_age_days=s.max_age_days, fresh_hours=s.fresh_hours,
                       enrich=enricher.enrich, enrich_limit=s.enrich_limit, screen=screen, assess_many=assess_many,
                       top_n_intern=s.top_n_intern)
    t.save()
    _save_costs(s)

    typer.echo(f"Checked {r.considered} new jobs. First screen dropped {r.screened_out}; "
               f"{r.second_read} read carefully.")
    for stage, label in STAGE_LABELS.items():
        if r.dropped.get(stage):
            typer.echo(f"  dropped {r.dropped[stage]:>3}  {label}")
    typer.echo(f"  passed  {r.passed:>3}  scored on fit")
    typer.echo(f"\nTop {len(r.shortlisted)}:")
    for i, j in enumerate(r.shortlisted, 1):
        flag = " (stretch)" if j.get("stretch") else ""
        typer.echo(f"{i:>2}. {j['score']:>5}  {j['company']} - {j['title']}{flag}")
        typer.echo(f"           {j.get('fit') or ''}")
    if r.enriched or r.not_found:
        typer.echo(f"\nFull postings found for {r.enriched} jobs; {r.not_found} had no usable employer posting.")
    for e in r.errors:
        typer.secho(f"  {e}", fg="yellow")
    return r


@app.command()
def tailor(limit: int = typer.Option(None, help="Only prepare this many jobs")) -> None:
    """Write a tailored one-page resume and cover letter for each shortlisted job."""
    from .llm import make_client
    from .tailor.render import Renderer
    from .tailor.research import research_company
    from .tailor.runner import run_tailor
    from .tailor.writer import claude_generator, claude_patcher

    s = load_settings()
    _setup_logging(s.logs_dir)
    t = _tracker()
    client = make_client(s.anthropic_api_key)
    master = load_master_resume(s.master_resume_path)
    generate = claude_generator(client, s.model, master)

    def research(company: str, title: str) -> list[str]:
        return research_company(client, s.model, company, title, s.data_dir / "companies")

    with Renderer(s.templates_dir) as renderer:
        r = run_tailor(t, master, load_profile(s.profile_path), generate, renderer, s.output_dir, limit=limit,
                       research=research, patch=claude_patcher(client, s.model, master))
    t.save()
    _save_costs(s)
    typer.echo(f"Prepared {len(r.prepared)} applications:")
    for j in r.prepared:
        typer.echo(f"  {j['company']} - {j['title']}  [{j['track']}, keywords: {j['coverage']}% of the job's, "
                   f"{j['covered']}% of the ones you have]")
        if j["missing"]:
            typer.echo(f"      you have these but they didn't fit: {', '.join(j['missing'][:6])}")
        if j["unsupported"]:
            typer.echo(f"      the job wants these, you don't list them: {', '.join(j['unsupported'][:6])}")
        typer.echo(f"      {j['folder']}")
    for e in r.errors:
        typer.secho(f"  {e}", fg="yellow")
    return r


@app.command()
def rerender() -> None:
    """Rebuild PDFs with the current templates (no Claude cost). Already-submitted ones are left as sent."""
    from .tailor.render import Renderer
    from .tailor.runner import rerender as rerender_folder

    s = load_settings()
    master, profile = load_master_resume(s.master_resume_path), load_profile(s.profile_path)
    t = _tracker()
    sent = {jid for jid, r in t.jobs.items() if r.get("status") in ("applied", "interview", "rejected", "offer")}
    # Documents already sent to an employer are kept exactly as they were sent.
    folders = [f.parent for f in sorted(s.output_dir.glob("*/*/job.json"))
               if json.loads(f.read_text())["job"].get("id") not in sent]
    with Renderer(s.templates_dir) as r:
        for folder in folders:
            pages = rerender_folder(folder, master, profile, r)
            typer.echo(f"  {folder.parent.name}/{folder.name}" + ("" if pages == 1 else f"  ({pages} pages!)"))


def _apply(limit: int | None) -> None:
    import yaml

    from .apply.runner import claude_drafter, run_apply
    from .llm import make_client
    from .tailor.research import research_company
    from .tailor.runner import Preparer
    from .tailor.writer import claude_generator, claude_patcher

    s = load_settings()
    _setup_logging(s.logs_dir)
    t = _tracker()
    profile, master = load_profile(s.profile_path), load_master_resume(s.master_resume_path)
    answers = yaml.safe_load(s.answers_path.read_text()) or {}
    client = make_client(s.anthropic_api_key)
    drafter = claude_drafter(client, s.model, master, profile)

    def research(company: str, title: str) -> list[str]:
        return research_company(client, s.model, company, title, s.data_dir / "companies")

    preparer = Preparer(t, master, profile, claude_generator(client, s.model, master), s.output_dir, s.templates_dir,
                        research, claude_patcher(client, s.model, master))
    counts = run_apply(t, profile, answers, master, s.browser_profile_dir, drafter, preparer=preparer,
                       echo=typer.echo, limit=limit)
    _save_costs(s)
    typer.echo(f"\nDone: {counts['applied']} applied, {counts['skipped']} skipped, {counts['later']} left for later. "
               f"Tracker: {s.tracker_path}")


@app.command()
def apply(limit: int = typer.Option(None, help="Only open this many applications")) -> None:
    """Choose from your shortlist, then each chosen job gets its resume and letter and its form filled.
    You click Submit."""
    _apply(limit)


@app.command()
def inbox(days: int = typer.Option(30, help="How far back to look")) -> None:
    """Read employer replies in Gmail (read-only) and update each application's status and notes."""
    from googleapiclient.errors import HttpError

    from . import inbox as inbox_mod
    from .llm import make_client
    from .search import gmail

    s = load_settings()
    _setup_logging(s.logs_dir)
    t = _tracker()
    try:
        svc = gmail.service(s.data_dir / "gmail_token.json")
        r = inbox_mod.check_inbox(t, svc, inbox_mod.claude_classifier(make_client(s.anthropic_api_key), s.model),
                                  s.data_dir / "inbox_seen.json", days=days)
    except gmail.GmailAuthError as e:
        typer.secho(f"Inbox not checked: {e}", fg="yellow")
        return
    except HttpError as e:
        typer.secho(f"Inbox not checked: {gmail.explain_http_error(e)}", fg="yellow")
        return
    t.save()
    _save_costs(s)
    typer.echo(f"Checked {r.checked} new hiring emails; {r.read} were about your applications.")
    for u in r.updates:
        typer.echo(f"  {u}")
    for a in r.actions:
        typer.secho(f"  TO DO: {a}", fg="cyan", bold=True)
    for e in r.errors:
        typer.secho(f"  {e}", fg="yellow")
    return r


@app.command()
def insights() -> None:
    """Which kinds of applications lead to interviews (by track, source, hiring site and score)."""
    from .insights import insights as report

    for line in report(_tracker()):
        typer.echo(line)


@app.command()
def prepare() -> None:
    """Check replies, find jobs and pick today's best: everything except the browser. Safe to schedule.
    Ends with a summary, also saved as output/<date>/summary.md. Resumes are written when you apply."""
    from .summary import build_summary, save_summary

    s = load_settings()
    typer.secho("\n== 1. inbox: replies from employers ==", bold=True)
    inbox_report = inbox(days=30)
    typer.secho("\n== 2. search: new jobs from every source ==", bold=True)
    search_report = search()
    typer.secho("\n== 3. score: your funnel, full postings, top picks ==", bold=True)
    funnel_report = score()
    costs = _save_costs(s)
    # Resumes and cover letters are written at apply time, only for the jobs you choose.
    text = build_summary(_tracker(), search_report, funnel_report, None, inbox_report,
                         max_age_days=s.max_age_days, costs=costs)
    path = save_summary(text, s.output_dir)
    typer.secho("\n" + "=" * 70, bold=True)
    typer.echo(text)
    typer.secho(f"\n(Saved to {path})", fg="cyan")


@app.command()
def run() -> None:
    """Full daily run: prepare, then choose and apply (resumes are written for the jobs you choose)."""
    prepare()
    typer.secho("\n== apply ==", bold=True)
    _apply(None)


LAUNCH_LABEL = "com.jobagent.prepare"


@app.command()
def schedule(
    at: str = typer.Option("07:30", help="Time of day, HH:MM (24-hour)"),
    off: bool = typer.Option(False, "--off", help="Remove the daily schedule"),
) -> None:
    """Run 'prepare' every day at a set time (macOS launchd). Applying stays manual: run 'apply' when ready."""
    import os
    import plistlib
    import shutil
    import subprocess

    s = load_settings()
    plist = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_LABEL}.plist"
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", domain, str(plist)], capture_output=True)
    if off:
        plist.unlink(missing_ok=True)
        typer.echo("Daily schedule removed.")
        return
    hour, minute = (int(x) for x in at.split(":"))
    uv = shutil.which("uv")
    if not uv:
        typer.secho("Can't find 'uv' on PATH.", fg="red")
        raise typer.Exit(1)
    s.logs_dir.mkdir(parents=True, exist_ok=True)
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_bytes(plistlib.dumps({
        "Label": LAUNCH_LABEL,
        "ProgramArguments": [uv, "run", "--project", str(s.root), "python", "-m", "jobagent", "prepare"],
        "WorkingDirectory": str(s.root),
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        "StandardOutPath": str(s.logs_dir / "daily.log"),
        "StandardErrorPath": str(s.logs_dir / "daily.log"),
    }))
    subprocess.run(["launchctl", "bootstrap", domain, str(plist)], check=True)
    typer.echo(f"Scheduled: search, score and tailor every day at {hour:02d}:{minute:02d}. "
               f"Output: {s.logs_dir / 'daily.log'}. Then run: python -m jobagent apply")


@app.command("eval-label")
def eval_label(n: int = typer.Option(40, help="How many jobs to label this time")) -> None:
    """Build your answer key: say whether you'd apply to real jobs (about 15 minutes for 40)."""
    from .evals import label_jobs

    s = load_settings()
    done = label_jobs(_tracker(), s.data_dir / "eval" / "jobs.jsonl", n, input, typer.echo)
    typer.echo(f"\nSaved {done} labels to {s.data_dir / 'eval' / 'jobs.jsonl'}. Run: python -m jobagent eval")


@app.command("eval")
def eval_cmd(no_screen: bool = typer.Option(False, "--no-screen", help="Test Sonnet alone, without the Haiku screen")) -> None:
    """Run the funnel on your labelled jobs and report what it gets wrong. Doesn't change the tracker."""
    from .evals import load_labels, run_eval
    from .llm import METER, make_client
    from .score.funnel import SCREEN_NOTE, claude_assessor

    s = load_settings()
    labels = load_labels(s.data_dir / "eval" / "jobs.jsonl")
    if not labels:
        typer.echo("No labels yet. Run: python -m jobagent eval-label")
        raise typer.Exit(1)
    client = make_client(s.anthropic_api_key)
    resume = load_master_resume(s.master_resume_path)
    assess = claude_assessor(client, s.model, resume)
    screen = None
    if not no_screen:
        from .llm import parse
        from .score.funnel import SYSTEM, Assessment, candidate_brief, posting

        system = SYSTEM.format(candidate=candidate_brief(resume)) + SCREEN_NOTE

        def screen(job, desc):
            return parse(client, model=s.screen_model, schema=Assessment, system=system, content=posting(job, desc),
                         max_tokens=4000, effort="low", cache_system=True, step="eval")

    report = run_eval(_tracker(), labels, assess, screen)
    cost = sum(METER.dollars.values())
    _save_costs(s)
    typer.echo(f"Eval on {report.yes + report.no} labelled jobs "
               f"({'Sonnet only' if no_screen else 'Haiku screen + Sonnet'}), cost ${cost:.2f}:")
    for line in report.lines():
        typer.echo(line)
