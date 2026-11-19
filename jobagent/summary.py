"""The end-of-prepare summary: what was found where, how the top jobs were picked, and what's ready to apply to.

Printed in the terminal and saved as output/<date>/summary.md.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from .apply.runner import OWN_BROWSER
from .insights import insights
from .score.funnel import WEIGHTS
from .search.common import ats_of
from .tracker import Tracker

SOURCE_NAMES = {"greenhouse": "Company boards (Greenhouse)", "lever": "Company boards (Lever)",
                "ashby": "Company boards (Ashby)", "workday": "Company sites (Workday)",
                "smartrecruiters": "Company sites (SmartRecruiters)", "workable": "Company sites (Workable)",
                "oracle": "Company sites (Oracle)", "amazon": "Amazon careers", "eightfold": "Netflix careers", "newgrad_list": "New-grad list (other company sites)",
                "intern_list": "Internship list (Summer/Fall 2027)",
                "adzuna": "Adzuna search", "gmail_linkedin": "LinkedIn alerts", "gmail_indeed": "Indeed alerts",
                "gmail_wellfound": "Wellfound alerts", "gmail_builtin": "Built In alerts", "gmail_yc": "YC alerts",
                "gmail_glassdoor": "Glassdoor alerts", "gmail_ziprecruiter": "ZipRecruiter alerts",
                "gmail_dice": "Dice alerts", "gmail_handshake": "Handshake alerts", "gmail_simplify": "Simplify alerts"}
STAGES = {"age": "older than 7 days", "1 us": "not in the US", "2 tech": "not a tech role",
          "3 experience": "needs 3+ years / senior / clearance", "4 career": "data entry, internship or no growth",
          "5 fit": "weak match with your skills", "applied": "already applied",
          "link": "no posting on the employer's own site"}


def build_summary(tracker: Tracker, search=None, funnel=None, tailored=None, inbox=None,
                  today: date | None = None, max_age_days: int = 7, costs: dict[str, float] | None = None) -> str:
    today = today or date.today()
    out = [f"# jobagent daily summary, {today.isoformat()}", ""]

    if inbox is not None:
        out += ["## 1. Replies from employers", f"Checked {inbox.checked} new hiring emails; "
                f"{inbox.read} were about your applications."]
        out += [f"- {u}" for u in inbox.updates] + [f"- **TO DO:** {a}" for a in inbox.actions] + [""]

    if search is not None:
        out += ["## 2. Jobs found today",
                f"New jobs added: **{search.added}** (US, tech-looking, posted in the last {max_age_days} days, "
                "not seen before).", "", "| Source | New jobs |", "|---|---|"]
        for src, n in sorted(search.by_source.items(), key=lambda kv: -kv[1]):
            out.append(f"| {SOURCE_NAMES.get(src, src)} | {n} |")
        out += ["", f"- Company boards checked: {search.boards_polled} ({search.new_boards} new, "
                f"{search.boards_gone} closed and dropped)",
                f"- New-grad list: {search.newgrad_listings} recent roles; internship list: "
                f"{search.intern_listings}; alert emails read: {search.emails_read}",
                f"- Skipped as older than {max_age_days} days: {search.stale}"]
        lane = getattr(search, "slow_lane", None)
        if lane:
            out.append(f"- Workday slow lane (one site at a time): {lane['checked']} sites checked, "
                       f"{lane['jobs']} new US tech jobs, {lane['throttled']} throttled, "
                       f"{lane['left for next run']} left for the next run")
        if search.hiccups:
            out.append("- Sites that couldn't be checked today (tried again tomorrow): "
                       + ", ".join(f"{n} {kind}" for kind, n in sorted(search.hiccups.items(), key=lambda kv: -kv[1])))
        if search.errors:
            out.append(f"- Other hiccups: {len(search.errors)}, e.g. {search.errors[0][:100]}")
        out.append("")

    if funnel is not None:
        out += ["## 3. How the top jobs were picked",
                f"**{funnel.considered}** new jobs went through your funnel. A quick lenient first screen (Claude Haiku) "
                f"dropped {funnel.screened_out} that clearly failed a check; Claude Sonnet then read "
                f"**{funnel.second_read}** carefully:", ""]
        out += [f"- dropped {n}: {STAGES.get(stage, stage)}" for stage, n in sorted(funnel.dropped.items())]
        out += [f"- **passed every check: {funnel.passed}**", "",
                f"Promising jobs with only a job-board link or a short description were traced to the employer's "
                f"own site and read in full before competing: **{funnel.enriched} confirmed**, "
                f"{funnel.not_found} not found (dropped).", "",
                "Score out of 100 = " + " + ".join(f"{k} {v}" for k, v in WEIGHTS.items())
                + " (fit = skills match; level = entry-level fit; recency = posted within 24 hours; "
                  "company = employer quality and growth; odds = realistic interview chance).", ""]

    if funnel is not None and tailored is None:
        waiting = tracker.with_status("shortlisted")
        out += [f"## 4. Your shortlists ({len(funnel.shortlisted)} new today)",
                "Run `apply` to choose; only the jobs you pick get a resume and cover letter.", ""]
        for kind, title in (("full_time", "Full-time"), ("intern_pt", "Internships & part-time "
                                                                       "(resume shows your UMass Amherst MS)")):
            jobs = [j for j in waiting if (j.get("kind") or "full_time") == kind]
            out += [f"### {title}: {len(jobs)}", "",
                    "| # | Score | Company & role | Posted | How you apply | Why it fits | Link |",
                    "|---|---|---|---|---|---|---|"]
            for i, j in enumerate(jobs, 1):
                detail = json.loads((tracker.jobs_dir / f"{j['id']}.json").read_text())
                parts = detail.get("score_parts") or {}
                breakdown = " / ".join(f"{parts[k]:g}" for k in WEIGHTS if k in parts)
                how = "your own Chrome" if ats_of(j["apply_url"]) in OWN_BROWSER else "auto-fill"
                out.append(f"| {i} | **{j['score']}** <br><sub>{breakdown}</sub> | {j['company']}: {j['title']} | "
                           f"{str(j.get('posted_at') or '')[:10]} | {how} | {(j.get('fit') or '')[:140]} | "
                           f"[open]({j['apply_url']}) |")
            out.append("")
        out += ["<sub>Score parts: " + " / ".join(WEIGHTS) + ".</sub>", ""]

    if tailored is not None:
        out += [f"## 4. Today's top {len(tailored.prepared)}, ready to apply", "",
                "| # | Score | Company & role | Posted | How you apply | Keywords | Link |", "|---|---|---|---|---|---|---|"]
        for i, j in enumerate(tailored.prepared, 1):
            detail = json.loads((tracker.jobs_dir / f"{j['id']}.json").read_text())
            parts = detail.get("score_parts") or {}
            breakdown = " / ".join(f"{parts[k]:g}" for k in WEIGHTS if k in parts)
            how = "your own Chrome" if ats_of(j["apply_url"]) in OWN_BROWSER else "auto-fill"
            posted = str(j.get("posted_at") or "")[:10]
            out.append(f"| {i} | **{j['score']}** <br><sub>{breakdown}</sub> | {j['company']}: {j['title']} "
                       f"<br><sub>{j['track']} resume{', researched letter' if j.get('hook') else ''}</sub> | "
                       f"{posted} | {how} | {j['covered']}% of yours | [open]({j['apply_url']}) |")
        out += ["", "<sub>Score parts: " + " / ".join(WEIGHTS) + ". Keywords: share of the job's keywords that you "
                "have which made it onto the resume.</sub>", ""]
        gaps: dict[str, int] = {}
        for j in tailored.prepared:
            for k in j.get("unsupported") or []:
                gaps[k.lower()] = gaps.get(k.lower(), 0) + 1
        common = [f"{k} ({n})" for k, n in sorted(gaps.items(), key=lambda kv: -kv[1]) if n >= 3][:8]
        if common:
            out += ["**Skills these jobs keep asking for that you don't list:** " + ", ".join(common)
                    + ". If you have any of them, add them to master_resume.yaml; otherwise they're worth learning.", ""]
        if tailored.errors:
            out += ["Couldn't prepare: " + "; ".join(e[:90] for e in tailored.errors), ""]

    out += ["## 5. What's working so far"] + [f"- {line}" for line in insights(tracker, today)]
    if costs:
        out += ["", f"## 6. Today's Claude cost: ${sum(costs.values()):.2f}",
                " | ".join(f"{step} ${v:.2f}" for step, v in sorted(costs.items(), key=lambda kv: -kv[1]))]
    out += ["", "Next: `uv run python -m jobagent apply`"]
    return "\n".join(out)


def save_summary(text: str, output_dir: Path, today: date | None = None) -> Path:
    path = output_dir / (today or date.today()).isoformat() / "summary.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path
