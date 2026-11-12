"""Oracle Recruiting Cloud ("Candidate Experience") career sites: public JSON, no login.

List:   GET https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions
            ?finder=findReqs;siteNumber=..,locationId=..,sortBy=POSTING_DATES_DESC   (newest first)
Detail: GET https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails?finder=ById;Id="..",siteNumber=..
Job:        https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{id}
"""
import html, json, re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from urllib.request import Request, urlopen

# (company, host, siteNumber, site name in job URLs, locationId of "India" in the site's location facet)
SITES = [
    ("Dell", "enterpriseplatform.dell.com", "CX_1001", "careers", "300000000471053"),
]
PAGE = 25
MAX_PAGES = 4          # newest first, so we stop at the first page older than the look-back
UA = "Mozilla/5.0 job-feed"
API = "https://{host}/hcmRestApi/resources/latest/"


def _json(url):
    req = Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urlopen(req, timeout=30) as r:
        return json.load(r)


def list_url(host, site_no, loc_id, offset):
    finder = f"findReqs;siteNumber={site_no},locationId={loc_id},limit={PAGE},offset={offset},sortBy=POSTING_DATES_DESC"
    return (API.format(host=host) + "recruitingCEJobRequisitions?onlyData=true"
            "&expand=requisitionList.secondaryLocations&finder=" + quote(finder, safe="=;,"))


def detail_url(host, site_no, job_id):
    return (API.format(host=host) + "recruitingCEJobRequisitionDetails?expand=all&onlyData=true&finder="
            + quote(f'ById;Id="{job_id}",siteNumber={site_no}', safe="=;,"))


def age_days(posted_date, now):
    """'2026-11-10' -> whole days before now (date only, so 'today' is 0)."""
    return (now.date() - datetime.strptime(posted_date[:10], "%Y-%m-%d").date()).days


def list_site(company, host, site_no, site, loc_id, max_days, now):
    out = []
    for page in range(MAX_PAGES):
        reqs = _json(list_url(host, site_no, loc_id, page * PAGE))["items"][0].get("requisitionList") or []
        for r in reqs:
            days = age_days(r["PostedDate"], now)
            if days > max_days:
                continue
            locs = [r.get("PrimaryLocation") or ""] + [s.get("Name") or "" for s in r.get("secondaryLocations") or []]
            out.append({"company": company, "title": r.get("Title", ""), "loc": " / ".join(l for l in locs if l),
                        "remote": "remote" in (r.get("WorkplaceType") or "").lower(), "days": days,
                        "id": r["Id"], "host": host, "site_no": site_no,
                        "url": f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{r['Id']}"})
        if len(reqs) < PAGE or (reqs and age_days(reqs[-1]["PostedDate"], now) > max_days):
            break
    return out


def _text(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(html.unescape(s or "")))).strip()


def add_detail(j):
    try:
        x = _json(detail_url(j["host"], j["site_no"], j["id"]))["items"][0]
        # the job's own text only; CorporateDescriptionStr is the same company blurb on every post
        j["desc"] = " ".join(_text(x.get(k)) for k in
                             ("ExternalDescriptionStr", "ExternalResponsibilitiesStr", "ExternalQualificationsStr"))
        if x.get("ExternalPostedStartDate"):
            j["posted"] = datetime.fromisoformat(x["ExternalPostedStartDate"])
    except Exception:
        j.setdefault("desc", "")
    return j


def collect(keep, max_days, now=None):
    now = now or datetime.now(timezone.utc)
    jobs, errors = [], []
    for site in SITES:
        try:
            jobs += list_site(*site, max_days, now)
        except Exception as e:
            errors.append(f"oracle/{site[0]}: {e}")
    for j in jobs:
        j["posted"] = now - timedelta(days=j["days"])  # replaced by the exact time from the detail
    cand = [j for j in jobs if keep(dict(j))]  # title + location first, then one detail call each
    with ThreadPoolExecutor(6) as pool:
        cand = list(pool.map(add_detail, cand))
    kept = [j for j in cand if keep(j)]
    for j in kept:
        j["source"] = "Oracle HCM"
        j["checks"] = [f"Posting date read from {j['company']}'s careers site", f"Location: {j['where']}"]
    return kept, errors


def probe():
    """One small request per site: which sites answer."""
    for company, host, site_no, _site, loc_id in SITES:
        try:
            d = _json(list_url(host, site_no, loc_id, 0))["items"][0]
            print(f"  ok    oracle  {company:24} {d.get('TotalJobsCount', '?')} India jobs")
        except Exception as e:
            print(f"  FAIL  oracle  {company:24} {e}")


def _selfcheck():
    now = datetime(2026, 11, 12, 18, 0, tzinfo=timezone.utc)
    assert age_days("2026-11-12", now) == 0 and age_days("2026-11-10", now) == 2
    u = list_url("h.example", "CX_1", "42", 25)
    assert u.startswith("https://h.example/hcmRestApi/resources/latest/recruitingCEJobRequisitions?") and \
        "siteNumber=CX_1,locationId=42,limit=25,offset=25,sortBy=POSTING_DATES_DESC" in u, u
    assert detail_url("h.example", "CX_1", "7").endswith("finder=ById;Id=%227%22,siteNumber=CX_1")
