"""Daily direct-careers job scanner for Surya.

Checks company job boards (Greenhouse, Lever, Ashby) directly, keeps only fresh
jobs in Hyderabad / remote-India that match a 0-4 yr full-stack / backend / AI
profile, and writes jobs_today.html. No pip installs needed.

Run:  python job_scanner.py            (last 3 days)
      python job_scanner.py --days 7   (last 7 days)
Then open jobs_today.html. Jobs already shown on earlier runs are marked "seen".
"""
import argparse, html, json, re, sys, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

HERE = Path(__file__).parent

COMPANIES = {
    "greenhouse": """abnormalsecurity affirm airbnb anthropic asana chime cloudflare cockroachlabs
        coherehealth coursera databricks datadog descript discord dropbox druva duolingo elastic
        envoyglobalinc fanaticscommerce figma fivetran gitlab groww gusto hackerrank instacart
        intercom launchdarkly lyft mixpanel mongodb netskope newrelic okta pinterest
        razorpaysoftwareprivatelimited reddit roblox samsara schrdinger singlestore sofi stripe
        twilio udemy utilityprofit vercel yugabyte zetaglobal zscaler scaleai""".split(),
    "lever": "gohighlevel cred hevodata mindtickle paytm spotify supermove zeta palantir".split(),
    "ashby": """livekit tempo reacher outmarket sarvam smallest supabase railway bureau atlan
        composio cursor deepgram elevenlabs harvey openai notion redis runpod lambda anyscale neon
        openrouter temporal writer zapier posthog n8n langchain linear vapi replit render mintlify
        resend modal cartesia bland""".split(),
    "smartrecruiters": "ServiceNow Experian Freshworks Gainsight Swiggy Upstox".split(),
}

# --- filters -------------------------------------------------------------
TITLE_OK = re.compile(r"engineer|developer|\bsde\b|full[\s-]?stack|backend|back-end|front[\s-]?end|software|programmer", re.I)
TITLE_BAD = re.compile(
    r"senior|\bsr\.?\b|staff|principal|lead|manager|director|head|architect|\bvp\b|\biii\b|\biv\b|\bl[4-9]\b|"
    r"intern|apprentice|java\b(?!script)|\.net|c#|c\+\+|golang|\bgo\b|rust|scala|kotlin|swift|android|ios|"
    r"salesforce|\bsap\b|data engineer|analytics|\bsre\b|reliability|devops|security|network|embedded|firmware|"
    r"hardware|\bqa\b|test|sdet|support|sales|solutions|research|scientist|machine learning engineer|ml engineer|"
    r"mobile|game|graphics|kernel|compiler|database engineer|infrastructure", re.I)
# Hyderabad, or explicitly remote and open to India / APAC / anywhere.
LOC_HYD = re.compile(r"hyderabad|telangana", re.I)
LOC_OPEN = re.compile(r"india|apac|apj|asia|anywhere|worldwide|global", re.I)
LOC_REMOTE = re.compile(r"remote", re.I)
LOC_OTHER_ONLY = re.compile(r"\b(us|usa|united states|canada|uk|united kingdom|emea|europe|germany|ireland|"
                            r"latam|brazil|mexico|australia|japan|singapore)\b", re.I)
MY_STACK = re.compile(r"\b(react|node\.?js|node|typescript|javascript|python|fastapi|mongodb|express|next\.?js|"
                      r"llm|livekit|webrtc|rest(?:ful)? api)\b", re.I)
YEARS = re.compile(r"(\d{1,2})\s*\+?\s*(?:-|–|to)?\s*(?:\d{1,2})?\s*\+?\s*years?", re.I)


def location_ok(loc, remote_flag=False):
    """Return a label if the job location suits Surya, else None."""
    if LOC_HYD.search(loc):
        return "Hyderabad"
    remote = remote_flag or LOC_REMOTE.search(loc)
    if remote and LOC_OPEN.search(loc):
        return "Remote (India/APAC)"
    if LOC_OPEN.search(loc) and not re.search(r"bengaluru|bangalore|pune|gurgaon|gurugram|noida|delhi|mumbai|chennai|kolkata", loc, re.I):
        return "India (check city)"
    return None


def years_required(text):
    # ponytail: regex guess at the experience bar; read the JD before applying.
    mins = [int(m.group(1)) for m in YEARS.finditer(text or "") if 0 < int(m.group(1)) <= 15]
    return max(mins) if mins else None


def keep(job):
    if not TITLE_OK.search(job["title"]) or TITLE_BAD.search(job["title"]):
        return False
    label = location_ok(job["loc"], job.get("remote", False))
    if not label:
        return False
    job["where"] = label
    return True


# --- fetchers ------------------------------------------------------------
def get_json(url):
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 job-scanner"})
    with urlopen(req, timeout=30) as r:
        return json.load(r)


def iso(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def fetch_greenhouse(slug):
    data = get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
    for j in data.get("jobs", []):
        yield {"company": j.get("company_name") or slug, "title": j["title"], "loc": j["location"]["name"],
               "posted": iso(j.get("first_published") or j.get("updated_at")), "url": j["absolute_url"],
               "detail": f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{j['id']}", "ats": "Greenhouse"}


def fetch_lever(slug):
    for j in get_json(f"https://api.lever.co/v0/postings/{slug}?mode=json"):
        c = j.get("categories", {})
        loc = " / ".join([c.get("location") or ""] + (c.get("allLocations") or []))
        sal = j.get("salaryRange") or {}
        yield {"company": slug, "title": j["text"], "loc": loc, "remote": j.get("workplaceType") == "remote",
               "posted": datetime.fromtimestamp(j["createdAt"] / 1000, timezone.utc), "url": j["hostedUrl"],
               "desc": j.get("descriptionPlain", "") + " " + " ".join(l.get("content", "") for l in j.get("lists", [])),
               "pay": f"{sal.get('currency','')} {sal.get('min','')}-{sal.get('max','')}" if sal else "", "ats": "Lever"}


def fetch_ashby(slug):
    data = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true")
    for j in data.get("jobs", []):
        locs = [j.get("location") or ""] + [l.get("location", "") for l in j.get("secondaryLocations") or []]
        addr = (j.get("address") or {}).get("postalAddress") or {}
        loc = " / ".join(locs + [addr.get("addressCountry") or ""])
        comp = (j.get("compensation") or {}).get("compensationTierSummary") or ""
        yield {"company": slug, "title": j["title"], "loc": loc, "remote": bool(j.get("isRemote")),
               "posted": iso(j.get("publishedAt")), "url": j["jobUrl"], "desc": j.get("descriptionPlain", ""),
               "pay": comp, "ats": "Ashby"}


def fetch_smartrecruiters(slug):
    data = get_json(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?country=in&limit=100")
    for j in data.get("content", []):
        l = j.get("location") or {}
        yield {"company": slug, "title": j["name"], "loc": l.get("fullLocation") or l.get("city", ""),
               "remote": bool(l.get("remote")), "posted": iso(j.get("releasedDate")),
               "url": f"https://jobs.smartrecruiters.com/{slug}/{j['id']}", "detail": j["ref"], "ats": "SmartRecruiters"}


FETCH = {"greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby,
         "smartrecruiters": fetch_smartrecruiters}


def scan(ats, slug):
    try:
        return list(FETCH[ats](slug)), None
    except Exception as e:  # one broken board must not stop the run
        return [], f"{ats}/{slug}: {e}"


def add_desc(job):
    """Greenhouse and SmartRecruiters list endpoints have no JD text; fetch it per job."""
    if "detail" not in job:
        return job
    try:
        d = get_json(job["detail"])
        raw = d.get("content") or json.dumps(((d.get("jobAd") or {}).get("sections")) or {})
        job["desc"] = re.sub(r"<[^>]+>", " ", html.unescape(raw))
    except Exception:
        job["desc"] = ""
    return job


# --- report --------------------------------------------------------------
def write_report(jobs, errors, days, path):
    rows = []
    for j in jobs:
        yrs = j.get("yrs")
        yrs_txt = f"{yrs}+ yrs" if yrs else "not stated"
        badge = ' <span class="seen">seen</span>' if j.get("seen") else ' <span class="new">NEW</span>'
        rows.append(f"<tr><td>{html.escape(j['company'])}</td><td><a href='{html.escape(j['url'])}' target=_blank>"
                    f"{html.escape(j['title'])}</a>{badge}</td><td>{html.escape(j['where'])}<br><small>{html.escape(j['loc'][:80])}</small></td>"
                    f"<td>{j['age']}</td><td>{yrs_txt}</td><td>{html.escape(', '.join(j.get('stack') or []))}</td>"
                    f"<td>{html.escape(j.get('pay') or '')}</td><td>{j['ats']}</td></tr>")
    err = "".join(f"<li>{html.escape(e)}</li>" for e in errors)
    path.write_text(f"""<!doctype html><meta charset=utf-8><title>Jobs today</title>
<style>body{{font:14px system-ui;margin:16px;background:#fff;color:#111}}table{{border-collapse:collapse;width:100%}}
td,th{{border-bottom:1px solid #ddd;padding:6px;text-align:left;vertical-align:top}}th{{background:#f3f3f3}}
.new{{background:#0a7;color:#fff;padding:1px 5px;border-radius:4px;font-size:11px}}.seen{{color:#888;font-size:11px}}</style>
<h2>Direct-careers jobs — last {days} day(s) — {datetime.now():%d %b %Y %H:%M}</h2>
<p>{len(jobs)} matching jobs. Experience is auto-read from the JD: always open the JD before applying.</p>
<table><tr><th>Company</th><th>Role</th><th>Location</th><th>Posted</th><th>Exp asked</th><th>Your stack in JD</th><th>Pay</th><th>ATS</th></tr>
{''.join(rows)}</table><details><summary>{len(errors)} boards failed</summary><ul>{err}</ul></details>""", encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=3)
    ap.add_argument("--max-years", type=int, default=4, help="skip jobs asking more than this many years")
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    tasks = [(ats, s) for ats, slugs in COMPANIES.items() for s in slugs]
    jobs, errors = [], []
    with ThreadPoolExecutor(16) as pool:
        for found, err in pool.map(lambda t: scan(*t), tasks):
            jobs += found
            if err:
                errors.append(err)

    fresh = [j for j in jobs if j["posted"] and (now - j["posted"]).total_seconds() <= args.days * 86400 and keep(j)]
    with ThreadPoolExecutor(16) as pool:
        fresh = list(pool.map(add_desc, fresh))

    seen_file = HERE / "seen_jobs.json"
    seen = set(json.loads(seen_file.read_text())) if seen_file.exists() else set()
    out = []
    for j in fresh:
        j["yrs"] = years_required(j.get("desc"))
        if j["yrs"] and j["yrs"] > args.max_years:
            continue
        j["stack"] = sorted({m.lower() for m in MY_STACK.findall(j.get("desc") or "")})
        if j.get("desc") and not j["stack"]:  # JD readable but none of Surya's stack (e.g. Haskell role)
            continue
        hours = (now - j["posted"]).total_seconds() / 3600
        j["age"] = f"{hours:.0f} h ago" if hours < 48 else f"{hours / 24:.0f} d ago"
        j["seen"] = j["url"] in seen
        out.append(j)
    out.sort(key=lambda j: (j["seen"], j["posted"] is None, -(j["posted"].timestamp())))
    seen_file.write_text(json.dumps(sorted(seen | {j["url"] for j in out})))

    report = HERE / "jobs_today.html"
    write_report(out, errors, args.days, report)
    print(f"Checked {len(tasks)} companies, {len(jobs)} jobs. {len(out)} match "
          f"({sum(not j['seen'] for j in out)} new). Open: {report}")
    if errors:
        print(f"{len(errors)} boards failed (see bottom of report).", file=sys.stderr)


def _selfcheck():
    assert location_ok("Hyderabad, Telangana, India") == "Hyderabad"
    assert location_ok("Remote - India") == "Remote (India/APAC)"
    assert location_ok("North America / APJ / EMEA", remote_flag=True) == "Remote (India/APAC)"
    assert location_ok("Remote - US") is None
    assert location_ok("Bengaluru, India") is None
    assert keep({"title": "Software Engineer II", "loc": "Hyderabad"})
    assert not keep({"title": "Senior Software Engineer", "loc": "Hyderabad"})
    assert not keep({"title": "Java Developer", "loc": "Hyderabad"})
    assert keep({"title": "Full Stack Engineer (JavaScript)", "loc": "Remote, India"})
    assert years_required("3-5 years of experience; company founded 30 years ago") == 3
    assert years_required("2+ years React, 6+ years overall") == 6
    assert years_required("no number here") is None


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        _selfcheck(); print("selfcheck ok")
    else:
        main()
