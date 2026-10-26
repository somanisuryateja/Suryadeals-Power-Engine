# Suryadeals Power Engine

A daily job feed that finds fresh software roles open to Hyderabad or remote India and checks each one at its source before listing it.

Most job boards show reposts, region-locked "remote" roles and listings that were filled weeks ago. This engine reads company job boards directly and only keeps jobs that pass every check.

## What it does

Every day at 8:00 AM IST, and on demand from the page (at most once per 15 minutes):

1. **Collects** from about 105 company job boards through their public APIs (Greenhouse, Lever, Ashby, SmartRecruiters), the Himalayas remote-jobs search filtered to India, and the current Hacker News "Who is hiring" thread.
2. **Checks** each job:
   - Posting date comes from the company's own applicant tracking system, never from an aggregator.
   - Location must be Hyderabad, or remote and open to India / APAC / worldwide.
   - The description is scanned for hidden region locks ("EU/LATAM only", "US work authorization").
   - Title filters drop Senior/Staff/Lead, interns, and stacks outside React / Node / TypeScript / Python.
   - Experience asked is parsed from the description; anything over 4 years is dropped.
   - Shown pay is converted to lakh INR; anything under ₹10 LPA is dropped and ₹25 LPA+ is flagged.
   - Gigs, outsourcing shops and data-labelling work are dropped.
3. **Stores** the jobs in SQLite with a short application note drafted for each one.
4. **Serves** one page where each job can be opened, its note copied, and marked applied or skipped. Old jobs are pruned after 21 days unless marked.

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
engine/daily_jobs.py    all sources + checks -> one list of verified jobs
app/main.py             FastAPI: storage, daily schedule, refresh, notes
app/static/index.html   the page
deploy/deploy.sh        server setup: venv, systemd, nginx, certbot
```
