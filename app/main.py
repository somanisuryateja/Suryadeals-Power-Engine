"""Suryadeals Power Engine: a source-checked job feed with Telegram alerts.

FastAPI app. Collects jobs with engine/daily_jobs.py every 30 minutes from
8:00 AM to 11:00 PM IST (and on demand, at most once per 15 minutes), stores
them in SQLite, sends each new match to Telegram once, and serves one page
where each job can be marked applied or skipped.

Telegram: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in /etc/power-engine.env.
Find the chat id:  python -m app.main --telegram-chat-id   (after pressing Start in the bot)
Send a test:       python -m app.main --telegram-test

Run locally:  uvicorn app.main:app --port 8010
Import jobs:  python -m app.main --import DIR   (DIR of <id>.json job files)
Mark jobs:    python -m app.main --mark skipped id1,id2
"""
import json, os, sqlite3, sys, threading, time
from contextlib import asynccontextmanager
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from engine import daily_jobs

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("ENGINE_DB", ROOT / "data" / "engine.db"))
SIGNATURE = os.environ.get("ENGINE_SIGNATURE", "")  # name, phone, email: kept out of the repo
IST = timezone(timedelta(hours=5, minutes=30))
REFRESH_GAP = 15 * 60
KEEP_DAYS = 7                    # page shows a week
LOOKBACK_DAYS = 2                # each run only looks at jobs posted in the last 2 days
EVERY_MIN = 30                   # run every 30 min ...
DAY_START, DAY_END = 8, 23       # ... between 08:00 and 23:00 IST
SLOW_EVERY_S = 3 * 3600          # HN at most every 3 h
MAX_ALERTS = 15                  # per run; more are summarised in one message
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")

@asynccontextmanager
async def lifespan(_app):
    startup()
    yield


app = FastAPI(title="Suryadeals Power Engine", lifespan=lifespan)
run_lock = threading.Lock()


# --- storage ---------------------------------------------------------------
def db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, posted_at TEXT, added_at TEXT, body TEXT);
            CREATE TABLE IF NOT EXISTS marks (id TEXT PRIMARY KEY, status TEXT, at TEXT);
            CREATE TABLE IF NOT EXISTS runs (at TEXT, body TEXT);
            CREATE TABLE IF NOT EXISTS alerts (id TEXT PRIMARY KEY, at TEXT);
        """)


def save_jobs(jobs, replace=False):
    """Insert new jobs (existing ones keep their notes unless replace). Returns rows written."""
    now = datetime.now(timezone.utc).isoformat()
    with db() as con:
        # same opening already saved from another source (different link): keep the first one
        taken = {daily_jobs.same_job_key(json.loads(r["body"])): r["id"] for r in con.execute("SELECT id, body FROM jobs")}
        jobs = [j for j in jobs if taken.get(daily_jobs.same_job_key(j), j["id"]) == j["id"]]
        before = con.total_changes
        con.executemany(f"INSERT OR {'REPLACE' if replace else 'IGNORE'} INTO jobs VALUES (?,?,?,?)",
                        [(j["id"], j["postedAt"], now, json.dumps(j, ensure_ascii=False)) for j in jobs])
        added = con.total_changes - before
        cut = (datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)).isoformat()
        con.execute("DELETE FROM jobs WHERE posted_at < ? AND id NOT IN (SELECT id FROM marks)", (cut,))
    return added


# --- notes -----------------------------------------------------------------
# ponytail: template notes from resume facts; swap for Claude-written notes when an API key exists.
FACTS = {
    "ai": "At Codegnan I built async FastAPI services connecting LiveKit with STT, LLM and TTS models "
          "for a real-time AI interview product, at about 650 ms end to end.",
    "api": "I built 156 REST endpoints and ~24 data models for an LMS serving 5,000+ users "
           "(React, Node.js, Express, MongoDB).",
    "perf": "I cut a key MongoDB query from ~20 s to 100 ms and brought average API response time "
            "from ~1 s to 40-50 ms.",
    "pay": "I integrated Razorpay with signature checks, webhooks and idempotency keys, plus billing "
           "automation that saved 15+ manual hours a week.",
}
AI_WORDS = {"llm", "livekit", "webrtc", "fastapi", "python"}


def make_note(job):
    stack = set(job.get("stack") or [])
    names = {"react": "React", "nodejs": "Node.js", "node": "Node.js", "typescript": "TypeScript", "python": "Python",
             "fastapi": "FastAPI", "mongodb": "MongoDB", "nextjs": "Next.js", "llm": "LLMs"}
    shown = list(dict.fromkeys(names[s] for s in names if s in stack))[:4]
    ai_role = bool(stack & AI_WORDS) or "ai" in job["title"].lower()
    picks = ["ai", "api"] if ai_role else ["api", "perf"]
    if "payment" in (job.get("desc") or "").lower():
        picks[-1] = "pay"
    # Rule: describe the work, never "3+ years", in free text.
    lead = "Hi, I'm a full-stack engineer" + (f" working mostly in {', '.join(shown)}." if shown else ".")
    body = " ".join(FACTS[p] for p in picks)
    close = f"I'd like to bring that to {job['company']}. I'm in Hyderabad and can join immediately."
    return f"{lead} {body}\n\n{close}" + (f"\n\n{SIGNATURE}" if SIGNATURE else "")


# --- runs ------------------------------------------------------------------
state = {"running": False, "error": "", "last_slow": 0.0}


# --- Telegram ----------------------------------------------------------------
def tg_send(text):
    if not (TG_TOKEN and TG_CHAT):
        return False
    data = urlencode({"chat_id": TG_CHAT, "text": text, "disable_web_page_preview": "true"}).encode()
    with urlopen(Request(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data=data), timeout=20) as r:
        return json.load(r).get("ok", False)


def alert_text(j, now=None):
    now = now or datetime.now(timezone.utc)
    mins = (now - datetime.fromisoformat(j["postedAt"])).total_seconds() / 60
    age = f"{mins:.0f} min ago" if mins < 90 else f"{mins / 60:.0f} h ago" if mins < 48 * 60 else f"{mins / 1440:.0f} days ago"
    yrs = f"{j['years']}+ yrs asked" if j.get("years") else "⚠️ years not stated, check the JD"
    pay = f"~{j['payLakh']}L" if j.get("payLakh") else "pay not listed"
    lines = [f"🟢 NEW JOB · posted {age}", f"{j['title']} · {j['company']}", f"📍 {j['where']} · {yrs} · {pay}",
             f"Source: {j['source']}" + (f" · {j['applicants']}" if j.get("applicants") else "")]
    if j.get("stack"):
        lines.append("Stack: " + ", ".join(j["stack"][:6]))
    lines.append(f"🔗 {j['url']}")
    return "\n".join(lines)


def send_alerts(jobs, prime=False):
    """Alert each job once. prime=True marks jobs as alerted without sending (first run, no flood)."""
    with db() as con:
        done = {r["id"] for r in con.execute("SELECT id FROM alerts")}
        done_keys = {daily_jobs.same_job_key(json.loads(r["body"]))
                     for r in con.execute("SELECT j.body FROM jobs j JOIN alerts a ON a.id = j.id")}
    new = [j for j in jobs if j["id"] not in done and daily_jobs.same_job_key(j) not in done_keys]
    sent = 0
    if prime or not (TG_TOKEN and TG_CHAT):
        done_now = new  # first run / no Telegram: record only
    else:  # mark only what Telegram accepted; anything unsent is retried on the next run
        done_now = []
        try:
            for j in new[:MAX_ALERTS]:
                if not tg_send(alert_text(j)):
                    raise RuntimeError("message not accepted")
                done_now.append(j)
                sent += 1
            rest = new[MAX_ALERTS:]
            if rest and tg_send(f"+{len(rest)} more new jobs on the page: https://searchengine.suryadeals.com/"):
                done_now += rest
        except Exception as e:
            state["error"] = f"Telegram: {e}"[:300]
    now = datetime.now(timezone.utc).isoformat()
    with db() as con:
        con.executemany("INSERT OR IGNORE INTO alerts VALUES (?,?)", [(j["id"], now) for j in done_now])
    return sent


def last_run():
    with db() as con:
        row = con.execute("SELECT at, body FROM runs ORDER BY at DESC LIMIT 1").fetchone()
    return json.loads(row["body"]) if row else None


def run_collect():
    if not run_lock.acquire(blocking=False):
        return False
    state.update(running=True, error="")
    try:
        slow = time.time() - state["last_slow"] >= SLOW_EVERY_S
        with db() as con:  # no alert history yet (fresh DB or first run after this update): don't flood
            first = con.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 0
        out = daily_jobs.collect(days=LOOKBACK_DAYS, slow=slow)
        if slow:
            state["last_slow"] = time.time()
        for j in out["kept"]:
            j["note"] = make_note(j)
            j["desc"] = j.get("desc", "")[:600]
        added = save_jobs(out["kept"])
        alerted = send_alerts(out["kept"], prime=first)
        meta = {"at": out["runAt"], "checked": len(out["kept"]) + out["rejectedCount"], "added": added,
                "alerted": alerted, "sources": out["sources"],
                "errors": out["errors"][:10] + ([state["error"]] if state["error"] else [])}  # Telegram failures too
        with db() as con:
            con.execute("INSERT INTO runs VALUES (?,?)", (meta["at"], json.dumps(meta)))
    except Exception as e:  # keep the server up; show the error on the page
        state["error"] = str(e)[:300]
    finally:
        state["running"] = False
        run_lock.release()
    return True


def next_run(now):
    """Next :00 or :30 slot between 08:00 and 23:00 IST."""
    t = now.astimezone(IST).replace(second=0, microsecond=0)
    t = t.replace(minute=(t.minute // EVERY_MIN) * EVERY_MIN) + timedelta(minutes=EVERY_MIN)
    if t.hour < DAY_START:
        t = t.replace(hour=DAY_START, minute=0)
    elif t.hour > DAY_END or (t.hour == DAY_END and t.minute > 0):
        t = (t + timedelta(days=1)).replace(hour=DAY_START, minute=0)
    return t


def scheduler():
    run_collect()  # once at start-up (also primes alerts on a fresh database)
    while True:
        target = next_run(datetime.now(timezone.utc))
        # Check the wall clock every 5 min: one long sleep woke an hour late on the VM.
        while (left := (target - datetime.now(timezone.utc)).total_seconds()) > 0:
            time.sleep(min(300, left))
        run_collect()


def startup():
    init_db()
    if "pytest" not in sys.modules and os.environ.get("ENGINE_NO_SCHEDULER") != "1":
        threading.Thread(target=scheduler, daemon=True).start()


# --- API -------------------------------------------------------------------
class Mark(BaseModel):
    status: str | None = None


@app.get("/api/health")
def health():
    return {"status": "healthy"}


@app.get("/api/jobs")
def jobs():
    with db() as con:
        rows = con.execute("SELECT body FROM jobs").fetchall()
        marks = {r["id"]: {"status": r["status"], "at": r["at"]} for r in con.execute("SELECT * FROM marks")}
    items = [json.loads(r["body"]) for r in rows]
    for j in items:
        j.pop("desc", None)
    return {"jobs": items, "marks": marks, "lastRun": last_run(), "running": state["running"], "error": state["error"],
            "nextRun": next_run(datetime.now(timezone.utc)).isoformat(), "refreshGapMin": REFRESH_GAP // 60}


@app.post("/api/jobs/{job_id}/mark")
def mark(job_id: str, m: Mark):
    if m.status not in (None, "applied", "skipped"):
        raise HTTPException(400, "status must be applied, skipped or null")
    with db() as con:
        if not con.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone():
            raise HTTPException(404, "job not found")
        if m.status:
            con.execute("INSERT OR REPLACE INTO marks VALUES (?,?,?)", (job_id, m.status, datetime.now(timezone.utc).isoformat()))
        else:
            con.execute("DELETE FROM marks WHERE id=?", (job_id,))
    return {"ok": True}


@app.post("/api/refresh")
def refresh():
    if state["running"]:
        raise HTTPException(409, "A search is already running.")
    r = last_run()
    if r:
        wait = REFRESH_GAP - (datetime.now(timezone.utc) - datetime.fromisoformat(r["at"])).total_seconds()
        if wait > 0:
            raise HTTPException(429, f"Last search was under 15 minutes ago. Try again in {int(wait // 60) + 1} min.")
    threading.Thread(target=run_collect, daemon=True).start()
    return {"started": True}


@app.get("/")
def page():
    return FileResponse(ROOT / "app" / "static" / "index.html")


# --- CLI: import job files (e.g. notes written by Claude) -------------------
def import_dir(path):
    """Load hand-checked jobs (their notes replace template ones)."""
    init_db()
    jobs = []
    for f in Path(path).glob("*.json"):
        if f.stem in ("meta", "writes"):
            continue
        d = json.loads(f.read_text(encoding="utf-8"))
        d["id"] = f.stem
        if SIGNATURE and d.get("note") and SIGNATURE not in d["note"]:
            d["note"] = d["note"].rsplit("\n\n", 1)[0] + f"\n\n{SIGNATURE}"
        jobs.append(d)
    print(f"Imported {save_jobs(jobs, replace=True)} jobs from {path}")


def set_marks(ids, status):
    """Mark jobs from the command line, e.g. old posts found by a manual check."""
    init_db()
    with db() as con:
        con.executemany("INSERT OR REPLACE INTO marks VALUES (?,?,?)",
                        [(i, status, datetime.now(timezone.utc).isoformat()) for i in ids])
    print(f"Marked {len(ids)} as {status}")


def _selfcheck():
    job = {"company": "Acme", "title": "AI Engineer", "stack": ["python", "fastapi", "llm"], "desc": ""}
    n = make_note(job)
    assert "3+ years" not in n and "650 ms" in n and "Acme" in n
    job = {"company": "Shop", "title": "Full Stack Developer", "stack": ["react", "nodejs"], "desc": "payments with stripe"}
    n = make_note(job)
    assert "156 REST" in n and "Razorpay" in n
    ist = lambda h, m, d=12: datetime(2026, 11, d, h, m, tzinfo=IST)
    assert next_run(ist(7, 10)) == ist(8, 0)
    assert next_run(ist(8, 0)) == ist(8, 30)
    assert next_run(ist(10, 29)) == ist(10, 30)
    assert next_run(ist(22, 45)) == ist(23, 0)
    assert next_run(ist(23, 0)) == ist(8, 0, 13)
    assert next_run(ist(23, 50)) == ist(8, 0, 13)
    j = {"id": "x", "title": "Full Stack Developer", "company": "Roche", "where": "Hyderabad", "years": 1,
         "payLakh": None, "source": "LinkedIn", "applicants": "47 applicants", "stack": ["react", "python"],
         "url": "https://www.linkedin.com/jobs/view/1/", "postedAt": (datetime.now(timezone.utc) - timedelta(minutes=18)).isoformat()}
    t = alert_text(j)
    assert "posted 18 min ago" in t and "Roche" in t and "1+ yrs asked" in t and "47 applicants" in t and t.endswith("/1/")
    # alerts: prime marks without sending; a job is never alerted twice
    global DB_PATH
    import tempfile
    DB_PATH = Path(tempfile.mkdtemp()) / "t.db"; init_db()
    assert send_alerts([j], prime=True) == 0
    with db() as con:
        assert con.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 1
    assert send_alerts([j]) == 0  # already alerted
    assert "⚠️ years not stated" in alert_text(dict(j, years=None))


if __name__ == "__main__":
    if "--import" in sys.argv:
        import_dir(sys.argv[sys.argv.index("--import") + 1])
    elif "--mark" in sys.argv:  # --mark applied|skipped id1,id2,...
        i = sys.argv.index("--mark")
        set_marks(sys.argv[i + 2].split(","), sys.argv[i + 1])
    elif "--telegram-chat-id" in sys.argv:  # press Start in the bot first
        with urlopen(f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates", timeout=20) as r:
            ids = {u["message"]["chat"]["id"] for u in json.load(r).get("result", []) if "message" in u}
        print("Chat id(s):", ", ".join(map(str, ids)) or "none: open the bot in Telegram, press Start, try again")
    elif "--telegram-test" in sys.argv:
        print("sent" if tg_send("✅ Suryadeals Power Engine: Telegram alerts are working.") else
              "not sent: check TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
    elif "--selfcheck" in sys.argv:
        _selfcheck(); print("selfcheck ok")
