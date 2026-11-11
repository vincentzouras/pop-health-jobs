# watcher

`job_watch.py` — one file, Python standard library only. Scrapes 12 employer job boards through
a local [Camofox](https://github.com/jo-inc/camofox-browser) browser, filters results down to roles
the graduate can actually apply for, pushes them to the dashboard, and sends a Telegram alert for
anything new.

## Why a browser

Four of the boards can't be scraped with plain HTTP:

- **Penn Medicine** sits behind Cloudflare (403 to `curl`)
- **ChristianaCare / Main Line / WellSpan** serve Next.js shells whose `/_next/data/*.json`
  routes 404 — only the rendered DOM has the jobs
- **Jefferson** and **Tower** render results client-side

Camofox (`127.0.0.1:9377`) handles all of them. It's already running as `camofox.service` for
other Hermes work.

## Deploy on the Pi

Never `scp` by hand — that is how the Pi and the repo drift. The Pi keeps a checkout of this
repo and installs from it:

```bash
ssh vincent@vez '~/.hermes/scripts/deploy-job-watch.sh'   # pull → selftest → install
```

The 07:45 cron (`dee89f6f4230`) runs that for you before the 08:00 scan. It is silent when there
is nothing to do, installs atomically only if `--selftest` passes, keeps five rollback copies, and
re-hashes the installed file against what it tested. Check everything with:

```bash
ssh vincent@vez '~/.hermes/bin/status-job-watch.sh'
```

Register the two jobs (already done):

```bash
cd ~/.hermes/hermes-agent
./hermes cron create "45 7 * * *" \
  --name "Job board: deploy watcher from git" \
  --script deploy-job-watch.sh --no-agent --deliver telegram
./hermes cron create "0 8 * * *" \
  --name "Job alerts: population health (PA/DE)" \
  --script job_watch.py --no-agent --deliver telegram
```

`--no-agent` means the script's stdout *is* the message and empty stdout sends nothing. No LLM in
the alert path: no per-run cost, no chance of an invented posting. Filtering is a fixed set of
regexes, and `--selftest` is what protects them.

## Commands

```bash
PY=~/.hermes/tools/python-3.14.7+20260901-linux-arm64/bin/python3

$PY job_watch.py             # normal run — silent unless something is new
$PY job_watch.py --dump      # show relevant / new / rejected
$PY job_watch.py --dump --dump-all
$PY job_watch.py --selftest  # 168 filter + parser + boundary tests; run after ANY rule change
$PY job_watch.py --baseline  # mark everything currently live as already-known
$PY job_watch.py --backfill-posted   # read posted dates off detail pages for jobs lacking one
$PY job_watch.py --backfill-location # replace a source's guessed city with the real one
$PY job_watch.py --backfill-maps     # resolve route cards for postings that lack one
$PY job_watch.py --check-origin      # re-geocode the pinned start point and report drift
$PY job_watch.py --check-maps        # prove ORS_KEY end to end (geocode, snap, route)
```

Scanning is **one fixed daily trigger** and nothing else. The employer list is `SOURCES` in
`job_watch.py`; adding an employer is a code change, because each board needs a parser for its
applicant tracking system. There is no discovery mode, no probe, and no on/off switch — an earlier
version auto-hunted for employers and it was removed as out of scope.

## Geography: two clusters, 50 minutes each

The rule is distance, not a city list:

| Cluster | Pin | Label on the card |
|---|---|---|
| York | Country Club of York, 39.9276, −76.7452 | "from York" |
| West Chester | Marshallton, West Bradford, PA, 39.9487, −75.6972 | "from West Chester" |

A posting is on the board only if it is **within `CLUSTER_MAX_MINUTES` (50) of one of those two
pins**, measured as the calibrated drive time from the *nearer* pin. The card shows the cluster
name, never the pin. Nothing outside that boundary reaches the board, the alert or the archive.

Both pins are hard-coded rather than geocoded per run, so drive times stay comparable;
`--check-origin` re-geocodes each address and fails if a pin has drifted more than 500 m.
`Marshallton, PA` on its own geocodes to Schuylkill County, so the West Bradford village is pinned
explicitly.

Between them: West Chester and Downingtown are ~9 min from the West Chester pin, Wilmington ~36,
Lancaster 43 from York, and Philadelphia ~60 from either — which is why Philadelphia postings,
present in the sources, never appear.

## Location: a default is a guess, the posting wins

Seven sources publish no city on their result cards, so they carry a `default_location`
(WellSpan → "York, PA", Tower → "Southeastern PA", PHMC → "Philadelphia, PA", Main Line, and the
three county GovernmentJobs boards). Those defaults are only stand-ins. WellSpan's board spans
York, Gettysburg, Ephrata, Lancaster and Lebanon — and **Chambersburg**, which is Franklin
County, ~50 min past York. A "Social Service Coordinator" at Chambersburg Hospital was being
shown as York, PA.

So for any match whose city came from a default, the detail page is read and `classify()` is run
again on the real city. The verified city is stored in `state.locations` and applied before
`classify()` on every later run, and the check runs *before* the dashboard push, so a wrong city
never reaches the board. Once the real city is known, `resolve_route()` decides whether it is
within 50 minutes; if it is not, the posting is dropped and recorded as `location_rejected`.

`--backfill-location` re-reads the real city for postings recorded before this existed and lists
anything that turns out to be outside the boundary.

A derived city is judged differently from a card's city: `classify(..., derived=True)` accepts any
PA/DE town, because the curated city list can never cover every township a health system posts in
(Chester County posts in Westtown, York County in Dillsburg). Distance decides afterwards, so the
footprint question is answered by the route rather than by a list.

## Route cards (map + drive time)

Every match gets the drive from **whichever cluster pin is nearer**, and the card says "from York"
or "from West Chester". The route is resolved **once per city** (not per posting), at ingest, and
the encoded polyline (~2 KB) is stored by the dashboard — viewing the board makes no routing call
at all, and the map is drawn in the browser over OpenStreetMap tiles.

One free key, in `~/.hermes/.env` and never in the browser:

| Var | Service | Free tier | Used for |
|---|---|---|---|
| `ORS_KEY` | openrouteservice | small daily quota (we spent a day's worth during development) | city → coordinates, snap to road, drive time/distance, route geometry |

Because the cache is per city, a normal run costs nothing: only a *new* town needs a route. The
whole board re-resolving is ~30 calls. Without the key the feature is absent: jobs still sync and
show, they just have no map.

- Geocodes are cached in `state.geo`, resolved routes in `state.maps_done`, so nothing is done twice.
- `normalize_location()` tidies what the boards publish (`"West Chester, PA, United States"`,
  `"Wilmington, Delaware"`, `"York, PA 17403"`) into `"City, ST"`; `geocodable()` then rejects
  regions (`"Southeastern PA"`), which are source defaults rather than places.
- `_pick_point()` only adopts a geocoder hit whose **state and name both match** the posting:
  Pelias will offer "Northeast Ithaca, NY" for "Northeast Philadelphia, PA", and asking for a
  locality-only layer (a shortcut tried for the Wilmington river problem) forced a match 50 miles
  from York. No convincing hit means no card, rather than a confidently wrong one.
- `_route_too_short()` is a second, independent invariant: a road distance below the straight line
  between the same two points cannot be real. It catches scrambled coordinates, not a wrong-but-
  nearby geocode — that is `_pick_point`'s job.
- `snap_point()` puts the geocoded city on the nearest road first: openrouteservice geocodes
  Wilmington, DE into the Delaware River and then refuses to route to it.
- `_ors_json()` paces calls to 40/minute (the free-tier limit) and backs off on 429/403, because a
  backfill fires two calls per posting.
- `--backfill-maps` resolves for the whole board from state (no re-scrape needed).
- `--check-maps` proves the key in one shot: geocode the origin, geocode a corridor city, snap it,
  route it and report the time, distance and geometry size. Run it right after adding the key.
- `--check-origin` re-geocodes the address and fails if the pinned point has drifted >500 m.
- A failure is left unsettled so the next run retries it; only an auth/quota error counts towards
  the three-strikes stop, so one odd address cannot end a backfill early.
- Times are free-flow, and the router's own minutes are multiplied by `DRIVE_CALIBRATION` (0.82)
  before they are stored. Measured, not guessed: openrouteservice and OSRM agree within 5 % on the
  same corridors and both sit ~20 % above Google Maps, because they price a route from OSM speed
  limits with a hair cut plus a fixed penalty at every turn and signal (ORS gives US-30 41 mph
  where the limit is 55), while Google's "typical" figure is measured from real traffic. The raw
  router value is kept in `state.maps_done[...]["raw"]` and printed by `--check-maps`.

Attribution is shown in the map corner: "© OpenStreetMap contributors · route by
openrouteservice".

## What counts as a match

Four gates, in order:

1. **Title matches one of 59 target-title patterns** — care coordinator, patient navigator, community
   health worker, health educator, outreach coordinator, quality improvement, referral/intake
   coordinator, practice manager, case manager, plus the adjacent doors into the same employers
   (patient services rep, medical staff coordinator, unit coordinator/secretary, health information,
   disease intervention, emergency preparedness, process improvement).
2. **Title isn't excluded** — 112 rejection patterns: clinical and administrative terms (RN, LPN,
   therapist, tech, coder, director, intern, and the CNA-equivalent titles: nursing assistant,
   patient care assistant), seniority words (lead, master, senior, sr., principal) that are not
   first-job titles, plus social-program terms (treatment court, life skills, re-entry, housing,
   shelter, justice) that were flooding in from PHMC.
3. **URL doesn't encode a license the title hides** — `rn-care-coordinator-per-diem` was slipping
   through as "Care Coordinator, Per Diem".
4. **Within 50 minutes of a cluster pin** — the run routes the posting's city from the nearer of
   the two pins and drops it if that drive is longer. Unknown or regional locations are dropped,
   never guessed: if it cannot be routed, it cannot be shown to be in range.

For *new* matches only, it then opens the posting and rejects on hard requirements: "must have RN
license", "master's required", "LCSW", "position filled". This exists because Melmark's $89,440
"Health Care Coordinator" looked perfect and required a PA RN.

## Posted dates

Two passes, because sources differ:

1. **Results page** — most cards carry nothing, but where a date is present (`Posted 3 days
   ago`, `11/02/2026`, `Nov 2, 2026`) it is normalised to `YYYY-MM-DD`.
2. **Detail page** — Workday ("posted on Posted 2 Days Ago"), UltiPro ("Posting Details Posted:
   October 14, 2026") and NEOGOV ("Opening Date 08/17/2026") only show it inside the posting.

Only 15 of the 55 live postings have a date, because Penn Medicine, Tower Health, ChristianaCare,
WellSpan, AmeriHealth and Main Line Health do not publish one anywhere on the page. Those send
`null` and the board shows "found <when the Pi first saw it>" instead of inventing a date.
`--backfill-posted` re-walks known postings that still have no date, one page load each, and can
be re-run after adding a source.

## Failure behaviour

| Failure | Behaviour |
|---|---|
| Camofox wedged / down | Self-heals — `/start` → `/health` → `systemctl restart camofox` |
| Tab reaped mid-run (404) | Self-heals — fresh tab, retry |
| A page returns an empty shell (WellSpan throttles) | Self-heals — backs off, retries twice |
| A source's markup changes | Alerts — `returned 0 postings (usually ~N)` |
| A source partially breaks | Alerts — `< 40% of peak` |
| Every source unreachable | Alerts — `⚠️ Job watch FAILED`, exit 1 |
| Nothing new | Silent — 0 bytes |

Per-source counts are persisted in `source_health` (`last` / `peak` / `zero_streak`), so a broken
parser is loud instead of looking like "no new jobs".

## Adding a source

Append to `SOURCES` in `job_watch.py`:

```python
{
    "name": "Employer Name",
    "base": "https://careers.example.org",
    "parse": parse_example,          # (href, card_text, base) -> (job_id, title, location, url)
    "default_location": "...",       # only if the cards never show a city
    "urls": ["https://careers.example.org/jobs?q=coordinator"],
},
```

Then `--baseline` so existing postings don't all alert at once, and add a parser case to
`selftest()`.

Sources that were investigated and rejected (with reasons) are listed in
[../docs/job-alerts-setup.md](../docs/job-alerts-setup.md).
