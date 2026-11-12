# Suryadeals Power Engine

A daily job feed that finds fresh software roles open to Hyderabad or remote India and checks each one at its source before listing it.

Most job boards show reposts, region-locked "remote" roles and listings that were filled weeks ago. This engine reads company job boards directly and only keeps jobs that pass every check.

## What it does

Every 30 minutes from 8:00 AM to 11:00 PM IST, and on demand from the page (at most once per 15 minutes):

1. **Collects** jobs posted in the last 2 days from:
   - about 104 company job boards through their public APIs (Greenhouse, Lever, Ashby, SmartRecruiters);
   - **Workday** career sites of large companies (NVIDIA, Salesforce, Adobe, Walmart, Intel, Novartis…), searched for Hyderabad and remote India;
   - **LinkedIn public job search** (logged out, no account): 4 searches per run, rotated, pausing 2 h if rate-limited;
   - every 3 hours: Himalayas (remote, India) and the Hacker News "Who is hiring" thread.
2. **Checks** each job against Surya's rules: Hyderabad or remote open to India; React / Node / TypeScript / Python stack;
   minimum experience asked **3 years or less**; no Lead/Staff/Principal/Manager titles, and "Senior" only when the JD asks ≤3 years;
   pay, when shown, **above ₹12 LPA**; no region-locked "remote", gigs or data-labelling work.
3. **Stores** them in SQLite with a short application note; the page shows the last 7 days.
4. **Alerts on Telegram** once per new job (title, company, location, years asked, pay, source, applicants, link).
   The first run after deploy only records existing jobs, so it never floods the chat.

## Telegram setup

1. In Telegram open **@BotFather** → `/newbot` → copy the token. Open your new bot and press **Start**.
2. On the server: `sudo nano /etc/power-engine.env` and add `TELEGRAM_BOT_TOKEN=...`
3. `cd /opt/power-engine && export $(sudo grep ^TELEGRAM_ /etc/power-engine.env | xargs) && venv/bin/python -m app.main --telegram-chat-id`
   → add `TELEGRAM_CHAT_ID=...` to the same file. (The file is root-only, mode 600, so read it with `sudo`.)
4. `sudo systemctl restart power-engine`, then in the same way: `export $(sudo grep ^TELEGRAM_ /etc/power-engine.env | xargs) && venv/bin/python -m app.main --telegram-test`.

## Checking that it works

```bash
python engine/daily_jobs.py --selfcheck && python -m app.main --selfcheck   # rules and parsers
ENGINE_NO_SCHEDULER=1 python test_offline.py                              # full run with fake sources
python engine/daily_jobs.py --probe                                       # live: which sources answer from this machine
```

## Stack

Python 3.10+, FastAPI, SQLite, vanilla JS. The collector uses only the standard library (`urllib`, `concurrent.futures`), so it runs anywhere. Deployed on an Ubuntu VM behind nginx with HTTPS and basic auth.

## Run locally

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
ENGINE_NO_SCHEDULER=1 .venv/bin/uvicorn app.main:app --port 8010
python engine/daily_jobs.py --selfcheck
python -m app.main --selfcheck
```

Set `ENGINE_SIGNATURE` to add a contact line under each note. Personal details live in the server's environment file, never in this repo.

## Layout

```
engine/job_scanner.py   company job-board readers + title/location filters
engine/daily_jobs.py    all sources + checks -> one list of verified jobs; --probe
engine/linkedin.py      LinkedIn public search (no login)
engine/workday.py       Workday career sites
test_offline.py         end-to-end test with fake sources
app/main.py             FastAPI: storage, 30-min schedule, Telegram alerts, refresh, notes
app/static/index.html   the page
deploy/deploy.sh        server setup: venv, systemd, nginx, certbot
```
