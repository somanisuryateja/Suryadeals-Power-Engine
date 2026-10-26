"""Daily job collector for the Morning Jobs page.

Sources: company job boards (Greenhouse, Lever, Ashby, SmartRecruiters via
job_scanner.py), Himalayas (India-eligible remote search) and the current
HN "Who is hiring" thread. Every job is checked at its source before it is
kept: real posting date, location really open to Hyderabad / India, stack,
experience asked. Writes jobs_today.json for the morning Claude task, which
adds notes and loads the page.

Run:  python daily_jobs.py            (last 7 days)
      python daily_jobs.py --days 3
      python daily_jobs.py --selfcheck
"""
import argparse, hashlib, html, json, re, sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

try:
    from engine import job_scanner as js
except ImportError:  # run as a script from this folder
    import job_scanner as js

HERE = Path(__file__).parent
USD_INR = 84  # ponytail: fixed rate, only used to flag 25L+ pay

# Description text that locks a "remote" job to other regions.
REGION_LOCK = re.compile(
    r"based in (latin america|latam|the us|the usa|the united states|europe|the eu|the uk|canada)|"
    r"(eu|latam|us)[ /-]?(only|countries)|available across all eu|latam countries|"
    r"us work authori[sz]ation|authori[sz]ed to work in the (us|united states)|"
    r"must (live|reside|be located) in the (us|united states|uk|eu)|us citizens? only|security clearance", re.I)
BAD_STACK = re.compile(r"\b(java|spring boot|\.net|c#|php|laravel|ruby on rails|golang|elixir|scala|kotlin|swift)\b", re.I)
BAD_TITLE = re.compile(r"react native|part[- ]time|equity only|unpaid|devrel|developer relations|commission|offshore", re.I)
# Gigs, outsourcing shops and data-labelling work that slipped through by title.
BAD_DESC = re.compile(r"\boutsourcing\b|fixed price|paid on completion|data annotation|data labell?ing|equity[- ]only", re.I)
HIMALAYAS_QUERIES = ["full stack", "node", "react", "typescript", "fastapi", "python backend", "ai engineer", "mern"]


def job_id(url):
    return hashlib.sha1(url.encode()).hexdigest()[:16]


def strip(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(html.unescape(s or "")))).strip()


def pay_inr_lakh(text):
    """Lowest USD yearly figure in a pay string -> lakh INR. None if unknown."""
    if not text:
        return None
    t = text.replace(",", "")
    m = re.search(r"\$\s?(\d+(?:\.\d+)?)\s?([kK])?", t) or re.search(r"USD\s?(\d+(?:\.\d+)?)\s?([kK])?", t)
    if not m:
        m2 = re.search(r"(?:INR|₹)\s?(\d+(?:\.\d+)?)", t)
        return round(float(m2.group(1)) / 1e5, 1) if m2 and float(m2.group(1)) > 1e5 else None
    usd = float(m.group(1)) * (1000 if m.group(2) else 1)
    if re.search(r"/\s?h(ou)?r|hourly", t, re.I):
        usd *= 2000
    elif re.search(r"/\s?mo|month", t, re.I):
        usd *= 12
    if usd < 5000:  # not a yearly figure we understand
        return None
    return round(usd * USD_INR / 1e5, 1)


def check_text(job, max_years):
    """Shared checks on the job text. Returns a reason string if rejected."""
    d = job.get("desc") or ""
    if REGION_LOCK.search(d):
        return "locked to another region: " + REGION_LOCK.search(d).group(0)
    if BAD_STACK.search(job["title"]):
        return "stack outside rules"
    if BAD_TITLE.search(job["title"]):
        return "not a fit: " + BAD_TITLE.search(job["title"]).group(0)
    if BAD_DESC.search(d):
        return "not a fit: " + BAD_DESC.search(d).group(0)
    lakh = pay_inr_lakh(job.get("pay"))
    if lakh is not None and lakh < 10:
        return f"pay about {lakh}L, under your 10L floor"
    yrs = js.years_required(d)
    job["yrs"] = yrs
    if yrs and yrs > max_years:
        return f"asks {yrs}+ years"
    job["stack"] = sorted({m.lower().replace(".", "") for m in js.MY_STACK.findall(d)})
    if d and not job["stack"]:
        return "too little of your stack in the description"
    return None


# --- source 1: company job boards (dates come from the ATS itself) ---------
def from_boards(days, now):
    tasks = [(ats, s) for ats, slugs in js.COMPANIES.items() for s in slugs]
    jobs, errors = [], []
    with ThreadPoolExecutor(16) as pool:
        for found, err in pool.map(lambda t: js.scan(*t), tasks):
            jobs += found
            errors += [err] if err else []
    fresh = [j for j in jobs if j["posted"] and (now - j["posted"]).total_seconds() <= days * 86400 and js.keep(j)]
    with ThreadPoolExecutor(16) as pool:
        fresh = list(pool.map(js.add_desc, fresh))
    for j in fresh:
        j["source"] = j.pop("ats")
        j["checks"] = [f"Posting date read from {j['source']}", f"Location: {j['where']}"]
    return fresh, len(tasks), errors


# --- source 2: Himalayas (dates re-checked on the job page itself) ---------
def from_himalayas(days, now):
    seen, out = set(), []
    for q in HIMALAYAS_QUERIES:
        for page in range(1, 6):
            try:
                data = js.get_json("https://himalayas.app/jobs/api/search?" + urlencode({"q": q, "country": "India", "page": page}))
            except Exception:
                break
            for j in data.get("jobs", []):
                if j["guid"] in seen:
                    continue
                seen.add(j["guid"])
                locs = [l.lower() for l in j.get("locationRestrictions") or []]
                if locs and "india" not in locs:
                    continue
                pay = f"{j.get('currency')} {j.get('minSalary')}-{j.get('maxSalary')} {j.get('salaryPeriod')}" if j.get("minSalary") else ""
                out.append({"company": j["companyName"], "title": j["title"], "loc": "India" if locs else "Worldwide",
                            "remote": True, "url": j["applicationLink"].split("?")[0], "desc": strip(j.get("description")),
                            "pay": pay, "source": "Himalayas", "feed_date": j.get("pubDate")})
    cut = now.timestamp() - days * 86400
    kept = []
    for j in out:
        if (j["feed_date"] or 0) < cut or not js.TITLE_OK.search(j["title"]) or js.TITLE_BAD.search(j["title"]):
            continue
        # ponytail: Himalayas re-dates old posts and blocks scripted page reads, so the
        # morning Claude run opens each job page and checks the real date before loading it.
        j["posted"] = datetime.fromtimestamp(j["feed_date"], timezone.utc)
        j["verify"] = "himalayas_date"
        j["where"] = "Remote (India)" if j["loc"] == "India" else "Remote (Worldwide)"
        j["checks"] = [f"Location filter on Himalayas: {j['loc']}",
                       "! Date comes from the Himalayas feed, which sometimes re-dates old posts: check it on the page"]
        kept.append(j)
    return kept


# --- source 3: HN "Who is hiring" (comment date = posting date) ----------
def from_hn(days, now):
    s = js.get_json("https://hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring&hitsPerPage=6")
    story = next(h for h in s["hits"] if h["title"].startswith("Ask HN: Who is hiring?"))
    item = js.get_json(f"https://hn.algolia.com/api/v1/items/{story['objectID']}")
    out = []
    for c in item.get("children", []):
        t = strip(c.get("text"))
        posted = js.iso(c["created_at"])
        if not t or (now - posted).total_seconds() > max(days, 14) * 86400:  # monthly thread
            continue
        head = t.split("|")
        if len(head) < 3 or not re.search(r"\b(india|worldwide|anywhere|apac|asia)\b", t[:400], re.I):
            continue
        if not js.MY_STACK.search(t) or re.search(r"\b(senior|staff|principal|lead)\b", t[:200], re.I) and not re.search(r"\bjunior|mid\b", t[:200], re.I):
            continue
        links = re.findall(r'href="([^"]+)"', html.unescape(c.get("text") or ""))
        out.append({"company": head[0].strip()[:60], "title": head[1].strip()[:100], "loc": t[:200], "desc": t,
                    "url": links[0] if links else f"https://news.ycombinator.com/item?id={c['id']}",
                    "hn": f"https://news.ycombinator.com/item?id={c['id']}", "posted": posted, "pay": "",
                    "where": "Remote (check post)", "source": "HN Who is hiring",
                    "checks": ["Posted by the company in the HN thread", "Mentions India / worldwide in the header"]})
    return out


def collect(days=7, max_years=4):
    """Run every source and checks; return the run as a dict."""
    now = datetime.now(timezone.utc)
    boards, n_boards, errors = from_boards(days, now)
    counts = {"Company job boards": len(boards)}
    jobs = list(boards)
    for name, fn in [("Himalayas", from_himalayas), ("HN Who is hiring", from_hn)]:
        try:
            got = fn(days, now)
        except Exception as e:
            got, errors = [], errors + [f"{name}: {e}"]
        counts[name] = len(got)
        jobs += got

    kept, rejected, seen = [], [], set()
    for j in jobs:
        key = j["url"]
        if key in seen:
            continue
        seen.add(key)
        why = check_text(j, max_years)
        if why:
            rejected.append(f"{j['company']} | {j['title']}: {why}")
            continue
        lakh = pay_inr_lakh(j.get("pay"))
        if j.get("yrs"):
            j["checks"].append(f"Asks {j['yrs']}+ years")
        kept.append({"id": job_id(j["url"]), "company": j["company"], "title": j["title"], "url": j["url"],
                     "where": j["where"], "loc": (j.get("loc") or "")[:120], "postedAt": j["posted"].isoformat(),
                     "years": j.get("yrs"), "stack": j.get("stack", []), "pay": j.get("pay") or "",
                     "payLakh": lakh, "pay25": bool(lakh and lakh >= 25), "source": j["source"],
                     "checks": j["checks"], "hn": j.get("hn", ""), "verify": j.get("verify", ""), "desc": (j.get("desc") or "")[:1500]})
    kept.sort(key=lambda j: (not j["pay25"], -datetime.fromisoformat(j["postedAt"]).timestamp()))
    return {"runAt": now.isoformat(), "days": days, "companiesChecked": n_boards, "sources": counts,
            "kept": kept, "rejectedCount": len(rejected), "rejectedSample": rejected[:40], "errors": errors[:40]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=7)
    ap.add_argument("--max-years", type=int, default=4)
    args = ap.parse_args()
    out = collect(args.days, args.max_years)
    (HERE / "jobs_today.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(f"Kept {len(out['kept'])} jobs. Sources: {out['sources']}. Rejected {out['rejectedCount']}. Wrote jobs_today.json")


def _selfcheck():
    js._selfcheck()
    assert pay_inr_lakh("USD 35000-55000 annual") == 29.4
    assert pay_inr_lakh("$4,000 - $5,000 per month") == 40.3
    assert pay_inr_lakh("$25–$30/hour") == 42.0
    assert pay_inr_lakh("INR 1200000-1700000/annual") == 12.0
    assert pay_inr_lakh("") is None
    j = {"title": "Backend Engineer", "desc": "Remote - available across all EU/LATAM countries. Python, React."}
    assert "region" in check_text(j, 4)
    j = {"title": "Full Stack Engineer", "desc": "3+ years with React, Node.js and TypeScript. Remote India."}
    assert check_text(j, 4) is None and j["yrs"] == 3
    j = {"title": "Full Stack Engineer", "desc": "6+ years React, Node.js"}
    assert check_text(j, 4) == "asks 6+ years"
    assert "floor" in check_text({"title": "Python Developer", "desc": "python fastapi", "pay": "INR 400000-600000 annual"}, 4)
    assert "not a fit" in check_text({"title": "AI Engineer - Equity Only", "desc": "python"}, 4)


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        _selfcheck(); print("selfcheck ok")
    else:
        main()
