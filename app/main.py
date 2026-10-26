"""Suryadeals Power Engine: a daily, source-checked job feed.

FastAPI app. Collects jobs with engine/daily_jobs.py every day at 8:00 AM IST
(and on demand, at most once per 15 minutes), stores them in SQLite, and
serves one page where each job can be marked applied or skipped.

Run locally:  uvicorn app.main:app --port 8010
Import jobs:  python -m app.main --import DIR   (DIR of <id>.json job files)
"""
import json, os, sqlite3, sys, threading, time
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
KEEP_DAYS = 21

app = FastAPI(title="Suryadeals Power Engine")
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
        """)


def save_jobs(jobs):
    """Insert new jobs only; existing ones keep their notes. Returns how many were new."""
    now = datetime.now(timezone.utc).isoformat()
    with db() as con:
        before = con.total_changes
        con.executemany("INSERT OR IGNORE INTO jobs VALUES (?,?,?,?)",
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
    shown = [s for s in ["react", "nodejs", "node", "typescript", "python", "fastapi", "mongodb", "nextjs", "llm"] if s in stack][:4]
    ai_role = bool(stack & AI_WORDS) or "ai" in job["title"].lower()
    picks = ["ai", "api"] if ai_role else ["api", "perf"]
    if "payment" in (job.get("desc") or "").lower():
        picks[-1] = "pay"
    lead = f"Hi, I'm a full-stack engineer with 3+ years of experience" + (f", mostly {', '.join(shown)}." if shown else ".")
    body = " ".join(FACTS[p] for p in picks)
    close = f"I'd like to bring that to {job['company']}. I'm in Hyderabad and can join immediately."
    return f"{lead} {body}\n\n{close}" + (f"\n\n{SIGNATURE}" if SIGNATURE else "")


# --- runs ------------------------------------------------------------------
state = {"running": False, "error": ""}


def last_run():
    with db() as con:
        row = con.execute("SELECT at, body FROM runs ORDER BY at DESC LIMIT 1").fetchone()
    return json.loads(row["body"]) if row else None


def run_collect():
    if not run_lock.acquire(blocking=False):
        return False
    state.update(running=True, error="")
    try:
        out = daily_jobs.collect(days=7, max_years=4)
        for j in out["kept"]:
            j["note"] = make_note(j)
            j["desc"] = j.get("desc", "")[:600]
        added = save_jobs(out["kept"])
        meta = {"at": out["runAt"], "checked": len(out["kept"]) + out["rejectedCount"], "added": added,
                "sources": out["sources"], "errors": out["errors"][:10]}
        with db() as con:
            con.execute("INSERT INTO runs VALUES (?,?)", (meta["at"], json.dumps(meta)))
    except Exception as e:  # keep the server up; show the error on the page
        state["error"] = str(e)[:300]
    finally:
        state["running"] = False
        run_lock.release()
    return True


def next_8am_ist(now):
    t = now.astimezone(IST).replace(hour=8, minute=0, second=0, microsecond=0)
    return t if t > now else t + timedelta(days=1)


def scheduler():
    if not last_run():
        run_collect()
    while True:
        now = datetime.now(timezone.utc)
        time.sleep(max(1, (next_8am_ist(now) - now).total_seconds()))
        run_collect()


@app.on_event("startup")
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
    return {"jobs": items, "marks": marks, "lastRun": last_run(), "running": state["running"], "error": state["error"]}


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
    init_db()
    jobs, marks = [], []
    for f in Path(path).glob("*.json"):
        d = json.loads(f.read_text(encoding="utf-8"))
        if f.stem in ("meta", "writes"):
            continue
        d["id"] = f.stem
        jobs.append(d)
    added = save_jobs(jobs)
    print(f"Imported {added} new jobs from {path}")


def _selfcheck():
    job = {"company": "Acme", "title": "AI Engineer", "stack": ["python", "fastapi", "llm"], "desc": ""}
    n = make_note(job)
    assert "3+ years" in n and "650 ms" in n and "Acme" in n
    job = {"company": "Shop", "title": "Full Stack Developer", "stack": ["react", "nodejs"], "desc": "payments with stripe"}
    n = make_note(job)
    assert "156 REST" in n and "Razorpay" in n
    assert next_8am_ist(datetime(2026, 10, 26, 1, 0, tzinfo=timezone.utc)).hour == 8
    assert next_8am_ist(datetime(2026, 10, 26, 3, 0, tzinfo=timezone.utc)).day == 27


if __name__ == "__main__":
    if "--import" in sys.argv:
        import_dir(sys.argv[sys.argv.index("--import") + 1])
    elif "--selfcheck" in sys.argv:
        _selfcheck(); print("selfcheck ok")
