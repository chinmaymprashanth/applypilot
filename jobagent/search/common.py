"""Helpers shared by every job source: role matching, US check, dates, ATS detection."""

from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

TIER1 = [
    r"software engineer", r"software developer", r"\bsde\b", r"back[- ]?end",
    r"python (developer|engineer)", r"\bai engineer", r"applied ai", r"\bllm\b",
    r"machine learning engineer", r"\bml engineer",
]
TIER2 = [
    r"data engineer", r"analytics engineer", r"applications? engineer",
    r"(technical )?support engineer", r"solutions engineer",
]

US_STATES = (
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM "
    "NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC"
).split()
US_WORDS = ("united states", "usa", "u.s.", "us-remote", "remote us", "remote - us", "remote, us")
NON_US_WORDS = (
    "canada", "united kingdom", "uk", "england", "scotland", "london", "manchester", "edinburgh", "ireland",
    "dublin", "india", "bengaluru", "bangalore", "hyderabad", "pune", "chennai", "mumbai", "delhi", "gurgaon",
    "gurugram", "noida", "germany", "berlin", "munich", "hamburg", "france", "paris", "netherlands", "amsterdam",
    "spain", "madrid", "barcelona", "portugal", "lisbon", "italy", "milan", "switzerland", "zurich", "geneva",
    "austria", "vienna", "belgium", "brussels", "sweden", "stockholm", "denmark", "copenhagen", "norway", "oslo",
    "finland", "helsinki", "poland", "warsaw", "krakow", "czech", "prague", "romania", "bucharest", "ukraine",
    "kyiv", "estonia", "tallinn", "israel", "tel aviv", "uae", "dubai", "saudi", "egypt", "cairo", "turkey",
    "istanbul", "south africa", "cape town", "nigeria", "lagos", "kenya", "nairobi", "singapore", "australia",
    "sydney", "melbourne", "new zealand", "auckland", "japan", "tokyo", "korea", "seoul", "china", "beijing",
    "shanghai", "shenzhen", "hong kong", "taiwan", "taipei", "vietnam", "philippines", "manila", "indonesia",
    "jakarta", "malaysia", "kuala lumpur", "thailand", "bangkok", "pakistan", "bangladesh", "mexico",
    "brazil", "sao paulo", "são paulo", "argentina", "buenos aires", "colombia", "bogota", "chile", "santiago",
    "peru", "lima", "toronto", "vancouver", "montreal", "ottawa", "emea", "apac", "latam", "europe",
)


# Search keeps anything that might be a tech job; Claude decides for real when scoring.
TECH_HINTS = re.compile(
    r"engineer|developer|programmer|\bswe\b|\bsde\b|\bmts\b|technical staff|software|analyst|analytics|"
    r"scientist|\bdata\b|\bai\b|\bml\b|machine learning|\bllm\b|\bbi\b|business intelligence|backend|"
    r"back-end|full[- ]?stack|front[- ]?end|cloud|devops|\bsre\b|platform|python|\bsql\b|automation|"
    r"salesforce|\bcrm\b|consultant|implementation|\btechnical\b|technology|rotational",
    re.I,
)
NOT_TECH = re.compile(
    r"\b(recruit\w*|sales|account (executive|manager)|marketing manager|nurse|driver|warehouse|"
    r"cashier|attorney|paralegal|accountant|bookkeeper|receptionist|teacher)\b",
    re.I,
)


def looks_tech(title: str) -> bool:
    return bool(TECH_HINTS.search(title)) and not NOT_TECH.search(title)


def role_tier(title: str) -> int | None:
    """1 or 2 if the title is a target role, else None."""
    t = title.lower()
    if any(re.search(p, t) for p in TIER1):
        return 1
    if any(re.search(p, t) for p in TIER2):
        return 2
    return None


def is_us(location: str | None, country: str | None = None) -> bool:
    """Whether a job can be done from the US. Anything that doesn't name another country counts as US;
    Claude double-checks from the description, so this only needs to catch the obvious cases."""
    if country:
        return country.strip().upper() in ("US", "USA", "UNITED STATES")
    if not location:
        return True
    loc = location.lower()
    if any(w in loc for w in US_WORDS) or re.search(r"\b(" + "|".join(US_STATES) + r")\b", location):
        return True
    return not any(re.search(rf"\b{re.escape(w)}\b", loc) for w in NON_US_WORDS)


def work_mode(*hints: str | None) -> str:
    text = " ".join(h for h in hints if h).lower()
    if "hybrid" in text:
        return "hybrid"
    if "remote" in text:
        return "remote"
    if "onsite" in text or "on-site" in text or "in office" in text:
        return "onsite"
    return "unknown"


def html_to_text(raw: str | None) -> str:
    if not raw:
        return ""
    text = html.unescape(raw)  # Greenhouse double-encodes its HTML
    text = re.sub(r"<(br|/p|/li|/h\d)[^>]*>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def iso_utc(value: str | int | float | None) -> str | None:
    """ISO 8601 string or epoch milliseconds -> ISO timestamp in UTC."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        dt = datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    else:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def age_hours(posted_at: str | None, now: datetime | None = None) -> float | None:
    if not posted_at:
        return None
    now = now or datetime.now(timezone.utc)
    return (now - datetime.fromisoformat(posted_at)).total_seconds() / 3600


def detect_board(url: str) -> tuple[str, str] | None:
    """('greenhouse'|'lever'|'ashby'|'workday', board token) if the URL is a job on one of those boards."""
    parts = urlsplit(url)
    host = parts.netloc.lower()
    segs = [s for s in parts.path.split("/") if s]
    if not segs:
        return None
    if host.endswith("greenhouse.io") and host.split(".")[0] in ("boards", "job-boards"):
        if segs[0] == "embed":  # boards.greenhouse.io/embed/job_app?for=<token>
            return None
        return ("greenhouse", segs[0].lower())
    if host == "jobs.lever.co":
        return ("lever", segs[0].lower())
    if host == "jobs.ashbyhq.com":
        return ("ashby", segs[0].lower())
    if host == "jobs.smartrecruiters.com":  # .../<Company>/<id> or .../oneclick-ui/company/<Company>/...
        company = segs[2] if segs[0] == "oneclick-ui" and len(segs) > 2 else segs[0]
        return ("smartrecruiters", company)
    if host == "apply.workable.com" and segs[0] not in ("api", "j"):
        return ("workable", segs[0].lower())
    if host.endswith("oraclecloud.com") and "sites" in segs and segs.index("sites") + 1 < len(segs):
        return ("oracle", f"{host}|{segs[segs.index('sites') + 1]}")
    m = WORKDAY_URL.match(url.strip())
    if m:  # token = "host|tenant|site" (Workday site names ignore case)
        return ("workday", f"{m.group(1).lower()}.{m.group(2)}.myworkdayjobs.com|{m.group(1).lower()}|{m.group(3).lower()}")
    return None


WORKDAY_URL = re.compile(r"https://([\w-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([\w-]+)", re.I)


def make_job(**fields) -> dict:
    """A job record in the shape Tracker.add_job expects."""
    required = ("company", "title", "url", "source")
    missing = [k for k in required if not fields.get(k)]
    if missing:
        raise ValueError(f"job missing {missing}")
    fields["title"] = " ".join(fields["title"].split())
    return fields


# Which hiring system a link belongs to, from its host. Order matters only for overlapping names.
ATS_HOSTS: list[tuple[str, tuple[str, ...]]] = [
    ("greenhouse", ("greenhouse.io",)), ("lever", ("lever.co",)), ("ashby", ("ashbyhq.com",)),
    ("workday", ("myworkdayjobs.com", "myworkday.com", "workday.com")), ("oracle", ("oraclecloud.com", "taleo.net")),
    ("icims", ("icims.com", "jibeapply.com")), ("successfactors", ("successfactors", "sapsf.")),
    ("smartrecruiters", ("smartrecruiters.com",)), ("workable", ("workable.com",)), ("jazzhr", ("applytojob.com",)),
    ("amazon", ("amazon.jobs",)), ("eightfold", ("eightfold.ai", "explore.jobs.netflix.net")), ("linkedin", ("linkedin.com",)), ("indeed", ("indeed.com",)), ("adzuna", ("adzuna.",)),
]


def ats_of(url: str | None) -> str:
    host = urlsplit(url or "").netloc.lower()
    return next((name for name, hosts in ATS_HOSTS if any(h in host for h in hosts)), "other")
