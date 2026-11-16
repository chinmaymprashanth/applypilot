"""Public job-board feeds for Greenhouse, Lever and Ashby. No API key needed.

Each fetcher returns the postings on one company's board, already narrowed to tech
roles in the US and, when `since` is given, to jobs posted after it.
Endpoints checked against live responses on 2026-11-11.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Callable

import httpx

from .common import html_to_text, is_us, iso_utc, looks_tech, make_job, work_mode

# The list without descriptions is ~20x smaller; descriptions are fetched per job, only for keepers.
GREENHOUSE = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
GREENHOUSE_JOB = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{id}"
LEVER = "https://api.lever.co/v0/postings/{token}?mode=json"
ASHBY = "https://api.ashbyhq.com/posting-api/job-board/{token}"


class BoardNotFound(Exception):
    pass


def _get_json(client: httpx.Client, url: str):
    r = client.get(url)
    if r.status_code == 404:
        raise BoardNotFound(url)
    r.raise_for_status()
    return r.json()


Skip = Callable[[dict], bool]


def _keep(job: dict, since: datetime | None, country: str | None = None) -> bool:
    if since and job.get("posted_at") and datetime.fromisoformat(job["posted_at"]) < since:
        return False
    return looks_tech(job["title"]) and is_us(job.get("location"), country)


def fetch_greenhouse(
    client: httpx.Client, token: str, since: datetime | None = None, skip: Skip | None = None
) -> list[dict]:
    """skip(job) -> True for jobs already tracked, so their descriptions aren't downloaded again."""
    data = _get_json(client, GREENHOUSE.format(token=token))
    out = []
    for j in data.get("jobs", []):
        loc = (j.get("location") or {}).get("name")
        job = make_job(
            company=j.get("company_name") or token,
            title=j["title"],
            location=loc,
            work_mode=work_mode(loc),
            posted_at=iso_utc(j.get("first_published") or j.get("updated_at")),
            description="",
            source="greenhouse",
            url=j["absolute_url"],
            apply_url=j["absolute_url"],
            ats_type="greenhouse",
        )
        if not _keep(job, since):
            continue
        if not (skip and skip(job)):
            detail = _get_json(client, GREENHOUSE_JOB.format(token=token, id=j["id"]))
            job["description"] = html_to_text(detail.get("content"))
        out.append(job)
    return out


def fetch_lever(
    client: httpx.Client, token: str, since: datetime | None = None, skip: Skip | None = None
) -> list[dict]:
    data = _get_json(client, LEVER.format(token=token))
    out = []
    for j in data:
        cats = j.get("categories") or {}
        loc = cats.get("location")
        desc = "\n\n".join(
            p for p in (j.get("descriptionPlain"), *(
                f"{l.get('text')}\n{html_to_text(l.get('content'))}" for l in j.get("lists") or []
            ), j.get("additionalPlain")) if p
        )
        job = make_job(
            company=token,
            title=j["text"],
            location=loc,
            work_mode=work_mode(j.get("workplaceType"), loc),
            posted_at=iso_utc(j.get("createdAt")),
            description=desc,
            source="lever",
            url=j["hostedUrl"],
            apply_url=j.get("applyUrl") or j["hostedUrl"],
            ats_type="lever",
        )
        if _keep(job, since, j.get("country")):
            out.append(job)
    return out


def fetch_ashby(
    client: httpx.Client, token: str, since: datetime | None = None, skip: Skip | None = None
) -> list[dict]:
    data = _get_json(client, ASHBY.format(token=token))
    out = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        country = ((j.get("address") or {}).get("postalAddress") or {}).get("addressCountry")
        loc = j.get("location")
        job = make_job(
            company=token,
            title=j["title"],
            location=loc,
            work_mode=work_mode(j.get("workplaceType"), "remote" if j.get("isRemote") else None, loc),
            posted_at=iso_utc(j.get("publishedAt")),
            description=j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml")),
            source="ashby",
            url=j["jobUrl"],
            apply_url=j.get("applyUrl") or j["jobUrl"],
            ats_type="ashby",
        )
        # Ashby's country is the office address; remote roles may still be open to the US.
        if _keep(job, since, None if j.get("isRemote") else country):
            out.append(job)
    return out


# ---- Workday ----------------------------------------------------------------
# Workday career sites have a public search feed that lists the newest jobs first, so each daily check
# reads pages until it reaches jobs older than the cutoff. Token: "host|tenant|site".

WORKDAY_JOBS = "https://{host}/wday/cxs/{tenant}/{site}/jobs"
WORKDAY_PAGES = 10  # at most 200 newest postings per site per day


def posted_days(text: str | None) -> int | None:
    """'Posted Today' -> 0, 'Posted Yesterday' -> 1, 'Posted 3 Days Ago' -> 3, 'Posted 30+ Days Ago' -> 30."""
    t = (text or "").lower()
    if "today" in t:
        return 0
    if "yesterday" in t:
        return 1
    m = re.search(r"(\d+)", t)
    return int(m.group(1)) if m else None


def fetch_workday(
    client: httpx.Client, token: str, since: datetime | None = None, skip: Skip | None = None
) -> list[dict]:
    host, tenant, site = token.split("|")
    now = datetime.now(timezone.utc)
    max_days = (now - since).days if since else 30
    url = WORKDAY_JOBS.format(host=host, tenant=tenant, site=site)
    out = []
    for page in range(WORKDAY_PAGES):
        r = client.post(url, json={"appliedFacets": {}, "limit": 20, "offset": page * 20, "searchText": ""},
                        headers={"Accept": "application/json"})
        if r.status_code in (404, 410, 422) and page == 0:
            raise BoardNotFound(url)
        r.raise_for_status()
        postings = r.json().get("jobPostings") or []
        too_old = False
        for posting in postings:
            days = posted_days(posting.get("postedOn"))
            if days is not None and days > max_days:
                too_old = True
                continue
            title = posting.get("title") or ""
            where = posting.get("locationsText")
            if not looks_tech(title) or (where and "location" not in where.lower() and not is_us(where)):
                continue
            path = posting.get("externalPath") or ""
            job = make_job(
                company=tenant, title=title, location=where, work_mode=work_mode(where),
                posted_at=iso_utc((now - timedelta(days=days or 0)).replace(microsecond=0).isoformat()),
                description="", source="workday", url=f"https://{host}/{site}{path}",
                apply_url=f"https://{host}/{site}{path}", ats_type="workday",
            )
            if not (skip and skip(job)):
                info = client.get(f"https://{host}/wday/cxs/{tenant}/{site}{path}",
                                  headers={"Accept": "application/json"}).json().get("jobPostingInfo") or {}
                country = (info.get("country") or {}).get("descriptor")
                in_us = country in ("United States of America", "United States") if country else \
                    is_us(info.get("location") or where)
                if not in_us:
                    continue
                job.update(location=info.get("location") or where,
                           description=html_to_text(info.get("jobDescription")),
                           posted_at=iso_utc(info["startDate"]) if info.get("startDate") else job["posted_at"],
                           work_mode=work_mode(info.get("location"), info.get("remoteType")))
            out.append(job)
        if too_old or len(postings) < 20:
            break
    return out


FETCHERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby, "workday": fetch_workday}
# Boards whose names can be guessed from a company name (Workday sites can't).
GUESSABLE = ("greenhouse", "lever", "ashby")


# ---- SmartRecruiters, Workable, Oracle: official public job feeds ----------------


def _fresh(posted: str | None, since: datetime | None) -> bool:
    return not (since and posted and datetime.fromisoformat(posted) < since)


def fetch_smartrecruiters(client: httpx.Client, token: str, since: datetime | None = None,
                          skip: Skip | None = None) -> list[dict]:
    """token: the company identifier (jobs.smartrecruiters.com/<Company>/...)."""
    api = f"https://api.smartrecruiters.com/v1/companies/{token}/postings"
    out = []
    for offset in range(0, 500, 100):
        data = _get_json(client, f"{api}?country=us&limit=100&offset={offset}")
        rows = data.get("content") or []
        for p in rows:
            posted = iso_utc(p.get("releasedDate"))
            loc = p.get("location") or {}
            where = ", ".join(x for x in (loc.get("city"), loc.get("region")) if x) or None
            if not _fresh(posted, since) or not looks_tech(p.get("name") or ""):
                continue
            url = f"https://jobs.smartrecruiters.com/{token}/{p['id']}"
            job = make_job(company=(p.get("company") or {}).get("name") or token, title=p["name"], location=where,
                           work_mode=work_mode("remote" if loc.get("remote") else None, where), posted_at=posted,
                           description="", source="smartrecruiters", url=url, apply_url=url, ats_type="smartrecruiters")
            if not (skip and skip(job)):
                sections = ((_get_json(client, f"{api}/{p['id']}").get("jobAd") or {}).get("sections") or {})
                job["description"] = "\n\n".join(html_to_text((sections.get(k) or {}).get("text"))
                                                 for k in ("jobDescription", "qualifications", "additionalInformation"))
            out.append(job)
        if len(rows) < 100:
            break
    return out


def fetch_workable(client: httpx.Client, token: str, since: datetime | None = None,
                   skip: Skip | None = None) -> list[dict]:
    """token: the account name (apply.workable.com/<account>/...)."""
    r = client.post(f"https://apply.workable.com/api/v3/accounts/{token}/jobs",
                    json={"query": "", "location": [], "department": [], "worktype": [], "remote": []})
    if r.status_code == 404:
        raise BoardNotFound(token)
    r.raise_for_status()
    out = []
    for p in r.json().get("results") or []:
        loc = p.get("location") or {}
        country = loc.get("countryCode") or loc.get("country")
        where = ", ".join(x for x in (loc.get("city"), loc.get("region")) if x) or ("Remote" if p.get("remote") else None)
        posted = iso_utc(p.get("published"))
        if not _fresh(posted, since) or not looks_tech(p.get("title") or "") or not is_us(where, country):
            continue
        url = f"https://apply.workable.com/{token}/j/{p['shortcode']}/"
        job = make_job(company=token, title=p["title"], location=where, posted_at=posted,
                       work_mode=work_mode("remote" if p.get("remote") else None, where), description="",
                       source="workable", url=url, apply_url=url, ats_type="workable")
        if not (skip and skip(job)):
            detail = _get_json(client, f"https://apply.workable.com/api/v2/accounts/{token}/jobs/{p['shortcode']}")
            job["description"] = "\n\n".join(html_to_text(detail.get(k)) for k in ("description", "requirements", "benefits"))
        out.append(job)
    return out


def fetch_oracle(client: httpx.Client, token: str, since: datetime | None = None, skip: Skip | None = None) -> list[dict]:
    """Oracle Recruiting Cloud. token: "host|site" (e.g. "abc.fa.ocs.oraclecloud.com|CX_1"). Newest first."""
    host, site = token.split("|")
    api = f"https://{host}/hcmRestApi/resources/latest"
    out = []
    for offset in range(0, 200, 25):
        data = _get_json(client, f"{api}/recruitingCEJobRequisitions?onlyData=true&expand=requisitionList&finder="
                                 f"findReqs;siteNumber={site},limit=25,offset={offset},sortBy=POSTING_DATES_DESC")
        rows = ((data.get("items") or [{}])[0].get("requisitionList")) or []
        too_old = False
        for p in rows:
            posted = iso_utc(p.get("PostedDate"))
            if not _fresh(posted, since):
                too_old = True
                continue
            if p.get("PrimaryLocationCountry") != "US" or not looks_tech(p.get("Title") or ""):
                continue
            url = f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{p['Id']}"
            job = make_job(company=site, title=p["Title"], location=p.get("PrimaryLocation"), posted_at=posted,
                           work_mode=work_mode(p.get("WorkplaceType"), p.get("PrimaryLocation")), description="",
                           source="oracle", url=url, apply_url=url, ats_type="oracle")
            if not (skip and skip(job)):
                d = _get_json(client, f'{api}/recruitingCEJobRequisitionDetails?expand=all&onlyData=true&finder='
                                      f'ById;Id="{p["Id"]}",siteNumber={site}')
                item = (d.get("items") or [{}])[0]
                job["description"] = "\n\n".join(html_to_text(item.get(k)) for k in (
                    "ExternalDescriptionStr", "ExternalResponsibilitiesStr", "ExternalQualificationsStr"))
            out.append(job)
        if too_old or len(rows) < 25:
            break
    return out


# ---- Big employers' own career sites -------------------------------------------

AMAZON_QUERIES = ["software engineer", "data engineer", "data scientist", "business intelligence engineer",
                  "machine learning", "solutions architect", "analyst", "intern"]


def fetch_amazon(client: httpx.Client, token: str = "amazon", since: datetime | None = None,
                 skip: Skip | None = None) -> list[dict]:
    """amazon.jobs search feed, newest first, for the role families that fit (it posts thousands a week)."""
    seen, out = set(), []
    for q in AMAZON_QUERIES:
        for offset in range(0, 300, 100):
            r = client.get("https://www.amazon.jobs/en/search.json", params={
                "base_query": q, "loc_query": "United States", "country": "USA", "sort": "recent",
                "result_limit": 100, "offset": offset})
            r.raise_for_status()
            data = r.json()
            rows = data.get("jobs") or []
            too_old = False
            for p in rows:
                if p.get("id_icims") in seen:
                    continue
                seen.add(p.get("id_icims"))
                posted = iso_utc(datetime.strptime(p["posted_date"], "%B %d, %Y").replace(tzinfo=timezone.utc).isoformat()) \
                    if p.get("posted_date") else None
                if not _fresh(posted, since):
                    too_old = True
                    continue
                if not looks_tech(p.get("title") or ""):
                    continue
                url = "https://www.amazon.jobs" + p["job_path"]
                out.append(make_job(
                    company=p.get("company_name") or "Amazon", title=p["title"], location=p.get("normalized_location"),
                    work_mode=work_mode(p.get("normalized_location")), posted_at=posted, source="amazon", url=url,
                    apply_url=url, ats_type="amazon",
                    description="\n\n".join(html_to_text(p.get(k)) for k in (
                        "description", "basic_qualifications", "preferred_qualifications")),
                ))
            if too_old or len(rows) < 100:
                break
    return out


def fetch_eightfold(client: httpx.Client, token: str, since: datetime | None = None,
                    skip: Skip | None = None) -> list[dict]:
    """Eightfold career sites (e.g. Netflix). token: "host|domain" (e.g. "explore.jobs.netflix.net|netflix.com")."""
    host, domain = token.split("|")
    api = f"https://{host}/api/apply/v2/jobs"
    out = []
    for start in range(0, 300, 50):
        data = _get_json(client, f"{api}?domain={domain}&query=&location=United%20States&start={start}&num=50&sort_by=new")
        rows = data.get("positions") or []
        too_old = False
        for p in rows:
            posted = iso_utc((p.get("t_create") or 0) * 1000)
            if not _fresh(posted, since):
                too_old = True
                continue
            if not looks_tech(p.get("name") or "") or not is_us(p.get("location")):
                continue
            url = p.get("canonicalPositionUrl") or f"https://{host}/careers/job/{p['id']}"
            job = make_job(company=domain.split(".")[0].title(), title=p["name"], location=p.get("location"),
                           work_mode=work_mode(p.get("location")), posted_at=posted, description="",
                           source="eightfold", url=url, apply_url=url, ats_type="eightfold")
            if not (skip and skip(job)):
                job["description"] = html_to_text(_get_json(client, f"{api}/{p['id']}?domain={domain}").get("job_description"))
            out.append(job)
        if too_old or len(rows) < 50:
            break
    return out


FETCHERS.update({"smartrecruiters": fetch_smartrecruiters, "workable": fetch_workable, "oracle": fetch_oracle,
                 "amazon": fetch_amazon, "eightfold": fetch_eightfold})
# Big employers checked every day regardless of the lists: (ats, token, company).
ALWAYS = [("amazon", "amazon", "Amazon"), ("eightfold", "explore.jobs.netflix.net|netflix.com", "Netflix")]
