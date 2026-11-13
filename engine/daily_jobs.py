"""Daily job collector for the Morning Jobs page.

Sources: company job boards (Greenhouse, Lever, Ashby, SmartRecruiters via
job_scanner.py), LinkedIn, Workday, Oracle career sites and the current
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
    from engine import job_scanner as js, linkedin, oracle_hcm, workday
except ImportError:  # run as a script from this folder
    import job_scanner as js, linkedin, oracle_hcm, workday

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
SENIOR = re.compile(r"\bsenior\b|\bsr\.?\b", re.I)
# Surya's rules (JOB_RULES_FINAL.md): min experience asked <= 3 yrs, pay above 12 LPA when shown.
MAX_YEARS = 3
PAY_FLOOR_LAKH = 12
BAD_DESC = re.compile(r"\boutsourcing\b|fixed price|paid on completion|data annotation|data labell?ing|equity[- ]only", re.I)
# A "remote" search hit whose JD pins the job to an office in another city (LinkedIn's remote filter is loose).
OTHER_CITY = (r"bengaluru|bangalore|pune|gurgaon|gurugram|noida|delhi|ncr|mumbai|chennai|kolkata|mohali|chandigarh|"
              r"ahmedabad|jaipur|indore|kochi|coimbatore|trivandrum|thiruvananthapuram|nagpur|lucknow|bhubaneswar")
ONSITE_ELSEWHERE = re.compile(r"(?:on-?site|in-office|work from (?:the |our )?office|based in|work from|relocate to|"
                              r"location\s*:)[^.]{0,60}?\b(" + OTHER_CITY + r")\b", re.I)
# Job boards that re-post someone else's listing (apply at the source instead), or re-dated old ones.
REPOST = re.compile(r"\bis the employer\b|\bjob radar\b|we track this posting|originally posted on", re.I)
# QA / testing roles that get past the title filter ("Automation Engineer"): by title, or by the JD's opening line.
QA_TITLE = re.compile(r"\bqa\b|quality assurance|\bsdet\b|test automation|automation test", re.I)
QA_HIRE = re.compile(r"(?:looking for|hiring|seeking)\s+an?\s+(?:[\w-]+\s+){0,3}?(?:qa|quality assurance|test|testing|sdet)\b", re.I)
# Stack words that only count as "alternatives" when the sentence offers a choice.
STACK_CHOICE = re.compile(r"e\.g|such as|for example|one or more|at least one|any of|\blike\b|nice to have|\bplus\b|"
                          r"bonus|preferred|\bor\b(?! (?:a |any )?similar| equivalent)", re.I)
# People posting themselves in the HN hiring thread ("I'm a software engineer...") rather than a job.
JOB_SEEKER = re.compile(r"\b(?:i'?m|i am)\s+an?\s+(?:[\w-]+\s+){0,3}?(?:engineer|developer|programmer)\b|"
                        r"looking for (?:a |my next )?(?:new )?(?:role|job|position|opportunit)|open to (?:work|opportunit)|"
                        r"\bhire me\b|my (?:resume|cv)\b", re.I)
SENTENCE_END = re.compile(r"(?<!e\.g)(?<!i\.e)[.!?]\s+(?=[A-Z0-9])|[•;\n]")


def required_bad_stack(d):
    """Java / .NET / PHP ... named as a requirement, not as one option among several."""
    for m in BAD_STACK.finditer(d):
        start = max([e.end() for e in SENTENCE_END.finditer(d, 0, m.start())] + [m.start() - 100, 0])
        end = min([e.start() for e in [SENTENCE_END.search(d, m.end())] if e] + [m.end() + 100])
        if not STACK_CHOICE.search(d[start:end]):
            return m.group(0)
    return None


def job_id(url):
    return hashlib.sha1(url.encode()).hexdigest()[:16]


CO_SUFFIX = re.compile(r"\b(inc|llc|ltd|limited|pvt|private|corp|corporation|plc|gmbh|technologies)\b")


def same_job_key(j):
    """company + title + city: the same opening found on two sources (links differ).
    ponytail: two real openings with the same title in the same city collapse into one."""
    norm = lambda s: " ".join(re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).split())
    return f"{' '.join(CO_SUFFIX.sub(' ', norm(j['company'])).split())}|{norm(j['title'])}|{j.get('where', '')}"


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


def check_text(job, max_years=MAX_YEARS, pay_floor=PAY_FLOOR_LAKH):
    """Shared checks on the job text. Returns a reason string if rejected."""
    d = job.get("desc") or ""
    if not d.strip():  # e.g. LinkedIn refused the job page: nothing to check years or stack against
        return "no job description to check (retried on a later run)"
    if REPOST.search(d):
        return "re-posted by a job board: " + REPOST.search(d).group(0)
    if QA_TITLE.search(job["title"]) or QA_HIRE.search(d[:300]):
        return "QA / testing role"
    m = ONSITE_ELSEWHERE.search(d)
    if m and job.get("where") != "Hyderabad" and not re.search(r"hyderabad|\bremote\b", d, re.I):
        return f"office job in {m.group(1).title()}"
    bad = required_bad_stack(d)
    if bad:
        return f"needs {bad}"
    if REGION_LOCK.search(d):
        return "locked to another region: " + REGION_LOCK.search(d).group(0)
    if BAD_STACK.search(job["title"]):
        return "stack outside rules"
    if BAD_TITLE.search(job["title"]):
        return "not a fit: " + BAD_TITLE.search(job["title"]).group(0)
    if BAD_DESC.search(d):
        return "not a fit: " + BAD_DESC.search(d).group(0)
    lakh = pay_inr_lakh(job.get("pay"))
    if lakh is not None and lakh <= pay_floor:
        return f"pay about {lakh}L, not above your {pay_floor}L floor"
    yrs = js.years_required(d)
    job["yrs"] = yrs
    if yrs and yrs > max_years:
        return f"asks {yrs}+ years"
    if SENIOR.search(job["title"]) and not yrs:
        return "senior title and years not stated"
    job["stack"] = sorted({m.lower().replace(".", "") for m in js.MY_STACK.findall(d)})
    if not job["stack"]:
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


# --- source 2: HN "Who is hiring" (comment date = posting date) ----------
def from_hn(days, now):
    s = js.get_json("https://hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring&hitsPerPage=6")
    story = next(h for h in s["hits"] if h["title"].startswith("Ask HN: Who is hiring?"))
    item = js.get_json(f"https://hn.algolia.com/api/v1/items/{story['objectID']}")
    out = []
    for c in item.get("children", []):
        t = strip(c.get("text"))
        posted = js.iso(c["created_at"])
        if not t or (now - posted).total_seconds() > days * 86400:
            continue
        head = t.split("|")
        if len(head) < 3 or not re.search(r"\b(india|apac|asia|hyderabad)\b", t[:400], re.I) or JOB_SEEKER.search(t[:300]):
            continue
        if not js.MY_STACK.search(t) or re.search(r"\b(senior|staff|principal|lead)\b", t[:200], re.I) and not re.search(r"\bjunior|mid\b", t[:200], re.I):
            continue
        links = re.findall(r'href="([^"]+)"', html.unescape(c.get("text") or ""))
        out.append({"company": head[0].strip()[:60], "title": head[1].strip()[:100], "loc": t[:200], "desc": t,
                    "url": links[0] if links else f"https://news.ycombinator.com/item?id={c['id']}",
                    "hn": f"https://news.ycombinator.com/item?id={c['id']}", "posted": posted, "pay": "",
                    "where": "Remote (check post)", "source": "HN Who is hiring",
                    "checks": ["Posted by the company in the HN thread", "Mentions India / APAC in the header"]})
    return out


def from_linkedin(days, now):
    got, errs = linkedin.collect(js.keep, now)
    return [j for j in got if (now - j["posted"]).total_seconds() <= days * 86400], errs


def from_workday(days, now):
    return workday.collect(js.keep, max_days=max(1, round(days)), now=now)


def from_oracle(days, now):
    return oracle_hcm.collect(js.keep, max_days=max(1, round(days)), now=now)


def collect(days=2, max_years=MAX_YEARS, slow=True):
    """Run every source and checks; return the run as a dict.
    slow=False skips HN (heavy, slow-changing) on the frequent runs."""
    now = datetime.now(timezone.utc)
    boards, n_boards, errors = from_boards(days, now)
    counts = {"Company job boards": len(boards)}
    jobs = list(boards)
    for name, fn in [("LinkedIn", from_linkedin), ("Workday", from_workday), ("Oracle HCM", from_oracle)]:
        try:
            got, errs = fn(days, now)
            errors += errs
        except Exception as e:
            got, errors = [], errors + [f"{name}: {e}"]
        counts[name] = len(got)
        jobs += got
    for name, fn in [("HN Who is hiring", from_hn)] if slow else []:
        try:
            got = fn(days, now)
        except Exception as e:
            got, errors = [], errors + [f"{name}: {e}"]
        counts[name] = len(got)
        jobs += got

    kept, rejected, seen, keys = [], [], set(), set()
    for j in jobs:
        key = j["url"]
        if key in seen:
            continue
        seen.add(key)
        # one gate for every source: older jobs would be saved, then pruned, yet still alerted
        why = "older than the look-back" if (now - j["posted"]).total_seconds() > days * 86400 \
            else check_text(j, max_years)
        if why:
            rejected.append(f"{j['company']} | {j['title']}: {why}")
            continue
        k = same_job_key(j)  # sources run boards-first, so the company's own ATS link wins
        if k in keys:
            rejected.append(f"{j['company']} | {j['title']}: same job already found on another source")
            continue
        keys.add(k)
        lakh = pay_inr_lakh(j.get("pay"))
        if j.get("yrs"):
            j["checks"].append(f"Asks {j['yrs']}+ years")
        kept.append({"id": job_id(j["url"]), "company": j["company"], "title": j["title"], "url": j["url"],
                     "where": j["where"], "loc": (j.get("loc") or "")[:120], "postedAt": j["posted"].isoformat(),
                     "years": j.get("yrs"), "stack": j.get("stack", []), "pay": j.get("pay") or "",
                     "payLakh": lakh, "pay25": bool(lakh and lakh >= 25), "source": j["source"],
                     "checks": j["checks"], "hn": j.get("hn", ""), "applicants": j.get("applicants", ""), "verify": j.get("verify", ""), "desc": (j.get("desc") or "")[:1500]})
    kept.sort(key=lambda j: (not j["pay25"], -datetime.fromisoformat(j["postedAt"]).timestamp()))
    return {"runAt": now.isoformat(), "days": days, "companiesChecked": n_boards + len(workday.SITES) + len(oracle_hcm.SITES), "sources": counts,
            "kept": kept, "rejectedCount": len(rejected), "rejectedSample": rejected[:40], "errors": errors[:40]}


def probe():
    """Live check of every source, one small request each. Run on the server after deploy."""
    print("Company job boards:")
    for ats, slugs in js.COMPANIES.items():
        for s in slugs:
            found, err = js.scan(ats, s)
            print(f"  {'FAIL' if err else 'ok  '}  {ats:15} {s:28} {err or str(len(found)) + ' jobs'}")
    print("Workday:")
    workday.probe()
    print("Oracle HCM:")
    oracle_hcm.probe()
    print("LinkedIn (1 search):")
    try:
        cards = linkedin.parse_cards(linkedin._get("https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?" +
                                     urlencode({"keywords": "react node", "location": "Hyderabad, Telangana, India",
                                                "f_TPR": "r86400", "sortBy": "DD"})), datetime.now(timezone.utc))
        print(f"  ok    LinkedIn returned {len(cards)} jobs from the last 24 h")
    except Exception as e:
        print(f"  FAIL  LinkedIn: {e}")
    for name, url in [("HN", "https://hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring&hitsPerPage=1")]:
        try:
            js.get_json(url); print(f"  ok    {name}")
        except Exception as e:
            print(f"  FAIL  {name}: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=2)
    ap.add_argument("--max-years", type=int, default=MAX_YEARS)
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
    linkedin._selfcheck(); workday._selfcheck(); oracle_hcm._selfcheck()
    assert "region" in check_text(j, 4)
    j = {"title": "Full Stack Engineer", "desc": "3+ years with React, Node.js and TypeScript. Remote India."}
    assert check_text(j, 4) is None and j["yrs"] == 3
    a = {"company": "ServiceNow, Inc.", "title": "Software Engineer (Full-Stack)", "where": "Hyderabad"}
    assert same_job_key(a) == same_job_key({"company": "ServiceNow", "title": "software engineer full stack", "where": "Hyderabad"})
    assert same_job_key(a) != same_job_key(dict(a, where="Remote (India/APAC)"))
    j = {"title": "Full Stack Engineer", "desc": "6+ years React, Node.js"}
    assert check_text(j, 4) == "asks 6+ years"
    assert "floor" in check_text({"title": "Python Developer", "desc": "python fastapi", "pay": "INR 400000-600000 annual"}, 4)
    assert "floor" in check_text({"title": "Python Developer", "desc": "python fastapi", "pay": "INR 1100000-1200000 annual"})
    assert check_text({"title": "Python Developer", "desc": "python fastapi", "pay": "INR 1400000-1800000 annual"}) is None
    assert check_text({"title": "Full Stack Engineer", "desc": "4+ years React"}) == "asks 4+ years"
    assert check_text({"title": "Full Stack Engineer", "desc": "3+ years React"}) is None
    assert check_text({"title": "Senior Software Engineer", "desc": "React, Node"}) == "senior title and years not stated"
    assert check_text({"title": "Senior Software Engineer 2", "desc": "1-3 years React, Node"}) is None
    assert "not a fit" in check_text({"title": "AI Engineer - Equity Only", "desc": "python"}, 4)
    # the six leaks found on 13 Nov, in the JDs' own words; each must be rejected...
    bad = {
        "no job description": {"title": "Software Engineer II", "desc": ""},  # Microsoft, LinkedIn page refused
        "Mohali": {"title": "Full Stack Developer", "where": "Remote (India/APAC)", "desc":
                   "Production experience with React or Next.js, and Node.js with Express. "
                   "Location: SEVOS office, Sector 75, Mohali (onsite). Hours: Full-time"},
        "Noida": {"title": "Software Engineer (Full Stack)", "where": "Remote (India/APAC)", "desc":
                  "2-3 years. One frontend stack (e.g. React, Next.js) and one backend stack (e.g. Node.js, Python, Java, Go). "
                  "Based in or willing to work from Noida (Delhi NCR)"},
        "needs Java": {"title": "Software Engineer", "where": "Hyderabad", "desc":
                       "2+ years of software industry experience. Expertise in fullstack engineering: HTML, CSS, "
                       "Javascript, Java, JUnit, REST, JSON. Frontend technologies, with solid JavaScript experience"},
        "needs Java ": {"title": "Applications Dev Engineer", "where": "Hyderabad", "desc":
                        "on the function or industry. 2-5 years of experience with Java or a similar OO language. "
                        "Passion for JavaScript and the Web. Experience with React"},
        "re-posted": {"title": "Software Engineer", "where": "Hyderabad", "desc":
                      "Servicenow is the employer — EngRadar is a job radar, not a recruiter. 2+ years JavaScript"},
        "QA": {"title": "Automation Engineer, Senior Associate", "where": "Hyderabad", "desc":
               "We are looking for a QA Automation Engineer with 3+ years of experience to design automated tests. Python"},
    }
    for want, j in bad.items():
        got = check_text(j) or ""
        assert want.strip() in got, (want, got)
    # ...while these still pass: Java only offered as one choice, a city named without an office rule
    ok = [{"title": "Software Engineer", "where": "Hyderabad", "desc":
           "experience in at least one modern programming language (e.g., Java, Python, JavaScript, C#, or Go). REST APIs"},
          {"title": "AI Developer", "where": "Remote (India/APAC)", "desc":
           "our Chennai studio (ISO 27001:2022 certified) runs the production. 1-2 years with React and a Node.js backend"},
          {"title": "Full Stack Engineer", "where": "Remote (India/APAC)", "desc":
           "We are a team based in Bangalore hiring remote engineers across India. 3+ years React, Node.js"}]
    for j in ok:
        assert check_text(j) is None, (j["title"], check_text(j))
    # SmartRecruiters / Greenhouse HTML: bullets must stay separate lines, or "function or industry" next
    # to "2-5 years of Java" reads like a list of options (ServiceNow leak, 13 Nov 11:30)
    sr = ("<ul><li>Think about AI's potential impact on the function or industry.</li>"
          "<li>2-5 years of experience with Java or a similar OO language</li><li>Passion for JavaScript and React</li></ul>")
    assert check_text({"title": "Applications Dev Engineer", "where": "Hyderabad", "desc": js.html_text(sr)}) == "needs Java"
    assert js.html_text("&lt;p&gt;React &amp;amp; Node&lt;/p&gt;&lt;p&gt;Remote&lt;/p&gt;") == "React & Node\nRemote"
    assert JOB_SEEKER.search("Hemant Singh | Software Engineer | India (REMOTE) I'm a software engineer focused on backend")
    assert not JOB_SEEKER.search("Acme | Full Stack Engineer | Remote (India) | We're hiring engineers who like React")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        _selfcheck(); print("selfcheck ok")
    elif "--probe" in sys.argv:
        probe()
    else:
        main()
