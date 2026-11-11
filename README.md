# pop-health-jobs

A job-hunting system for a population-health graduate (BS, Lehigh) covering two job clusters, each
one a **50 minute drive**: **York** (pinned at Country Club of York) and **West Chester** (pinned at
Marshallton, PA). Anything further than 50 minutes from either pin never reaches the board.

Two parts that work together:

| | What it is | Where it runs |
|---|---|---|
| **[watcher/](watcher/)** | Scrapes 12 employer job boards daily, filters to roles she can actually apply for, alerts on Telegram | Raspberry Pi (`vez`) via Hermes cron |
| **[web/](web/)** | Dashboard showing every match, with one-click Apply, Applied / Not-for-me tracking, and an optional note on each dismissal | Vercel + Postgres |

**Live:** https://pop-health-jobs.vercel.app

---

## How the pieces fit

```
vez (Pi)                              Vercel                       Postgres (Neon)
──────                                ──────                       ───────────────
07:45  deploy-job-watch.sh ── git pull ──▶ install + selftest
08:00  watcher/job_watch.py        one run, every employer
  ├─ Camofox browser ──▶ 12 job boards
  ├─ relevance filter (regex, deterministic)
  ├─ route card per match ──▶ openrouteservice (geocode, snap, drive time)
  ├─ Telegram alert ──────────────▶ phone
  ├─ POST /api/ingest ─────────────▶ /api/ingest ──upsert──▶ jobs + job_maps
  │    (jobs, scanner hash, maps)             ▲               (status: new/
  │                                           │                applied/ignored)
  │                               /api/jobs   │
  └── GET /api/suppressed ────────────────────┘
       (skips jobs already applied to)
```

Each card also carries a **route card** — the drive from the nearer of the two cluster pins
(Country Club of York, or Marshallton for West Chester), drawn over OpenStreetMap tiles with the
time and distance and labelled with the cluster name rather than the pin. The Pi resolves the route
once per city and stores the encoded polyline (~2 KB) in Postgres, so the dashboard makes no
routing requests: Leaflet draws the line in the browser from data already in the page, and only for
cards that are on screen. Stored minutes are corrected by 0.82, because the OSM-based router runs
~20 % above Google Maps on these routes (measurements in [watcher/README.md](watcher/README.md)).

**No LLM runs in the pipeline.** Hermes cron is only a scheduler: both jobs use `--no-agent`,
so the script's stdout *is* the Telegram message and empty stdout sends nothing. Relevance is a
fixed set of regexes in `job_watch.py` (`POSITIVE` / `NEGATIVE` / `URL_REJECT` / `DETAIL_REJECT`
plus the location rules), covered by `--selftest`. AI is used when *authoring* those rules — e.g.
reading the "not applicable" reasons off the dashboard and tightening a filter — never at runtime.
That is what keeps a run free, reproducible and immune to invented postings.

---

## One trigger

Scanning is a single fixed run: `8e7a83cfdc37` at **08:00 daily**, which scrapes every employer in
the `SOURCES` table and nothing else. There is no second schedule, no interval knob and no on/off
switch, because re-scanning the same boards more often only costs page loads.

**The Pi does not look for new employers.** An earlier version did — an agent proposing employers
and probing their boards — and it was removed because it muddied the app's purpose and put an
unverified guess in front of the one thing that matters: the employer list. Adding an employer is a
deliberate edit to `SOURCES` in `job_watch.py`, because a new board needs a parser for its
applicant tracking system, and a parser has to be written and tested against the real site.

---

## Keeping the Pi in sync

The repo is the source of truth; the Pi holds a checkout and a deployed copy.

| Piece | Path |
|---|---|
| Git checkout on the Pi | `~/.hermes/pop-health-jobs` |
| Deployed script (what cron runs) | `~/.hermes/scripts/job_watch.py` |
| Cron shim (stable, never edited) | `~/.hermes/scripts/deploy-job-watch.sh` |
| Deploy logic (lives in the repo) | [`deploy/deploy-job-watch.sh`](deploy/deploy-job-watch.sh) |

A second Hermes cron job (`dee89f6f4230`, 07:45) runs the shim, which pulls `main`, self-tests
the candidate, and installs it atomically — keeping five rollbacks and hashing the installed file
to prove it matches what was tested. It is silent when there is nothing to do and only speaks on
an install or a failure. So `git push` is the whole deploy.

Drift is visible two ways: the dashboard header shows `scanner <hash>` (the hash of the file that
last fed it, sent in the ingest payload), and [`deploy/status-job-watch.sh`](deploy/status-job-watch.sh)
prints a PASS/FAIL table for installed-vs-`origin/main`, selftest, cron presence, state freshness,
board reachability and the scanner hash the board reports.

---

## Layout

```
watcher/
  job_watch.py          the whole scraper/filter notifier — single file, stdlib only
  README.md             how to install it and the filter rules

deploy/
  deploy-job-watch.sh   pull → selftest → install atomically → verify hash
  cron-shim.sh          installed as ~/.hermes/scripts/deploy-job-watch.sh
  status-job-watch.sh   PASS/FAIL health check for the whole Pi side

web/
  app/                  Next.js app router
    page.tsx            server component: loads jobs from Postgres
    JobBoard.tsx        client component (Material UI): search, filters, status buttons, archive
    employerColor.ts    per-employer chip colour (stable hue per source)
    RouteMap.tsx        Leaflet map of the drive (lazy: only cards on screen)
    polyline.ts         decodes the stored route polyline into lat/lng pairs
    theme.ts            MUI theme (Roboto, flat buttons)
    Providers.tsx       theme + CssBaseline wrapper
    api/ingest/         POST snapshot from the Pi (token auth, idempotent upsert)
    api/jobs/           GET active + archive
    api/jobs/[id]/      PATCH status, DELETE an addressed not-applicable row
    api/suppressed/     GET jobs already applied to (consumed by the Pi)
  lib/db.ts             Postgres pool, schema bootstrap, stable job ids
  README.md             app-level setup and endpoint reference

docs/
  job-alerts-setup.md            watcher runbook: sources, filters, failure modes, commands
  population-health-job-search.md  full role/track/pay research
  live-openings.md               hand-verified posting list from the initial sweep
  *-tracker.csv                  spreadsheets for tracking applications
```

---

## Environment

### Vercel (`web/`)

| Variable | Source | Notes |
|---|---|---|
| `POSTGRES_URL` | Neon integration | Auto-injected when the Postgres store is attached |
| `INGEST_TOKEN` | You generate it | Shared secret; must match the Pi's `JOB_BOARD_TOKEN` |

### Pi (`~/.hermes/.env`)

```
JOB_BOARD_URL=https://pop-health-jobs.vercel.app
JOB_BOARD_TOKEN=<same value as INGEST_TOKEN>
ORS_KEY=<openrouteservice key>      # geocoding, snapping and the drive time
```

The board credentials are optional — without them the watcher still scrapes and alerts, it just
doesn't publish to the dashboard. The map key is optional too: without it jobs sync normally, they
just have no route card. One key covers everything (free tier: 2,000 directions + 1,000 geocodes a
day; our usage is a few per week, and only for *new* postings).

---

## Local development

```bash
cd web
npm install
cp .env.example .env.local     # fill in POSTGRES_URL and INGEST_TOKEN
npm run dev                    # http://localhost:3000
```

Then in another shell, push a fake snapshot:

```bash
curl -X POST http://localhost:3000/api/ingest \
  -H 'content-type: application/json' \
  -H "x-ingest-token: $INGEST_TOKEN" \
  -d '{"jobs":[{"employer":"Test","job_id":"1","title":"Health Coach","location":"Wilmington, DE","url":"https://example.com"}]}'
```

The schema creates itself on first request.

---

## Deploying

Vercel deploys `web/` automatically on every push to `main` (Root Directory is set to `web`).

To update the watcher on the Pi:

```bash
scp watcher/job_watch.py vincent@vez:~/.hermes/scripts/job_watch.py
ssh vincent@vez '~/.hermes/tools/python-3.14.7+20260901-linux-arm64/bin/python3 \
  ~/.hermes/scripts/job_watch.py --selftest'
```

Run `--selftest` after **any** change to the filter rules — it has 179 cases covering title
matching, geography, licensure screens and the parsers.

