"""The eval set: your own answer key for the funnel.

You label real jobs once ("would apply" / "wouldn't"); `eval` then runs the current funnel on them and reports
the good jobs it would wrongly drop and the bad jobs it would wrongly let through, with the cost. Run it after
any change to models, prompts or thresholds to see whether the change helped. The tracker is never changed.
"""

from __future__ import annotations

import json
import random
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .score.funnel import SCREEN_MIN_MATCH, Assessment, judge, precheck
from .tracker import Tracker

LABELS = {"y": "yes", "n": "no", "m": "maybe"}


def load_labels(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    # One JSON line per label; a later label for the same job replaces an earlier one.
    return {r["id"]: r for r in (json.loads(line) for line in path.read_text().splitlines() if line.strip())}


def pick_for_labelling(tracker: Tracker, labelled: set[str], n: int, seed: int | None = None) -> list[dict]:
    """A mix of jobs the funnel passed and jobs it dropped (after reading them), each with a real description."""
    rng = random.Random(seed)

    def usable(r):
        return r["id"] not in labelled and len(tracker.description(r["id"])) >= 800

    passed = [r for r in tracker.jobs.values() if r.get("score") is not None and usable(r)]
    dropped = [r for r in tracker.jobs.values() if r.get("status") == "skipped" and usable(r)
               and re.match(r"(1 us|2 tech|3 experience|4 career|5 fit):", r.get("filtered_reason") or "")
               and "by title" not in (r.get("filtered_reason") or "")]
    rng.shuffle(passed)
    rng.shuffle(dropped)
    mix = passed[: n // 2] + dropped[: n - n // 2]
    rng.shuffle(mix)
    return mix


def preview(text: str, chars: int = 700) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text[:chars] + ("..." if len(text) > chars else "")


def label_jobs(tracker: Tracker, path: Path, n: int, ask: Callable[[str], str], echo: Callable[[str], None]) -> int:
    labels = load_labels(path)
    jobs = pick_for_labelling(tracker, set(labels), n)
    done = 0
    path.parent.mkdir(parents=True, exist_ok=True)
    for i, job in enumerate(jobs, 1):
        echo(f"\n[{i}/{len(jobs)}] {job['company']} - {job['title']}  ({job.get('location') or 'n/a'})")
        echo(f"  {preview(tracker.description(job['id']))}")
        echo(f"  {job['apply_url']}")
        c = ""
        while c not in ("y", "n", "m", "s", "q"):
            c = ask("  Would you apply? [y]es / [n]o / [m]aybe / [s]kip / [q]uit: ").strip().lower()[:1]
        if c == "q":
            break
        if c == "s":
            continue
        with path.open("a") as f:
            f.write(json.dumps({"id": job["id"], "label": LABELS[c], "company": job["company"],
                                "title": job["title"]}) + "\n")
        done += 1
    return done


class _NoHistory:
    """Labelled jobs are judged as if you hadn't applied to anything yet."""

    def already_applied(self, company: str, title: str) -> bool:
        return False


NO_HISTORY = _NoHistory()


@dataclass
class EvalReport:
    yes: int = 0
    no: int = 0
    kept_yes: int = 0
    missed: list[str] = field(default_factory=list)       # "yes" jobs the funnel would drop
    let_through: list[str] = field(default_factory=list)  # "no" jobs the funnel would keep
    screen_drops_of_yes: int = 0                          # of the missed, how many the first screen caused
    errors: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        out = [f"Your 'yes' jobs kept: {self.kept_yes}/{self.yes}"
               + (f" ({round(100 * self.kept_yes / self.yes)}%)" if self.yes else ""),
               f"Your 'no' jobs wrongly kept: {len(self.let_through)}/{self.no}"]
        if self.missed:
            out.append(f"Good jobs it would drop ({self.screen_drops_of_yes} of them at the cheap first screen):")
            out += [f"  - {m}" for m in self.missed]
        if self.let_through:
            out.append("Jobs you'd skip that it would keep:")
            out += [f"  - {m}" for m in self.let_through]
        out += [f"  ! {e}" for e in self.errors]
        return out


def run_eval(tracker: Tracker, labels: dict[str, dict], assess: Callable[[dict, str], Assessment],
             screen: Callable[[dict, str], Assessment] | None, *, fresh_hours: int = 24, workers: int = 4) -> EvalReport:
    """Score the labelled jobs with the current funnel. Age is ignored (old labels stay useful)."""
    report = EvalReport()
    now = datetime.now(timezone.utc)
    jobs = [tracker.jobs[i] for i, lab in labels.items() if i in tracker.jobs and lab["label"] in ("yes", "no")]

    def decide(job: dict) -> tuple[dict, str | None, bool]:
        """(job, reason it would be dropped or None, dropped at the first screen?)"""
        desc = tracker.description(job["id"])
        v = precheck({**job, "posted_at": None}, NO_HISTORY, 99, now)
        if v and v.stage != "applied":
            return job, f"{v.stage}: {v.reason} (free check)", False
        if screen:
            v = judge(screen(job, desc), job, fresh_hours, now, min_match=SCREEN_MIN_MATCH, screen=True)
            if v.stage:
                return job, f"{v.stage}: {v.reason}", True
        v = judge(assess(job, desc), job, fresh_hours, now)
        return job, (f"{v.stage}: {v.reason}" if v.stage else None), False

    def safe(job):
        try:
            return decide(job), None
        except Exception as e:  # noqa: BLE001
            return None, f"{job['company']} - {job['title']}: {e}"

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for result, err in pool.map(safe, jobs):
            if err:
                report.errors.append(err)
                continue
            job, dropped, at_screen = result
            name = f"{job['company']} - {job['title']}"
            if labels[job["id"]]["label"] == "yes":
                report.yes += 1
                if dropped:
                    report.missed.append(f"{name}: {dropped[:140]}")
                    report.screen_drops_of_yes += at_screen
                else:
                    report.kept_yes += 1
            else:
                report.no += 1
                if not dropped:
                    report.let_through.append(name)
    return report
