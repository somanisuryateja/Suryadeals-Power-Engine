"""Workday career sites (most large companies and GCCs): public JSON, no login.

List:   POST https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
Detail: GET  https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}{externalPath}
Each site is searched for "Hyderabad" and for "India remote".
"""
import html, json, re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen

# (company, tenant, wd host, site). ponytail: hand list; `--probe` shows which ones answer, prune the rest.
SITES = [
    ("NVIDIA", "nvidia", "wd5", "NVIDIAExternalCareerSite"),
    ("Salesforce", "salesforce", "wd12", "External_Career_Site"),
    ("Adobe", "adobe", "wd5", "external_experienced"),
    ("Intel", "intel", "wd1", "External"),
    ("Workday", "workday", "wd5", "Workday"),
    ("Novartis", "novartis", "wd3", "Novartis_Careers"),
    ("Mastercard", "mastercard", "wd1", "CorporateCareers"),
    ("Micron", "micron", "wd1", "External"),
    ("HP", "hp", "wd5", "ExternalCareerSite"),
    ("Thomson Reuters", "thomsonreuters", "wd5", "External_Career_Site"),
    ("Autodesk", "autodesk", "wd1", "Ext"),
    ("CrowdStrike", "crowdstrike", "wd5", "crowdstrikecareers"),
    ("Fidelity", "fmr", "wd1", "FidelityCareers"),
    ("State Street", "statestreet", "wd1", "Global"),
    ("Zoetis", "zoetis", "wd5", "zoetis"),
    ("Warner Bros. Discovery", "warnerbros", "wd5", "global"),
    ("Marvell", "marvell", "wd1", "MarvellCareers"),
]
SEARCHES = ["Hyderabad", "remote India"]
MAX_PAGES = 3          # 20 jobs a page
UA = "Mozilla/5.0 job-feed"


def _json(url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = Request(url, data=data, method="POST" if data else "GET",
                  headers={"User-Agent": UA, "Content-Type": "application/json", "Accept": "application/json",
                           "Accept-Language": "en-US"})
    with urlopen(req, timeout=30) as r:
        return json.load(r)


def posted_days(text):
    """'Posted Today' -> 0, 'Posted Yesterday' -> 1, 'Posted 3 Days Ago' -> 3, '30+' -> 30."""
    t = (text or "").lower()
    if "today" in t:
        return 0
    if "yesterday" in t:
        return 1
    m = re.search(r"(\d+)\+?\s*day", t)
    return int(m.group(1)) if m else None


def _base(tenant, wd, site):
    return f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}"


def list_site(company, tenant, wd, site, max_days):
    base, out, seen = _base(tenant, wd, site), [], set()
    for q in SEARCHES:
        for page in range(MAX_PAGES):
            d = _json(base + "/jobs", {"appliedFacets": {}, "limit": 20, "offset": page * 20, "searchText": q})
            posts = d.get("jobPostings") or []
            for p in posts:
                days = posted_days(p.get("postedOn"))
                if p.get("externalPath") in seen or days is None or days > max_days:
                    continue
                seen.add(p["externalPath"])
                out.append({"company": company, "title": p.get("title", ""), "loc": p.get("locationsText", ""),
                            "days": days, "path": p["externalPath"], "base": base,
                            "url": f"https://{tenant}.{wd}.myworkdayjobs.com/{site}{p['externalPath']}"})
            if len(posts) < 20:
                break
    return out


def add_detail(j):
    try:
        info = _json(j["base"] + j["path"]).get("jobPostingInfo") or {}
        locs = " / ".join([info.get("location") or ""] + list(info.get("additionalLocations") or []))
        j["loc"] = locs or j["loc"]
        j["desc"] = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(info.get("jobDescription") or "")))
        j["url"] = info.get("externalUrl") or j["url"]
        if info.get("startDate"):
            j["posted"] = datetime.fromisoformat(info["startDate"][:10]).replace(tzinfo=timezone.utc)
    except Exception:
        j.setdefault("desc", "")
    return j


def collect(keep, max_days, now=None):
    now = now or datetime.now(timezone.utc)
    jobs, errors = [], []

    def one(site):
        try:
            return list_site(*site, max_days), None
        except Exception as e:
            return [], f"workday/{site[1]}: {e}"

    with ThreadPoolExecutor(6) as pool:
        for got, err in pool.map(one, SITES):
            jobs += got
            errors += [err] if err else []
    for j in jobs:
        j["posted"] = now - timedelta(days=j["days"])
    # "2 Locations" can't be judged from the list: read the detail first, then filter.
    cand = [j for j in jobs if "location" in j["loc"].lower() or keep(dict(j))]
    with ThreadPoolExecutor(6) as pool:
        cand = list(pool.map(add_detail, cand))
    kept = [j for j in cand if keep(j)]
    for j in kept:
        j["source"] = "Workday"
        j["checks"] = [f"Posted {j['days']} day(s) ago on {j['company']}'s Workday site", f"Location: {j['where']}"]
    return kept, errors


def probe():
    """One small request per site: which tenants answer."""
    for company, tenant, wd, site in SITES:
        try:
            d = _json(_base(tenant, wd, site) + "/jobs", {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": "Hyderabad"})
            print(f"  ok    workday {company:24} {d.get('total', '?')} Hyderabad jobs")
        except Exception as e:
            print(f"  FAIL  workday {company:24} {e}")


def _selfcheck():
    assert posted_days("Posted Today") == 0 and posted_days("Posted Yesterday") == 1
    assert posted_days("Posted 3 Days Ago") == 3 and posted_days("Posted 30+ Days Ago") == 30
    assert posted_days("") is None
    assert _base("nvidia", "wd5", "X") == "https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/X"
