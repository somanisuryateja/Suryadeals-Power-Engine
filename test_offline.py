"""End-to-end run with fake sources (no network): filters -> DB -> Telegram -> API.

Run:  ENGINE_NO_SCHEDULER=1 python test_offline.py
"""
import json, os, tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ["ENGINE_NO_SCHEDULER"] = "1"
os.environ["ENGINE_DB"] = str(Path(tempfile.mkdtemp()) / "engine.db")
os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"] = "t", "1"

from app import main as m
from engine import daily_jobs as dj, job_scanner as js, linkedin, workday

now = datetime.now(timezone.utc)
iso = lambda **kw: (now - timedelta(**kw)).isoformat().replace("+00:00", "Z")
GH = {"jobs": [
    {"id": 1, "title": "Software Engineer II", "location": {"name": "Hyderabad, India"}, "first_published": iso(hours=3),
     "absolute_url": "https://gh/1", "company_name": "Acme"},
    {"id": 2, "title": "Software Engineer", "location": {"name": "Hyderabad"}, "first_published": iso(hours=5),
     "absolute_url": "https://gh/2", "company_name": "Acme"},
    {"id": 3, "title": "Software Engineer", "location": {"name": "Remote - US"}, "first_published": iso(hours=1),
     "absolute_url": "https://gh/3", "company_name": "Acme"},
    {"id": 4, "title": "Software Engineer", "location": {"name": "Hyderabad"}, "first_published": iso(days=5),
     "absolute_url": "https://gh/4", "company_name": "Acme"}]}
DETAIL = {1: "2+ years React, Node.js, TypeScript", 2: "4+ years Python, React"}


def fake_get_json(url):
    if url.endswith("/acme/jobs"):
        return GH
    if "/acme/jobs/" in url:
        return {"content": DETAIL[int(url.rsplit("/", 1)[1])]}
    raise RuntimeError("no network in test")


LI_LIST = ('<li><div data-entity-urn="urn:li:jobPosting:777"><h3 class="base-search-card__title">Full Stack Developer</h3>'
           '<h4 class="base-search-card__subtitle"><a>Roche</a></h4><span class="job-search-card__location">Hyderabad, Telangana, India</span>'
           '<time>18 minutes ago</time></div></li>'
           '<li><div data-entity-urn="urn:li:jobPosting:778"><h3 class="base-search-card__title">Lead Engineer</h3>'
           '<h4 class="base-search-card__subtitle"><a>X</a></h4><span class="job-search-card__location">Hyderabad</span>'
           '<time>1 hour ago</time></div></li>')
LI_DETAIL = ('<div class="show-more-less-html__markup">1-5 years React, Python FastAPI</div>'
             '<figcaption class="num-applicants__caption">47 applicants</figcaption>')


def fake_li_get(url):
    return LI_DETAIL if "/jobPosting/" in url else LI_LIST


def fake_wd_json(url, body=None):
    if url.endswith("/jobs"):
        if body["searchText"] != "Hyderabad" or "nvidia" not in url:
            return {"jobPostings": []}
        return {"jobPostings": [{"title": "Software Engineer", "externalPath": "/job/Hyderabad/SE_JR1",
                                 "locationsText": "Hyderabad, India", "postedOn": "Posted Today"}]}
    return {"jobPostingInfo": {"location": "Hyderabad, India", "jobDescription": "<p>2+ years Node.js, React</p>",
                               "externalUrl": "https://nvidia.wd5.myworkdayjobs.com/x/job/SE_JR1"}}


js.COMPANIES = {"greenhouse": ["acme"]}
js.get_json = fake_get_json
linkedin._get = fake_li_get
linkedin.PAUSE = 0
workday._json = fake_wd_json
sent = []
m.tg_send = lambda text: sent.append(text) or True

m.init_db()
with m.db() as con:  # an old run in the DB, like the live server: first run must still not flood
    con.execute("INSERT INTO runs VALUES (?,?)", (iso(days=1), json.dumps({"at": iso(days=1)})))
m.state["last_slow"] = 9e18          # skip Himalayas/HN (network) in this test
m.run_collect()                      # first run: primes alerts, sends nothing
assert m.state["error"] == "", m.state["error"]
titles = {(j["company"], j["source"]) for j in m.jobs()["jobs"]}
assert titles == {("Acme", "Greenhouse"), ("Roche", "LinkedIn"), ("NVIDIA", "Workday")}, titles
assert sent == [], "first run must not flood Telegram"

GH["jobs"].append({"id": 5, "title": "Full Stack Engineer", "location": {"name": "Hyderabad"}, "first_published": iso(minutes=10),
                   "absolute_url": "https://gh/5", "company_name": "Acme"})
DETAIL[5] = "3+ years React, Node.js"
m.run_collect()                      # second run: only the brand-new job is alerted
assert len(sent) == 1 and "Full Stack Engineer · Acme" in sent[0] and "3+ yrs asked" in sent[0], sent
m.run_collect()                      # third run: nothing new, nothing sent
assert len(sent) == 1
run = m.last_run()
assert run["sources"]["LinkedIn"] == 1 and run["sources"]["Workday"] == 1 and run["alerted"] == 0
note = next(j for j in m.jobs()["jobs"] if j["company"] == "Roche")["note"]
assert "3+ years" not in note and "Roche" in note

from fastapi.testclient import TestClient
r = TestClient(m.app).get("/api/jobs").json()
assert len(r["jobs"]) == 4 and r["lastRun"]["checked"] >= 4
print("offline end-to-end ok:", sorted(titles), "| Telegram alerts:", len(sent))
