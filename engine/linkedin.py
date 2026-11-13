"""LinkedIn public (logged-out) job search: no account, no password.

Uses the same guest endpoints a logged-out browser uses. Slow on purpose:
2 searches and 6 job pages, at most once an hour, rotated, with pauses. If LinkedIn
answers 429 we back off for six hours. Results go through the same checks as every other source.
"""
import html, re, time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
QUERIES = ["react node", "full stack developer", "mern stack", "next.js developer",
           "python fastapi", "node.js developer"]
PLACES = [{"location": "Hyderabad, Telangana, India"}, {"location": "India", "f_WT": "2"}]  # f_WT=2: remote
PAIRS = [(q, p) for q in QUERIES for p in PLACES]
PER_RUN = 2            # searches per run; all 12 are covered every 6 runs (6 h)
WINDOW_S = 7 * 3600    # each search looks back 7 h, so the 6-h rotation never leaves a gap
MAX_DETAILS = 6        # job pages read per run
EVERY_S = 55 * 60      # at most once an hour (the scheduler ticks every 30 min)
BLOCK_S = 6 * 3600     # back-off after a 429
PAUSE = 1.5
_state = {"next": 0, "blocked_until": 0.0, "last_run": 0.0}


def _get(url):
    with urlopen(Request(url, headers={"User-Agent": UA, "Accept-Language": "en-US"}), timeout=25) as r:
        return r.read().decode("utf-8", "replace")


def _text(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(s or ""))).strip()


def ago_to_dt(text, now):
    """'35 minutes ago' / '2 hours ago' / '3 days ago' -> datetime. None if unknown."""
    m = re.search(r"(\d+)\s*(second|minute|hour|day|week|month)", text or "", re.I)
    if not m:
        return now if re.search(r"just now", text or "", re.I) else None
    n, unit = int(m.group(1)), m.group(2).lower()
    secs = {"second": 1, "minute": 60, "hour": 3600, "day": 86400, "week": 604800, "month": 2592000}[unit]
    return now - timedelta(seconds=n * secs)


def parse_cards(page, now):
    out = []
    for li in re.split(r"<li[\s>]", page)[1:]:
        jid = re.search(r"jobPosting:(\d+)", li)
        title = re.search(r'base-search-card__title[^>]*>(.*?)</h3>', li, re.S)
        if not (jid and title):
            continue
        comp = re.search(r'base-search-card__subtitle[^>]*>(.*?)</h4>', li, re.S)
        loc = re.search(r'job-search-card__location[^>]*>(.*?)</span>', li, re.S)
        when = re.search(r'<time[^>]*>(.*?)</time>', li, re.S)
        out.append({"lid": jid.group(1), "title": _text(title.group(1)), "company": _text(comp.group(1) if comp else ""),
                    "loc": _text(loc.group(1) if loc else ""), "posted": ago_to_dt(_text(when.group(1) if when else ""), now),
                    "url": f"https://www.linkedin.com/jobs/view/{jid.group(1)}/"})
    return out


def parse_detail(page):
    d = re.search(r'show-more-less-html__markup[^>]*>(.*?)</div>', page, re.S)
    a = re.search(r'num-applicants__caption[^>]*>(.*?)<', page, re.S) or re.search(r'(Be among the first \d+ applicants)', page)
    return _text(d.group(1) if d else ""), _text(a.group(1) if a else "")


def collect(keep, now=None):
    """keep(job) -> bool is the shared title/location filter. Returns (jobs, errors)."""
    now = now or datetime.now(timezone.utc)
    if time.time() < _state["blocked_until"]:
        return [], ["LinkedIn: paused after a 429, resumes " +
                    datetime.fromtimestamp(_state["blocked_until"], timezone.utc).strftime("%H:%M UTC")]
    if time.time() - _state["last_run"] < EVERY_S:
        return [], []  # ran under an hour ago: skip quietly
    _state["last_run"] = time.time()
    picks = [PAIRS[(_state["next"] + i) % len(PAIRS)] for i in range(PER_RUN)]
    _state["next"] = (_state["next"] + PER_RUN) % len(PAIRS)
    seen, cards, errors = set(), [], []
    for q, place in picks:
        url = ("https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?" +
               urlencode({"keywords": q, **place, "f_TPR": f"r{WINDOW_S}", "sortBy": "DD", "start": 0}))
        try:
            for c in parse_cards(_get(url), now):
                c["remote"] = "f_WT" in place
                if c["lid"] not in seen:
                    seen.add(c["lid"]); cards.append(c)
        except HTTPError as e:
            if e.code == 429:
                _state["blocked_until"] = time.time() + BLOCK_S
                errors.append("LinkedIn: 429 rate limit, pausing 6 h")
                break
            errors.append(f"LinkedIn search '{q}': HTTP {e.code}")
        except Exception as e:
            errors.append(f"LinkedIn search '{q}': {e}")
        time.sleep(PAUSE)
    fresh = [c for c in cards if c["posted"] and keep(c)]
    for c in fresh[:MAX_DETAILS]:
        c["desc"], c["applicants"] = "", ""
        if time.time() < _state["blocked_until"]:
            continue  # blocked during this run: read no more job pages
        try:
            c["desc"], c["applicants"] = parse_detail(_get(f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{c['lid']}"))
        except HTTPError as e:
            if e.code == 429:
                _state["blocked_until"] = time.time() + BLOCK_S
                errors.append("LinkedIn: 429 rate limit on a job page, pausing 6 h")
            else:
                errors.append(f"LinkedIn job {c['lid']}: HTTP {e.code}")
        except Exception as e:
            errors.append(f"LinkedIn job {c['lid']}: {e}")
        time.sleep(PAUSE)
    for c in fresh:
        c["source"] = "LinkedIn"
        c["checks"] = ["Found in LinkedIn public search (no login)", f"Location: {c['where']}"] + \
                      ([f"Applicants: {c['applicants']}"] if c.get("applicants") else [])
    return fresh[:MAX_DETAILS], errors


def _selfcheck():
    now = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)
    assert ago_to_dt("35 minutes ago", now) == now - timedelta(minutes=35)
    assert ago_to_dt("2 hours ago", now) == now - timedelta(hours=2)
    assert ago_to_dt("Just now", now) == now
    assert ago_to_dt("", now) is None
    page = ('<li><div data-entity-urn="urn:li:jobPosting:4488288268" class="base-card">'
            '<h3 class="base-search-card__title">  Full Stack Developer &amp; React </h3>'
            '<h4 class="base-search-card__subtitle"><a href="#">Accordion</a></h4>'
            '<span class="job-search-card__location">Hyderabad, Telangana, India</span>'
            '<time class="job-search-card__listdate--new" datetime="2026-11-12">22 minutes ago</time></div></li>')
    c = parse_cards(page, now)
    assert len(c) == 1 and c[0]["lid"] == "4488288268" and c[0]["title"] == "Full Stack Developer & React"
    assert c[0]["company"] == "Accordion" and c[0]["loc"].startswith("Hyderabad")
    assert c[0]["posted"] == now - timedelta(minutes=22)
    d, a = parse_detail('<div class="show-more-less-html__markup x">2+ years <b>React</b></div>'
                        '<figcaption class="num-applicants__caption">Over 200 applicants</figcaption>')
    assert d == "2+ years React" and a == "Over 200 applicants"
    assert len(PAIRS) == 12 and len(PAIRS) % PER_RUN == 0
    assert WINDOW_S >= len(PAIRS) // PER_RUN * 3600  # rotation fits inside the look-back
