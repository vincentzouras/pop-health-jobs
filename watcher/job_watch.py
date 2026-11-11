#!/usr/bin/env python3
"""
Hermes job watch for population-health roles (BS Population Health, CNA + scribe).
Corridors: (A) Downingtown / West Chester / Wilmington, (B) York / Lancaster / Harrisburg.

Runs via Camofox (local anti-detect browser on :9377) because Penn Medicine and
several other ATS fronts are Cloudflare-protected.

Modes
  (default)      scan every source once, print alert text for NEW + relevant jobs, else nothing
  --baseline     record everything currently live, alert nothing
  --selftest     run the relevance filter against a labelled corpus, print pass/fail
  --backfill-posted  read posted dates off detail pages for known jobs that lack one
  --dump         print every posting that passed/didn't pass the filter (debug)
  --resimulate N drop N random seen keys so they re-alert (validation only)

The employer list is the SOURCES table below. Adding an employer is a code change, on purpose:
each one needs a parser for its applicant tracking system, and a parser is not something to
generate at 3am.

Exit codes: 0 ok, 1 total source failure (so Hermes surfaces it), 0 otherwise.
"""
from __future__ import annotations

import json
import hashlib
import math
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

CAMO = "http://127.0.0.1:9377"
USER = "jobwatch"
HOME = Path.home()
STATE_DIR = HOME / ".hermes" / "job-watches"
STATE_FILE = STATE_DIR / "job-alerts-state.json"
LOG_FILE = STATE_DIR / "job-alerts-last-run.json"
STATE_DIR.mkdir(parents=True, exist_ok=True)

# --------------------------------------------------------------------------
# Web dashboard sync (Vercel). Both are optional; if unset the watcher still
# works exactly as before.
# --------------------------------------------------------------------------
def _load_env_file(path: Path) -> None:
    """Hermes cron does not guarantee the service EnvironmentFile reaches scripts."""
    if not path.exists():
        return
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip("'\"")
            if k and k not in os.environ:
                os.environ[k] = v
    except Exception:
        pass


_load_env_file(HOME / ".hermes" / ".env")

WEB_URL = os.environ.get("JOB_BOARD_URL", "https://pop-health-jobs.vercel.app").rstrip("/")
WEB_TOKEN = os.environ.get("JOB_BOARD_TOKEN", "")

# --------------------------------------------------------------------------
# Two job clusters, each a ~50 minute drive. The map for a posting starts from whichever of
# these two points is nearer, and the card shows the cluster name ("York" / "West Chester")
# rather than the pin: a golf club address is not how she thinks about York, and Marshallton
# is simply the point that stands in for the West Chester cluster.
#
# Both are pinned rather than geocoded on every run so drive times stay comparable;
# `--check-origin` re-geocodes each address to catch a stale pin.
# --------------------------------------------------------------------------
ORIGINS = {
    "york": {
        "label": "York",
        "address": "1400 Country Club Rd, York, PA 17403",
        "lat": 39.9276,
        "lon": -76.7452,
    },
    "west_chester": {
        "label": "West Chester",
        # "Marshallton, PA" alone geocodes to Schuylkill County, so pin the West Bradford one.
        "address": "Marshallton, West Bradford, PA",
        "lat": 39.9487,
        "lon": -75.6972,
    },
}

# The whole point of the board: nothing further than this from one of the two clusters.
# Applies to the calibrated minutes, i.e. the number shown on the card.
CLUSTER_MAX_MINUTES = 50
ORS_KEY = os.environ.get("ORS_KEY", "") or os.environ.get("OPENROUTESERVICE_KEY", "")

# Locations like "Southeastern PA" are source defaults, not real places.
_GEOCODABLE = re.compile(
    r",\s*(?:PA|NJ|DE|MD|NY|WV|VA)(?:\s+\d{5}(?:-\d{4})?)?\s*$", re.I)
_REGION_WORD = re.compile(
    r"\b(?:Southeastern|Southwestern|Northeastern|Northwestern|Central|Statewide|"
    r"Various|Multiple|Remote|Hybrid)\b", re.I)


def normalize_location(loc) -> str:
    """Tidy the shapes the sources use, so one test can tell a city from a region.

    Boards publish the same place as "West Chester, PA", "West Chester, PA, United States"
    and "Wilmington, Delaware"; all three have to reach the geocoder as "City, ST".
    """
    s = (loc or "").strip()
    s = re.sub(r",?\s*(?:United States|USA|US)\s*$", "", s, flags=re.I)
    parts = s.rsplit(",", 1)
    if len(parts) == 2:
        abbr = _STATE_ABBR.get(parts[1].strip().lower())
        if abbr:
            s = "%s, %s" % (parts[0].strip(), abbr)
    return s.strip()


def geocodable(loc: str) -> bool:
    """A city we can route to: state-qualified, and not a region standing in for one."""
    return bool(_GEOCODABLE.search(loc)) and not _REGION_WORD.search(loc)

# Set by the provider helpers so --check-maps can say why a step failed, not just that it did.
MAP_ERROR = {"geocode": "", "route": ""}


def has_map_keys() -> bool:
    return bool(ORS_KEY)


def _http_json(method: str, url: str, body=None, headers=None, timeout: int = 30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def fetch_suppressed() -> set:
    """Jobs already marked applied / not-applicable on the dashboard.

    This is a safety net, not the primary dedupe: `seen` already stops re-alerting.
    It matters when the baseline is reset or a new source is added, so jobs she has
    already handled do not come back as 'new'.
    """
    if not WEB_URL:
        return set()
    try:
        data = _http_json("GET", WEB_URL + "/api/suppressed", timeout=20)
        return {"%s|%s" % (r.get("employer", ""), r.get("job_key", "")) for r in data.get("suppressed", [])}
    except Exception:
        return set()


def scanner_version() -> str:
    """Hash of the running file, so the board can prove which build fed it."""
    try:
        return hashlib.md5(Path(__file__).read_bytes()).hexdigest()[:12]
    except Exception:
        return "unknown"


def push_snapshot(relevant_jobs: dict, run_at: str, full: bool = True) -> str:
    if not WEB_URL or not WEB_TOKEN:
        return "skipped (JOB_BOARD_URL / JOB_BOARD_TOKEN not set)"
    payload = {
        "run_at": run_at,
        "scanner": scanner_version(),
        # "This is the whole board": anything the dashboard still lists as new but that is
        # absent here has fallen outside the two clusters and gets retired rather than left
        # sitting there forever.
        "full": bool(full),
        "jobs": [
            {
                "employer": j["employer"],
                "job_id": str(j["id"]),
                "title": j["title"],
                "location": j.get("location") or None,
                "url": j.get("url") or None,
                "posted": j.get("posted") or None,
            }
            for j in relevant_jobs.values()
        ],
    }
    if not payload["jobs"] and not full:
        return "skipped (nothing relevant this run)"
    try:
        res = _http_json("POST", WEB_URL + "/api/ingest", payload,
                         {"x-ingest-token": WEB_TOKEN}, timeout=45)
        return "ok (%s new, %s updated, %s retired)" % (
            res.get("inserted"), res.get("updated"), res.get("retired"))
    except Exception as e:
        return "FAILED: %s" % str(e)[:120]

# --------------------------------------------------------------------------
# Geography: only the two corridors the user asked for.
# A location is accepted if it contains one of these tokens.
# --------------------------------------------------------------------------
ALLOWED_CITIES = [
    # Corridor A - Chester County / Philly metro
    "west chester", "exton", "malvern", "downingtown", "coatesville", "kennett",
    "west grove", "avondale", "oxford", "parkesburg", "gap", "chadds ford",
    "paoli", "berwyn", "devon", "wayne", "radnor", "phoenixville", "pottstown",
    "bala cynwyd", "wynnewood", "bryn mawr", "media", "chester", "glen mills",
    # Delaware County boroughs - a county job can sit anywhere in the county, and all of
    # these are within ~25 min of West Chester / Media.
    "drexel hill", "upper darby", "havertown", "haverford", "lansdowne", "yeadon",
    "darby", "collingdale", "sharon hill", "clifton heights", "folcroft", "ridley park",
    "prospect park", "brookhaven", "aston", "boothwyn", "holmes", "broomall",
    "springfield", "swarthmore", "villanova",
    "norristown", "king of prussia", "newtown square", "plymouth meeting",
    "blue bell", "lansdale", "horsham", "willow grove", "abington", "jenkintown",
    "philadelphia", "collegeville", "west point",
    # Delaware - New Castle County only. Kent/Sussex (Dover, Smyrna, Milford) sit another
    # 60-90 min past Wilmington, so they are rejected by FAR_DE below.
    "wilmington", "newark", "new castle", "middletown", "bear", "glasgow",
    "dover",  # Dover, PA (York County, in corridor); Dover, DE is caught by FAR_DE
    # Corridor B - York / Lancaster / Harrisburg
    "york", "hanover", "spring grove", "gettysburg", "lancaster", "ephrata",
    "lititz", "mount joy", "elizabethtown", "new holland", "quarryville",
    "columbia", "hershey", "harrisburg", "camp hill", "mechanicsburg",
    "carlisle", "lebanon", "wyomissing", "reading",
    # synthetic defaults used by sources whose listing cards omit the city
    "southeastern pa",
]

# Hard rejects on location: any state outside PA/DE, plus far-flung PA metros.
# Matching is token-based (see _city_in_corridor) so "District Of Columbia" no longer
# matches "Columbia, PA" and "Dorchester" no longer matches "Chester, PA".
BAD_LOCATION = re.compile(
    r"\b(?:"
    r"new jersey|new york|new hampshire|new mexico|north carolina|north dakota|"
    r"south carolina|south dakota|rhode island|west virginia|"
    r"district of columbia|washington,?\s*d\.?c\.?|"
    r"maryland|virginia|ohio|connecticut|massachusetts|georgia|florida|texas|"
    r"illinois|michigan|indiana|missouri|tennessee|kentucky|wisconsin|minnesota|"
    r"oregon|california|arizona|colorado|nevada|utah|alabama|mississippi|arkansas|"
    r"louisiana|oklahoma|kansas|nebraska|iowa|idaho|montana|wyoming|alaska|hawaii|"
    r"vermont|maine"
    r")\b|"
    r"\b(?:NJ|NY|MD|VA|WV|OH|DC|CT|MA|NH|NC|SC|GA|FL|TX|IL|MI|IN|MO|TN|KY|WI|MN|"
    r"OR|CA|AZ|CO|NV|UT|AL|MS|AR|LA|OK|KS|NE|IA|ID|MT|WY|AK|HI|VT|ME)\.?(?![a-z])|"
    r"pittsburgh|erie|scranton|wilkes|altoona|johnstown|state college|"
    r"stroudsburg|hazleton|williamsport|bethlehem|allentown|easton|"
    # Franklin County: WellSpan's Chambersburg / Waynesboro hospitals are ~50 min past York.
    r"chambersburg|waynesboro|shippensburg|mercersburg|greencastle",
    re.I,
)

_CITY_RE = re.compile(
    r"(?<![a-z])(?:" + "|".join(re.escape(c) for c in sorted(ALLOWED_CITIES, key=len, reverse=True)) + r")(?![a-z])",
    re.I,
)

# Kent/Sussex County DE is past Wilmington, not on the way to it. Keyed off the state so
# "Dover, PA" (York County) keeps working - the city name alone can't tell them apart.
FAR_DE = re.compile(r"\b(?:dover|smyrna|milford)\b[^;|]*?(?:\bDE\b|\bdelaware\b)", re.I)


def _city_in_corridor(loc: str) -> bool:
    return bool(_CITY_RE.search(loc))

# --------------------------------------------------------------------------
# Title relevance. Both lists are case-insensitive regex.
# --------------------------------------------------------------------------
POSITIVE = re.compile(
    r"population health|care coordinat|care navigator|patient navigator|"
    r"community health|community educator|community outreach|community resource|"
    r"health educator|health coach|health promotion|health navigator|"
    r"care management|care manager|patient coordinator|patient services|"
    r"patient access|patient liaison|patient experience|patient advocate|"
    r"program coordinator|program liaison|program specialist|"
    r"quality coordinator|quality improvement|quality specialist|"
    r"compliance coordinator|compliance specialist|"
    r"credentialing|medical staff|payer enrollment|referral coordinator|intake coordinator|"
    r"practice manager|practice coordinator|practice supervisor|"
    r"social services?|case manager|case management|outreach coordinator|"
    r"wellness coordinator|benefits coordinator|eligibility specialist|"
    # Adjacent doors into the same health systems: front-of-house patient access,
    # hospital unit/clerical coordination, public-health program roles.
    r"front desk|receptionist|unit coordinator|unit secretary|unit support coordinator|"
    r"clinical support assistant|clinical services coordinator|"
    r"health information|medical records|provider services|"
    r"prevention coordinator|prevention specialist|disease intervention|"
    r"emergency preparedness|process improvement|improvement advisor",
    re.I,
)

NEGATIVE = re.compile(
    r"\brn\b|registered nurse|\bnurse\b|\blpn\b|\blvn\b|crna|\bcrnp\b|nurse practitioner|"
    r"licensed professional|licensed social worker|"
    r"physician|\bpa-c\b|physician assistant|therapist|physical therap|"
    r"occupational therap|speech|technologist|technician|\btech\b|pharmac|"
    r"radiolog|sonograph|cytotech|histolog|perfusion|surgical|\bma\b|"
    r"medical assistant|coder|\bcoding\b|\bcdi\b|dietitian|nutritionist|"
    r"social worker|lcsw|lmsw|psycholog|counselor|clinician|behavior analyst|"
    r"bcba|direct support|caregiver|home health aide|\bcna\b|resident assistant|"
    r"nursing assistant|nurse assistant|patient care assistant|patient care asst|"
    r"patient care tech|patient sitter|patient observer|"
    r"paramedic|\bemt\b|phlebotom|sterile processing|dental|hygienist|"
    r"driver|\bcook\b|chef|housekeep|custodial|security officer|facilities|"
    r"engineer|electrician|plumber|hvac|accountant|attorney|paralegal|"
    r"marketing|fundrais|director|vice president|\bvp\b|\bchief\b|president|"
    r"intern\b|internship|student|fellowship|resident\b|volunteer|"
    r"transcription|scheduler\b|"
    # Seniority verbs: these are the roles she is not hired into straight out of a BS.
    r"\blead\b|\bmaster\b|\bsenior\b|\bsr\.?(?![a-z])|\bprincipal\b|"
    r"treatment court|drug court|life skills|benefits advocate|"
    r"re-?entry|shelter|housing|homeless|justice|probation|parole|"
    r"bips\b|safety & responsibility|residential|group home|"
    r"behavior consultant|bc-aba|registered behavior",
    re.I,
)

# Requisition URLs often encode the license the title hides ("rn-care-coordinator-per-diem").
URL_REJECT = re.compile(
    r"(?:^|[/\-_])(?:rn|lpn|lvn|crna|crnp|np|pa-c|md|do)(?:[/\-_]|$)"
    r"|registered-nurse|nurse-practitioner|physician-assistant",
    re.I,
)


def classify(title: str, location: str = "", url: str = "", derived: bool = False) -> tuple[bool, str]:
    """Return (is_relevant, reason). Deterministic - this is the whole *role* filter.

    Geography is deliberately not decided here: distance is the rule (see `in_range`), and it
    needs a route, so the run applies it separately. This only answers "is this the kind of job
    she wants" and "can we trust the location enough to route it".
    """
    t = (title or "").strip()
    if len(t) < 4:
        return False, "empty title"
    if NEGATIVE.search(t):
        return False, "excluded title term"
    if url and URL_REJECT.search(url):
        return False, "licensed role (url)"
    if not POSITIVE.search(t):
        return False, "not a target role"
    loc = (location or "").strip()
    # Normalise trailing country noise ("Philadelphia, PA, United States") before checks.
    loc = re.sub(r",?\s*United States\s*$", "", loc, flags=re.I).strip(" ,")
    if not loc:
        # Without a place it cannot be routed, so it cannot be shown to be in range.
        return False, "unknown location"
    if not geocodable(normalize_location(loc)):
        # A regional stand-in ("Southeastern PA") from a result card is only a placeholder: the
        # detail screen replaces it with the real city before distance is judged. Once it *has*
        # come from the posting and is still a region, there is nothing to route to.
        if derived:
            return False, "location is a region, not a city"
        return True, "ok (city from detail page)"
    return True, "ok"


# --------------------------------------------------------------------------
# Camofox plumbing
# --------------------------------------------------------------------------
def camo(method: str, path: str, body=None, timeout: int = 120):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(CAMO + path, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", "replace")
    return json.loads(raw)


def camo_healthy() -> bool:
    try:
        with urllib.request.urlopen(CAMO + "/health", timeout=15) as r:
            return bool(json.loads(r.read().decode()).get("ok"))
    except Exception:
        return False


def revive_browser(tries: int = 2) -> bool:
    """Recover from browser_rss_pressure / wedged browser.

    Camofox kills the page when RSS crosses BROWSER_RSS_RESTART_THRESHOLD_MB and then
    reports ok=false with reason=browser_rss_pressure until something relaunches it.
    Without this, the daily job would silently produce zero results forever.
    """
    for i in range(tries):
        try:
            camo("POST", "/start", {}, timeout=120)
        except Exception:
            pass
        for _ in range(15):
            time.sleep(2)
            if camo_healthy():
                return True
        # last resort: bounce the user service
        try:
            subprocess.run(["systemctl", "--user", "restart", "camofox.service"],
                           capture_output=True, timeout=60)
        except Exception:
            pass
        time.sleep(5)
    return camo_healthy()


# ATS cards advertise the posting date in wildly different shapes. Grab the raw hint in
# the page and normalise it in Python, which knows the run date.
DATE_JS = r"""
  const dateHint = (s) => {
    const m = s.match(/(?:posted|posting date|date posted|open date|start date)\s*[:\-]?\s*(today|yesterday|\d+\+?\s*(?:hour|day|week|month)s?\s+ago|[A-Z][a-z]{2,8}\.?\s+\d{1,2},?\s+\d{4}|\d{1,2}\/\d{1,2}\/\d{2,4}|\d{4}-\d{2}-\d{2})/i)
      || s.match(/\b(\d{4}-\d{2}-\d{2})\b/)
      || s.match(/\b(\d{1,2}\/\d{1,2}\/\d{4})\b/);
    return m ? m[1] : '';
  };
"""

EXTRACT_JS = r"""
(() => {""" + DATE_JS + r"""
  const out = [], seen = new Set();
  for (const a of document.querySelectorAll('a[href]')) {
    const href = a.getAttribute('href') || '';
    if (!/\/posting\/\w|jobId=\d|jk=[a-z0-9]{8,}|\/job\/\d+|jobs\/\d+|OpportunityDetail|\/careers\/[^/]+\/jobs\//i.test(href)) continue;
    if (/privacy|terms|login|accessibility|linkedin|instagram|facebook|twitter|youtube|job-cart|saved-jobs/i.test(href)) continue;
    const own = (a.innerText || '').replace(/\s+/g, ' ').trim();
    const card = a.closest('li,article,tr,[class*="card"],[class*="job"],[class*="result"],[class*="posting"]');
    let text = own;
    if (card) {
      const ct = (card.innerText || '').replace(/\s+/g, ' ').trim();
      // Only trust the card when it is not a giant wrapper that swallowed the page nav.
      if (ct.length <= Math.max(300, own.length * 5)) text = ct;
    }
    // Some ATSs (AmeriHealth) put only the title in the anchor and the city in a sibling
    // panel. Climb looking for a "Location X" marker; never accept an ancestor without one.
    if (!/Location\s+[A-Z]/.test(text)) {
      let n = card || a;
      for (let i = 0; i < 6 && n && n.parentElement; i++) {
        n = n.parentElement;
        const t = (n.innerText || '').replace(/\s+/g, ' ').trim();
        if (t.length > 1500) break;
        if (/Location\s+[A-Z]/.test(t)) { text = t.slice(0, 400); break; }
      }
    }
    if (!text) text = own;
    if (text.length < 4) continue;
    const key = href + '|' + text.slice(0, 30);
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({ h: href, t: text.slice(0, 260), d: dateHint(text) });
    if (out.length >= 60) break;
  }
  return out;
})()
"""

BODY_JS = r"""
(() => {
  const main = document.querySelector('main') || document.body;
  const body = (main.innerText || '').replace(/\s+/g, ' ').slice(0, 6000);
  // Posted dates sit below the fold on some ATSs (NEOGOV's "Opening Date"), so keep a
  // wider copy purely for date extraction. The 6k `body` stays the input to the
  // hard-requirement screen so extra chrome cannot trigger a false rejection.
  const full = (document.body.innerText || '').replace(/\s+/g, ' ').slice(0, 20000);
  return { body: body, full: full };
})()
"""

# AmeriHealth (Phenom) wraps each result in a container whose text holds the title,
# the city and the requisition id; the anchor alone carries only the title.
AH_EXTRACT_JS = r"""(() => {""" + DATE_JS + r"""
  const out = [], seen = new Set();
  for (const a of document.querySelectorAll('a[href*="/us/en/job/"]')) {
    const h = a.getAttribute('href') || '';
    if (!/\/us\/en\/job\/\d+\//.test(h)) continue;
    const row = a.closest('[data-ph-at-id], li, article, tr, div[class*="job"], div[class*="card"]');
    let t = '';
    if (row) t = (row.innerText || '').replace(/\s+/g, ' ').trim();
    if (!/Location\s+[A-Z]/.test(t)) {
      let n = a.parentElement;
      for (let i = 0; i < 6 && n; i++) {
        const x = (n.innerText || '').replace(/\s+/g, ' ').trim();
        if (/Location\s+[A-Z]/.test(x) && x.length < 500) { t = x; break; }
        n = n.parentElement;
      }
    }
    if (!t) t = (a.innerText || '').replace(/\s+/g, ' ').trim();
    if (t.length < 4) continue;
    const k = h.slice(-24) + t.slice(0, 20);
    if (seen.has(k)) continue;
    seen.add(k);
    out.push({ h: h, t: t.slice(0, 300), d: dateHint(t) });
    if (out.length >= 40) break;
  }
  return out;
})()
"""

# PHMC runs on UKG (UltiPro), which renders every posting as a web component: the title is a
# <ukg-link>, not an <a>, so an anchor scrape saw a full board as zero postings. Query the cards
# by their data-automation hooks instead - that also hands us the city, which the anchors never
# carried (the source used to guess "Philadelphia, PA" for all of them).
PHMC_EXTRACT_JS = r"""
(() => {
  const clean = (el) => el ? (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim() : '';
  const out = [];
  for (const card of document.querySelectorAll('[data-automation="opportunity"]')) {
    const a = card.querySelector('[data-automation="job-title"]');
    const href = a ? (a.getAttribute('href') || '') : '';
    if (!href) continue;
    // The address element also holds a facility name ("Corp", "PHMC Care Clinic"), so read the
    // one field that is only "City, ST ZIP, USA" rather than its combined text.
    const addr = card.querySelector('[data-automation="physical-location"]');
    const city = addr ? addr.querySelector('[data-automation="city-state-zip-country-label"]') : null;
    out.push({
      h: href,
      t: (clean(a) + ' ||| ' + clean(city || addr)).slice(0, 300),
      d: clean(card.querySelector('[data-automation="opportunity-posted-date"]'))
    });
    if (out.length >= 60) break;
  }
  return out;
})()
"""

# Hard requirements that mean she cannot apply, no matter how good the title looks.
# Checked against the full job description of NEW matches only (cheap: a handful per week).
DETAIL_REJECT = re.compile(
    r"must (?:have|possess|hold)[^.]{0,40}(?:registered nurse|RN license|nursing license|"
    r"active RN|LPN|licensed practical nurse|BSN|CRNA|LCSW|LMSW|PharmD|CPA)\b"
    r"|current (?:and active )?(?:US |Pennsylvania |PA |Delaware |DE )?(?:RN|registered nurse|"
    r"LPN|licensed practical nurse)[^.]{0,30}(?:license|licensure)"
    r"|RN licensure (?:is )?required"
    r"|require[sd]? (?:a )?(?:current )?(?:active )?(?:RN|registered nurse|nursing) licen[cs]e"
    r"|(?:master'?s|MPH|MSW|MHA) degree (?:is )?required"
    r"|board[- ]certified (?:behavior analyst|registered nurse)"
    r"|licensed clinical social worker"
    r"|no longer available|position has been filled|this job has expired",
    re.I,
)


def detail_reject(text: str) -> tuple[bool, str]:
    """Return (should_reject, matched_phrase). Pure function, unit-testable."""
    if not text:
        return False, ""
    m = DETAIL_REJECT.search(text)
    return (True, m.group(0)[:60]) if m else (False, "")


def fetch_jobs(url: str, wait: float = 5.0, extract_js: str = None):
    last = None
    for attempt in range(3):
        try:
            return _fetch_jobs(url, wait, extract_js)
        except Exception as e:
            last = e
            msg = str(e)
            # 503/502 = browser pressure or cold start; 404 on /evaluate = the tab was
            # reaped underneath us. Both are transient and worth one fresh tab.
            if "503" in msg or "502" in msg:
                if not revive_browser():
                    time.sleep(3)
            elif "404" in msg or "410" in msg:
                time.sleep(1.5)
            else:
                raise
    raise last


def _fetch_jobs(url: str, wait: float = 5.0, extract_js: str = None):
    tab = camo("POST", "/tabs", {"userId": USER, "sessionKey": "jobwatch", "url": url})
    tid = tab.get("tabId") or tab.get("id") or (tab.get("tab") or {}).get("tabId")
    if not tid:
        raise RuntimeError("no tabId: %s" % str(tab)[:120])
    try:
        time.sleep(wait)
        ev = camo("POST", "/tabs/%s/evaluate" % tid, {"userId": USER, "expression": extract_js or EXTRACT_JS})
        return ev.get("result") or []
    finally:
        try:
            camo("DELETE", "/tabs/%s?userId=%s" % (tid, USER), timeout=20)
        except Exception:
            pass


def fetch_detail(url: str, wait: float = 4.0):
    """Return (main_text_6k, full_text_20k) for a posting. Empty strings on failure."""
    tab = camo("POST", "/tabs", {"userId": USER, "sessionKey": "jobwatch", "url": url})
    tid = tab.get("tabId") or tab.get("id") or (tab.get("tab") or {}).get("tabId")
    if not tid:
        return "", ""
    try:
        time.sleep(wait)
        ev = camo("POST", "/tabs/%s/evaluate" % tid, {"userId": USER, "expression": BODY_JS})
        res = ev.get("result") or {}
        if isinstance(res, str):
            return res, res
        return (res.get("body") or ""), (res.get("full") or res.get("body") or "")
    finally:
        try:
            camo("DELETE", "/tabs/%s?userId=%s" % (tid, USER), timeout=20)
        except Exception:
            pass


def posted_for(url: str, wait: float = 4.0):
    """(body, posted_date) for a posting — one page load, used by the detail screen."""
    body, full = fetch_detail(url, wait)
    return body, (posted_from_body(full) or posted_from_body(body))


# --------------------------------------------------------------------------
# Source adapters: turn raw anchors into (id, title, location, url)
# --------------------------------------------------------------------------
def parse_christianacare(href, text, base):
    m = re.search(r"/posting/(JR\d+)", href)
    if not m:
        return None
    jid = m.group(1)
    title = re.split(r"\bREQ ID\b", text)[0].strip()
    loc = ""
    ml = re.search(r"LOCATION\s+(.+?)\s+CATEGORY", text)
    if ml:
        loc = ml.group(1).strip()
    return jid, title, loc, base + href if href.startswith("/") else href


PENN_CATEGORIES = (
    "Clerical/Administrative|Customer Service/Patient Access|Allied Health|Nursing|"
    "Professional|Technical|Research|Finance|Leadership|Clinical Support|Administrative|"
    "Physician Assistant|Other|Information Services|Facilities|Human Resources|Social Work"
)


PENN_CATEGORY_WORDS = {
    "other", "clerical/administrative", "clinical", "support", "administrative",
    "customer", "service/patient", "access", "allied", "health", "nursing",
    "professional", "technical", "research", "finance", "leadership",
    "physician", "assistant", "social", "work", "information", "services",
    "facilities", "human", "resources", "new",
}


def _clean_penn_loc(raw: str) -> str:
    """Strip category/label words that a greedy match pulls in front of the city."""
    head, _, tail = raw.partition(",")
    words = head.split()
    # A card with no recognisable category glues the title onto the city
    # ("Master Improvement Advisor Business Process Improvement Philadelphia"). Snap to the
    # longest trailing run that is a real corridor city so "West Chester" survives intact.
    for n in (3, 2, 1):
        if len(words) >= n and _CITY_RE.fullmatch(" ".join(words[-n:])):
            return (" ".join(words[-n:]) + "," + tail).strip()
    if len(words) > 3:
        words = words[-3:]
    while words and words[0].lower() in PENN_CATEGORY_WORDS:
        words.pop(0)
    return (" ".join(words) + "," + tail).strip() if words else ""


_PENN_US_ANCHOR = re.compile(r",\s*(?:PA|NJ|DE|NY|MD),\s*United States", re.I)


def _trim_penn_title(title: str) -> str:
    """Drop a "<Category> <City, ST, United States> <shift> | ..." tail glued onto a title.

    Anchored on the state suffix and walked back over a real corridor city, because a
    left-to-right match happily starts the "city" at the last word of the title.
    """
    title = (title or "").strip(" |-—")
    m = _PENN_US_ANCHOR.search(title)
    if not m:
        return title
    words = title[: m.start()].split()
    for n in (3, 2, 1):
        if len(words) >= n and _CITY_RE.fullmatch(" ".join(words[-n:])):
            return " ".join(words[:-n]).strip(" |-—")
    return " ".join(words).strip(" |-—")


def parse_penn(href, text, base):
    m = re.search(r"/jobs/(\d+)", href)
    if not m:
        return None
    jid = m.group(1)
    body = re.sub(r"^\d{6}\s*", "", (text or "")).strip()

    # Some Penn searches render as a TABLE, so every anchor's text is the entire table
    # ("Showing 1-13 of 13 results Job ID Job Title ...") rather than one card.
    # In that case fall back to the URL slug for the title and locate the row for the
    # location.
    if "Showing" in body and "Job ID" in body:
        slug = href.rstrip("/").rsplit("/", 1)[-1]
        slug = re.sub(r"^\d+-", "", slug).replace("slash", "/").replace("-", " ")
        title = slug.title()
        loc = ""
        # rows look like: "<id> <Title> [NEW] <Category> <City, ST, United States> <WorkType>"
        for row in re.split(r"(?=\b\d{6}\s)", body):
            probe = re.sub(r"[^a-z0-9]", "", row.lower())
            if re.sub(r"[^a-z0-9]", "", slug.lower())[:22] in probe:
                mloc = re.search(r"([A-Z][A-Za-z.'-]*(?:[ -][A-Z][A-Za-z.'-]*)*,\s*(?:PA|NJ|DE|NY|MD),\s*United States)", row)
                if mloc:
                    loc = _clean_penn_loc(mloc.group(1))
                break
        return jid, title.strip(), loc, href

    # Cards read: "<title> <Category> <City, ST, United States> <shift>"
    parts = re.split(r"\b(?:%s)\b" % PENN_CATEGORIES, body, maxsplit=1)
    title = _trim_penn_title(parts[0].strip(" |-—"))
    rest = parts[1] if len(parts) > 1 else body
    loc = ""
    found = re.findall(r"([A-Z][A-Za-z.'-]*(?:[ -][A-Z][A-Za-z.'-]*)*,\s*(?:PA|NJ|DE|NY|MD),\s*United States)", rest)
    if found:
        loc = _clean_penn_loc(found[0])
    if not title:
        slug = href.rsplit("/", 1)[-1]
        title = re.sub(r"^\d+-", "", slug).replace("-", " ").replace("slash", "/").title()
    return jid, _trim_penn_title(title), loc, href


def parse_mainline(href, text, base):
    m = re.search(r"/posting/[a-z0-9-]+/([A-Za-z0-9]+)/?", href)
    if not m:
        return None
    jid = m.group(1)
    title = re.sub(r"VIEW POSITION", "", text, flags=re.I).strip()
    # Cards prefix the job category in caps, e.g. "CLINICAL SUPPORT Patient Outreach
    # Coordinator - Primary Care" or "& SUPPORT SERVICE Care Coordination...".
    title = re.sub(r"^(?:[A-Z][A-Z&/\-]+(?:\s+[A-Z][A-Z&/\-]+)*)\s+", "", title).strip()
    title = re.sub(r"^(?:NURSING|ALLIED HEALTH|PHYSICIANS & ADVANCED PRACTICE PROVIDERS)\s*", "", title, flags=re.I)
    title = re.sub(r"\s+", " ", title).strip(" |-—")
    loc = ""
    ml = re.search(r"([A-Z][A-Za-z .'-]+,\s*PA)", text)
    if ml:
        loc = ml.group(1)
    return jid, title, loc, base + href if href.startswith("/") else href


def parse_wellspan(href, text, base):
    m = re.search(r"jobId=(\d+)", href)
    if not m:
        return None
    jid = m.group(1)
    title = text.strip()
    loc = ""
    ml = re.search(r"([A-Z][A-Za-z .'-]+,\s*PA)", text)
    if ml:
        loc = ml.group(1)
    return jid, title, loc, "https://www.joinwellspan.org" + href if href.startswith("/") else href


def parse_govjobs(href, text, base):
    m = re.search(r"/jobs/(\d+)", href)
    if not m:
        return None
    return m.group(1), text.strip(), "", "https://www.governmentjobs.com" + href if href.startswith("/") else href


def parse_jefferson(href, text, base):
    # Workday path encodes both: /job/<Location-Slug>/<Title-Slug>_REQ-12345
    m = re.search(r"/job/([^/]+)/([^/?]+)", href)
    if not m:
        return None
    loc_slug, title_slug = m.group(1), m.group(2)
    title = re.sub(r"_REQ[-_]?\d+.*$", "", title_slug)
    title = title.replace("XMLNAME-2B-", "+").replace("-", " ").strip()
    title = re.sub(r"\s+", " ", title)
    # "Camden-County-NJ" -> "Camden County, NJ"
    parts = loc_slug.split("-")
    loc = ""
    if len(parts) >= 2 and len(parts[-1]) == 2:
        loc = "%s, %s" % (" ".join(parts[:-1]), parts[-1].upper())
    else:
        loc = " ".join(parts)
    jid = re.search(r"REQ[-_]?(\d+)", title_slug)
    return (jid.group(1) if jid else title_slug[:40]), title, loc, \
           ("https://jeffersonhealth.wd5.myworkdayjobs.com" + href if href.startswith("/") else href)


def parse_amerihealth(href, text, base):
    m = re.search(r"/us/en/job/(\d+)/", href)
    if not m:
        return None
    jid = m.group(1)
    body = re.sub(r"\s+", " ", text).strip()
    title = re.split(r"\bLocation\b", body)[0].strip(" |-—")
    title = re.sub(r"^Job\b", "", title).strip()
    loc = ""
    ml = re.search(r"Location\s+(.+?)\s+(?:Category|Job Id)\b", body)
    if ml:
        loc = re.sub(r",?\s*\d{5}$", "", ml.group(1).strip()).strip(" ,")
    return jid, title, loc, href


def parse_phmc(href, text, base):
    m = re.search(r"opportunityId=([0-9a-f-]{8,})", href)
    if not m:
        return None
    jid = m.group(1)[:12]
    # cards read "<title> ||| <city>, <ST> <zip>, USA" (see PHMC_EXTRACT_JS)
    title_part, _, city = text.partition(" ||| ")
    title = re.sub(r"\s+(?:Today|Yesterday|[A-Z][a-z]{2}\s+\d{1,2},\s+\d{4})\s*$", "", title_part.strip()).strip()
    link = "https://recruiting.ultipro.com" + href if href.startswith("/") else href
    return jid, title, city.strip(), link


def parse_tower(href, text, base):
    m = re.search(r"/jobs/(\d+)\?", href)
    if not m:
        return None
    if "icims.com" in href and "/login" in href:
        return None
    jid = m.group(1)
    title = text.strip()
    # Cards read "<Title> Req ID: 65533 Location <Facility> <City>, Pennsylvania"
    title = re.split(r"\bReq ID\b|\bLocation\b", title)[0].strip(" |-—,")
    title = re.sub(r"^(?:star\s+)?(?:Featured Job|New)\s+", "", title, flags=re.I).strip()
    if not title or title.lower() in ("apply now", "view job"):
        return None
    link = base + href if href.startswith("/") else href
    return jid, title, "", link


# BambooHR boards are a plain server-rendered list: <a href="/careers/123">Title</a>, with the
# city in a sibling column rather than inside the link, so climb a few levels for it.
BAMBOO_JS = r"""
(() => {
  const out = [], seen = new Set();
  const CITY = /([A-Z][A-Za-z.'\-]+(?: [A-Z][A-Za-z.'\-]+)*),\s*([A-Z]{2})\b/;
  for (const a of document.querySelectorAll('a[href*="/careers/"]')) {
    const h = a.getAttribute('href') || '';
    if (!/\/careers\/\d+/.test(h)) continue;
    const base = h.split('?')[0];
    if (seen.has(base)) continue;
    seen.add(base);
    const title = (a.innerText || '').replace(/\s+/g, ' ').trim();
    let city = '';
    let n = a;
    for (let i = 0; i < 5 && n && !city; i++) {
      n = n.parentElement;
      const t = n ? (n.innerText || '').replace(/\s+/g, ' ').trim() : '';
      const m = CITY.exec(t);
      if (m && t.length < 300) city = m[1] + ', ' + m[2];
    }
    out.push({ h: base, t: city ? (title + ' ||| ' + city) : title, d: '' });
    if (out.length >= 60) break;
  }
  return out;
})()
"""


def parse_bamboohr(href, text, base):
    m = re.search(r"/careers/(\d+)", href)
    if not m:
        return None
    jid = m.group(1)
    raw = (text or "").strip()
    title_part, _, city_hint = raw.partition(" ||| ")
    title = re.sub(r"\s+", " ", title_part).strip(" |-—,\n")
    loc = city_hint.strip()
    if not loc:
        # Older cards put the city in the title text: "<title> - <city>, <ST>".
        tail = re.search(r",\s*([A-Z]{2})\s*$", raw)
        if tail:
            head = raw[:tail.start()]
            city = re.split(r"\s[-\u2013|]\s|,|\n", head)[-1].strip()
            if city:
                loc = "%s, %s" % (city, tail.group(1))
                title = re.sub(r"\s+", " ", head[:len(head) - len(city)]).strip(" |-—,\n")
    link = base + href if href.startswith("/") else href
    return jid, title, loc, link


def parse_upmc(href, text, base):
    m = re.search(r"/job/(\d+)/([^/]+)?", href)
    if not m:
        return None
    jid = m.group(1)
    title = re.sub(r"^NEW\s*", "", text).strip()
    # UPMC puts the city in the slug: .../patient-service-coordinator-camp-hill-pa/
    loc = ""
    slug = (m.group(2) or "").strip("/")
    tokens = [t for t in slug.split("-") if t]
    if len(tokens) >= 2 and tokens[-1] in ("pa", "ny", "md", "nj", "wv", "oh", "de"):
        state = tokens[-1].upper()
        for n in (1, 2, 3):
            cand = " ".join(tokens[-1 - n:-1])
            if any(c in cand for c in ALLOWED_CITIES):
                loc = "%s, %s" % (cand.title(), state)
                break
        else:
            loc = "%s, %s" % (" ".join(tokens[-3:-1]).title(), state)
    # UPMC posts across PA/NY/MD; only keep roles whose city we could place in-corridor
    if not loc or not any(c in loc.lower() for c in ALLOWED_CITIES):
        return None
    return jid, title, loc, "https://careers.upmc.com" + href if href.startswith("/") else href


SOURCES = [
    {
        "name": "ChristianaCare",
        "base": "https://careers.christianacare.org",
        "parse": parse_christianacare,
        "urls": [
            "https://careers.christianacare.org/job-search/?keyword=community%20health",
            "https://careers.christianacare.org/job-search/?keyword=care%20coordinator",
            "https://careers.christianacare.org/job-search/?keyword=health%20coach",
            "https://careers.christianacare.org/job-search/?keyword=outreach",
        ],
    },
    {
        "name": "Penn Medicine",
        "base": "https://careers.pennmedicine.org",
        "parse": parse_penn,
        # Cloudflare-fronted and slow under load; 5s intermittently yields an empty shell.
        "wait": 10.0,
        "urls": [
            "https://careers.pennmedicine.org/search/jobs?q=population+health",
            "https://careers.pennmedicine.org/search/jobs?q=care+coordinator",
            "https://careers.pennmedicine.org/search/jobs?q=community+health+worker",
            "https://careers.pennmedicine.org/search/jobs?q=health+educator",
            "https://careers.pennmedicine.org/search/jobs?q=patient+navigator",
        ],
    },
    {
        "name": "Main Line Health",
        "base": "https://careers.mainlinehealth.org",
        "parse": parse_mainline,
        # Cards show no city; Main Line is entirely in Delaware/Montgomery Counties.
        "default_location": "Suburban Philadelphia, PA",
        "urls": [
            "https://careers.mainlinehealth.org/job-search/?keyword=care+coordinator",
            "https://careers.mainlinehealth.org/job-search/?keyword=community+health",
            "https://careers.mainlinehealth.org/job-search/?keyword=outreach",
        ],
    },
    {
        "name": "WellSpan (York)",
        "base": "https://www.joinwellspan.org",
        "parse": parse_wellspan,
        "default_location": "York, PA",
        # Their ?keyword= is ignored and the board returns the full list alphabetically,
        # so page through the whole thing and let the filters decide.
        "paginate": {"pattern": "https://www.joinwellspan.org/Search?keyword=care+coordinator&page={page}",
                     "start": 1, "max": 16, "gap": 1.5, "retries": 2},
        "wait": 8.0,
        "urls": [],
    },
    {
        "name": "Chester County",
        "base": "https://www.governmentjobs.com/careers/chesco",
        "parse": parse_govjobs,
        "default_location": "West Chester, PA",
        # NEOGOV ignores ?keyword= (it bounces straight back to the bare board), so the only
        # way past the first 10 of ~78 postings is `?page=N`. Sorted by posting date desc.
        # Recount with the board's "Showing 1-10 of N jobs" line if this starts truncating.
        "paginate": {"pattern": "https://www.governmentjobs.com/careers/chesco?page={page}",
                     "start": 1, "max": 8, "gap": 1.5, "retries": 1},
        "urls": [],
    },
    {
        "name": "Montgomery County",
        "base": "https://www.governmentjobs.com/careers/montcopa",
        "parse": parse_govjobs,
        "default_location": "Norristown, PA",
        "paginate": {"pattern": "https://www.governmentjobs.com/careers/montcopa?page={page}",
                     "start": 1, "max": 5, "gap": 1.5, "retries": 1},
        "urls": [],
    },
    {
        "name": "Delaware County",
        "base": "https://www.governmentjobs.com/careers/delcopa",
        "parse": parse_govjobs,
        "default_location": "Media, PA",
        "paginate": {"pattern": "https://www.governmentjobs.com/careers/delcopa?page={page}",
                     "start": 1, "max": 2, "gap": 1.5, "retries": 1},
        "urls": [],
    },
    # PA Civil Service is NOT monitored: governmentjobs.com/careers/pabureau ignores
    # ?keyword= and returns the same alphabetical first page (liquor enforcement, state
    # police cadet, PennDOT winter maintenance). It is a statewide board of thousands of
    # postings that cannot be filtered server-side, so it cost 2 page loads per run and
    # produced zero relevant results. PA Dept of Health roles still surface via the
    # saved Indeed searches.
    {
        "name": "Jefferson Health",
        "base": "https://jeffersonhealth.wd5.myworkdayjobs.com",
        "parse": parse_jefferson,
        # Workday encodes city+state in the URL path, so no default is needed.
        "wait": 14.0,
        "extract_js": r"""
(() => {""" + DATE_JS + r"""
  const out = [], seen = new Set();
  for (const a of document.querySelectorAll('a[href*="/job/"]')) {
    const h = a.getAttribute('href') || '';
    if (!/\/job\/[^/]+\/[^/?]+/.test(h)) continue;
    const base = h.split('?')[0];
    if (seen.has(base)) continue;
    seen.add(base);
    const row = a.closest('li,tr,[role="row"],[data-automation-id="jobItem"]');
    const d = row ? dateHint((row.innerText || '').replace(/\s+/g, ' ')) : '';
    out.push({ h: base, t: (a.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 150), d: d });
    if (out.length >= 40) break;
  }
  return out;
})()
""",
        "urls": [
            "https://jeffersonhealth.wd5.myworkdayjobs.com/ThomasJeffersonExternal?q=care%20coordinator",
            "https://jeffersonhealth.wd5.myworkdayjobs.com/ThomasJeffersonExternal?q=community%20health",
            "https://jeffersonhealth.wd5.myworkdayjobs.com/ThomasJeffersonExternal?q=patient%20navigator",
        ],
    },
    {
        "name": "AmeriHealth Caritas",
        "base": "https://careers.amerihealthcaritas.com",
        "parse": parse_amerihealth,
        "extract_js": AH_EXTRACT_JS,
        # Phenom renders the result list late; at 5s the cards are still empty.
        "wait": 11.0,
        # National payer: posts in DC/OH/SC/NH too, so NO default location. If we cannot
        # read the city off the card, the posting is dropped rather than guessed.
        "urls": [
            "https://careers.amerihealthcaritas.com/us/en/search-results?keywords=community%20health",
            "https://careers.amerihealthcaritas.com/us/en/search-results?keywords=care%20coordinator",
            "https://careers.amerihealthcaritas.com/us/en/search-results?keywords=care%20connector",
        ],
    },
    {
        "name": "PHMC (Philadelphia)",
        "base": "https://recruiting.ultipro.com",
        "parse": parse_phmc,
        "extract_js": PHMC_EXTRACT_JS,
        # UKG's board renders late; at 5s the cards are still empty.
        "wait": 11.0,
        # UltiPro cards now carry the city, so the old Philadelphia default would only be a
        # fallback for a card whose address failed to render.
        "default_location": "Philadelphia, PA",
        "urls": [
            "https://recruiting.ultipro.com/PUB1002/JobBoard/c8784846-358b-1bec-45e9-f994af5fccee?q=&o=postedDateDesc",
        ],
    },
    {
        "name": "Tower Health",
        "base": "https://www.towercareers.org",
        "parse": parse_tower,
        # Tower is a SE PA system (Reading, Pottstown, Phoenixville, Chestnut Hill...);
        # listing cards omit the city and only the detail page has it.
        "default_location": "Southeastern PA",
        "paginate": {"pattern": "https://www.towercareers.org/jobs?page={page}",
                     "start": 1, "max": 12, "gap": 1.5, "retries": 2},
        "urls": [],
    },
    # UPMC is intentionally NOT monitored: careers.upmc.com returns only navigation
    # for 10s, and when the results do render they span PA/NY/MD with the city buried
    # in the URL slug (and often only a county name, e.g. "-berks-county"). Keeping it
    # either injected out-of-region false positives or required opening every posting.
    # Use the saved Indeed search for UPMC instead.
    {
        "name": "ChesPenn Health Services",
        "base": "https://chespenn.bamboohr.com",
        "parse": parse_bamboohr,
        "extract_js": BAMBOO_JS,
        # BambooHR renders the list late and the cards carry no city at all, only the title;
        # every ChesPenn site is in Delaware County, so the default is theirs and the daily run
        # still reads the real city off each posting and corrects it.
        "wait": 10.0,
        "default_location": "Eddystone, PA",
        "urls": ["https://chespenn.bamboohr.com/careers"],
    },
]


# --------------------------------------------------------------------------
# Source health: a source that silently returns zero is worse than one that errors,
# because a broken parser looks exactly like "no new jobs".
# --------------------------------------------------------------------------
def check_source_health(found, errors, prev_health) -> tuple[list, dict]:
    counts = {}
    for job in found.values():
        counts[job["employer"]] = counts.get(job["employer"], 0) + 1
    for s in SOURCES:
        counts.setdefault(s["name"], 0)

    warnings, health = [], {}
    err_by_src = {}
    for e in errors:
        name = e.split(":", 1)[0]
        err_by_src[name] = err_by_src.get(name, 0) + 1

    for s in SOURCES:
        name = s["name"]
        n = counts.get(name, 0)
        prev = prev_health.get(name, {})
        # "typical" = the high-water mark we have seen; robust to a single bad day
        peak = max(prev.get("peak", 0), n)
        zero_streak = (prev.get("zero_streak", 0) + 1) if n == 0 else 0
        health[name] = {"last": n, "peak": peak, "zero_streak": zero_streak,
                        "errors": err_by_src.get(name, 0)}

        if err_by_src.get(name):
            warnings.append("%s: %d page load(s) failed" % (name, err_by_src[name]))
        if n == 0 and peak >= 5:
            warnings.append("%s: returned 0 postings (usually ~%d) — parser or site may have changed"
                            % (name, peak))
        elif peak >= 8 and n < peak * 0.4:
            warnings.append("%s: only %d postings (usually ~%d) — possible partial breakage"
                            % (name, n, peak))
    return warnings, health


# One scan at a time. A full scan takes ~13-15 minutes and a gate tick lands every 5, so without
# this the second tick would start a second scan (state still holds the previous run's last_run)
# and two browsers on a Pi would fight over memory. Released when the process exits.
RUN_LOCK = None


def acquire_run_lock(path=None) -> bool:
    """Take the scan lock, or report that another run owns it. Never raises.

    `path` exists so the selftest can prove the mutual exclusion without fighting a scan that is
    running at the time (the doctor runs the selftest on demand).
    """
    global RUN_LOCK
    try:
        import fcntl
    except ImportError:
        return True  # not POSIX: nothing to coordinate with, carry on
    if RUN_LOCK is not None:
        return False
    try:
        fh = open(path or (STATE_DIR / "job-watch.lock"), "a+")
    except Exception:
        return True  # unwritable state dir is a bigger problem than a double scan
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except Exception:
        fh.close()
        return False
    RUN_LOCK = fh
    return True


def release_run_lock() -> None:
    global RUN_LOCK
    if RUN_LOCK is not None:
        try:
            RUN_LOCK.close()
        except Exception:
            pass
        RUN_LOCK = None


def load_state():
    for path in (STATE_FILE, STATE_FILE.with_suffix(".bak")):
        if path.exists():
            try:
                return json.loads(path.read_text())
            except Exception:
                continue
    return {"seen": {}, "baseline_done": False, "runs": 0, "source_fail_streak": {}}


def save_state(st):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=1, sort_keys=True))
    if STATE_FILE.exists():
        try:
            STATE_FILE.with_suffix(".bak").write_bytes(STATE_FILE.read_bytes())
        except Exception:
            pass
    tmp.replace(STATE_FILE)


def _write_run_log(now, new_keys, errors, health_warnings, found=0, relevant=0) -> None:
    """Record every run, including silent ones.

    A log that only updates when something alerts cannot distinguish "nothing new today"
    from "the scheduled run never happened".
    """
    try:
        LOG_FILE.write_text(json.dumps({
            "at": now, "new": list(new_keys), "errors": errors,
            "health_warnings": health_warnings,
            "found": found, "relevant": relevant, "scanner": scanner_version(),
        }, indent=1))
    except Exception:
        pass


_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}

POSTED_BODY_RE = re.compile(
    r"(?:posting details\s*)?(?:posted on\s*)?(?:opening date|posted(?:\s+date)?|posting date|date posted|open date)\s*:?\s*"
    r"(today|yesterday|\d+\+?\s*(?:hour|day|week|month)s?\s+ago|"
    r"[A-Z][a-z]{2,8}\.?\s+\d{1,2},?\s+\d{4}|\d{1,2}/\d{1,2}/\d{2,4}|\d{4}-\d{2}-\d{2})",
    re.I,
)


def parse_posted(hint, today=None):
    """Normalise an ATS date hint ("Posted 3 days ago", "11/02/2026", ...) to YYYY-MM-DD.

    Returns None when the card carried no usable date, which is the honest answer for
    sources that simply do not publish one on the results page.
    """
    if not hint:
        return None
    today = today or datetime.now(timezone.utc).date()
    h = re.sub(r"\s+", " ", str(hint)).strip().lower()
    h = re.sub(r"^(?:posted|posting date|date posted|open date|start date)\s*[:\-]?\s*", "", h).strip()
    if not h:
        return None
    try:
        if h == "today":
            return today.isoformat()
        if h == "yesterday":
            return (today - timedelta(days=1)).isoformat()
        m = re.match(r"(\d+)\+?\s*(hour|day|week|month)s?\s+ago", h)
        if m:
            step = {"hour": 0, "day": 1, "week": 7, "month": 30}[m.group(2)]
            return (today - timedelta(days=step * int(m.group(1)))).isoformat()
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", h)
        if m:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date().isoformat()
        m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", h)
        if m:
            year = int(m.group(3))
            year += 2000 if year < 100 else 0
            return datetime(year, int(m.group(1)), int(m.group(2))).date().isoformat()
        m = re.match(r"([a-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})", h)
        if m:
            month = _MONTHS.get(m.group(1)[:3])
            if month:
                return datetime(int(m.group(3)), month, int(m.group(2))).date().isoformat()
    except Exception:
        return None
    return None


def posted_from_body(text: str, today=None):
    """Pull a posting date out of a job detail page.

    Only some ATSs publish one (Workday's "posted on Posted 2 Days Ago", UltiPro's
    "Posting Details Posted: October 14, 2026"). Penn, Tower and ChristianaCare do not,
    so a missing date is a normal outcome, not a parse failure.

    `today` is injectable so the selftest does not depend on the wall clock.
    """
    if not text:
        return None
    m = POSTED_BODY_RE.search(text)
    return parse_posted(m.group(1), today) if m else None


# "Chambersburg,PA" / "York, PA" / "Reading, Pennsylvania" — the comma may have no space.
_STATE_ABBR = {
    "pennsylvania": "PA", "new jersey": "NJ", "delaware": "DE", "maryland": "MD",
    "new york": "NY", "west virginia": "WV", "virginia": "VA",
}
# Used to confirm a geocoder hit is in the state we asked for.
_STATE_NAMES = {
    "PA": ("pa", "pennsylvania"), "NJ": ("nj", "new jersey"), "DE": ("de", "delaware"),
    "MD": ("md", "maryland"), "NY": ("ny", "new york"), "WV": ("wv", "west virginia"),
    "VA": ("va", "virginia"),
}
_LOC_IN_BODY = re.compile(
    r"([A-Z][A-Za-z.'\-]*(?:[ ][A-Z][A-Za-z.'\-]*){0,3}),\s*"
    r"(PA|NJ|DE|MD|NY|WV|VA|Pennsylvania|New Jersey|Delaware|Maryland|New York|"
    r"West Virginia|Virginia)\b"
)


# Pay/shift labels that sit in front of a city in some ATS templates
# ("Annually Location Drexel Hill, PA").
_CITY_LABELS = {
    "annually", "hourly", "salary", "location", "locations", "shift", "schedule",
    "department", "posted", "job", "type", "category", "status", "position",
}


def _trim_city(city: str) -> str:
    """Snap "Patient Access Representative Reading" down to "Reading"."""
    words = [w for w in city.split() if w.lower().strip(":,") not in _CITY_LABELS] or city.split()
    for n in (3, 2, 1):
        if len(words) >= n and _CITY_RE.fullmatch(" ".join(words[-n:])):
            return " ".join(words[-n:])
    return " ".join(words[-4:])


def location_from_body(text: str) -> str:
    """Read the posting's real city off a detail page.

    Multi-site systems publish a per-posting city but hide it from the results cards, so the
    source config carries a default (WellSpan -> "York, PA"). That default is a guess and
    WellSpan posts in Chambersburg, Gettysburg, Ephrata and Lebanon too, so the detail page
    wins. A street-numbered address is preferred over a bare mention of the corporate city.
    """
    if not text:
        return ""
    fallback = ""
    for m in _LOC_IN_BODY.finditer(text):
        city, state = m.group(1).strip(), m.group(2)
        state = _STATE_ABBR.get(state.lower(), state.upper())
        # Drop leading noise the greedy city group can swallow ("Hospital, York, PA").
        city = re.sub(r"^(?:Hospital|Center|Medical|Health|The)\s+", "", city).strip()
        city = _trim_city(city)
        if len(city) < 3:
            continue
        cand = "%s, %s" % (city, state)
        if not fallback:
            fallback = cand
        window = text[max(0, m.start() - 90):m.start()]
        if re.search(r"\b\d{1,6}\b", window):
            return cand
    return fallback


def collect(prev_health=None):
    prev_health = prev_health or {}
    found, errors = {}, []
    if not camo_healthy():
        revive_browser()
    for src in SOURCES:
        idle_pages = 0
        pag = src.get("paginate")
        urls = list(src["urls"])
        page = (pag or {}).get("start", 1)
        page_budget = (pag or {}).get("max", 20)
        pages_done = 0
        while True:
            if pag:
                if pages_done >= page_budget:
                    break
                url = pag["pattern"].format(page=page)
            else:
                if not urls:
                    break
                url = urls.pop(0)

            try:
                raw = fetch_jobs(url, wait=src.get("wait", 5.0), extract_js=src.get("extract_js"))
                if pag and not raw:
                    # WellSpan throttles when pages are fetched back-to-back and answers
                    # with an empty shell. Back off and retry before calling it a miss.
                    for attempt in range(pag.get("retries", 2)):
                        time.sleep(pag.get("gap", 2.5) * 2)
                        raw = fetch_jobs(url, wait=src.get("wait", 5.0) + 3,
                                         extract_js=src.get("extract_js"))
                        if raw:
                            break
            except Exception as e:
                errors.append("%s: %s" % (src["name"], str(e)[:80]))
                if pag:
                    page += 1
                    pages_done += 1
                continue

            new_here = 0
            for item in raw:
                parsed = src["parse"](item.get("h", ""), item.get("t", ""), src["base"])
                if not parsed:
                    continue
                jid, title, loc, link = parsed
                if not jid or not title:
                    continue
                guessed_loc = not loc
                if guessed_loc:
                    loc = src.get("default_location", "")
                key = "%s|%s" % (src["name"], jid)
                if key in found:
                    continue
                title = re.sub(r"\s+", " ", title)
                title = re.sub(r"\s+NEW$", "", title).strip(" |-—")
                found[key] = {
                    "employer": src["name"],
                    "id": jid,
                    "title": title[:120],
                    "location": re.sub(r"\s+", " ", loc)[:80],
                    "url": link,
                    "posted": parse_posted(item.get("d")),
                    # True when the city came from the source's default rather than the card.
                    "location_guessed": guessed_loc,
                }
                new_here += 1

            if pag:
                pages_done += 1
                if new_here == 0:
                    idle_pages += 1
                    if idle_pages >= 2:
                        break
                else:
                    idle_pages = 0
                page += 1
                time.sleep(pag.get("gap", 0.0))

        # A source that quietly returned far fewer postings than usual is probably a
        # partial load (one search page came back short). Retry that source once.
        if not pag:
            prev = prev_health.get(src["name"], {})
            peak = prev.get("peak", 0)
            got = sum(1 for j in found.values() if j["employer"] == src["name"])
            if peak >= 8 and 0 < got < peak * 0.6:
                for url in src["urls"]:
                    try:
                        raw = fetch_jobs(url, wait=src.get("wait", 5.0) + 3,
                                         extract_js=src.get("extract_js"))
                    except Exception:
                        continue
                    for item in raw:
                        parsed = src["parse"](item.get("h", ""), item.get("t", ""), src["base"])
                        if not parsed:
                            continue
                        jid, title, loc, link = parsed
                        if not jid or not title:
                            continue
                        key = "%s|%s" % (src["name"], jid)
                        if key in found:
                            continue
                        title = re.sub(r"\s+", " ", title).strip(" |-—")
                        found[key] = {
                            "employer": src["name"], "id": jid, "title": title[:120],
                            "location": re.sub(r"\s+", " ", loc or src.get("default_location", ""))[:80],
                            "url": link, "posted": parse_posted(item.get("d")),
                        }
    return found, errors


def _boundary_selftest() -> int:
    """Exercise the 50 minute rule with the network stubbed out, so it is the logic under test.

    Returns the number of failures. Runs both cluster pins, the boundary itself, and the case
    where the nearer pin is over the line but the other is not.
    """
    global geocode, snap_point, route_summary
    saved = (geocode, snap_point, route_summary)
    # The minutes are the real measured values (calibrated) for these pairs.
    places = {
        "Lancaster, PA": (40.0381, -76.3041),
        "West Chester, PA": (39.9595, -75.6061),
        "Philadelphia, PA": (39.9907, -75.1683),
        "Wilmington, DE": (39.7325, -75.5175),
    }
    drives = {
        ("york", 40.04, -76.30): 43, ("west_chester", 40.04, -76.30): 54,
        ("york", 39.96, -75.61): 90, ("west_chester", 39.96, -75.61): 9,
        ("york", 39.99, -75.17): 113, ("west_chester", 39.99, -75.17): 60,
        ("york", 39.73, -75.52): 100, ("west_chester", 39.73, -75.52): 36,
    }

    def fake_geocode(loc):
        return places.get(normalize_location(loc))

    def fake_route(cluster, lat, lon):
        mins = drives.get((cluster, round(lat, 2), round(lon, 2)))
        if not mins:
            return {"minutes": None, "raw_minutes": None, "miles": None, "geometry": ""}
        # Roads are never shorter than the straight line, and _route_too_short enforces it.
        straight = _straight_miles(ORIGINS[cluster]["lat"], ORIGINS[cluster]["lon"], lat, lon)
        return {"minutes": mins, "raw_minutes": mins * 1.22,
                "miles": round(straight * 1.2, 1), "geometry": "abc"}

    geocode = fake_geocode
    snap_point = lambda lat, lon: (lat, lon)
    route_summary = fake_route
    fails = 0
    try:
        cases = [
            ("Lancaster, PA", "york", 43, True),
            ("West Chester, PA", "west_chester", 9, True),
            ("Wilmington, DE", "west_chester", 36, True),
            # 60 min from the nearer pin and worse from the other: dropped either way.
            ("Philadelphia, PA", "west_chester", 60, False),
            ("Southeastern PA", None, None, False),
        ]
        for loc, want_cluster, want_min, want_range in cases:
            place = resolve_route(normalize_location(loc), {"geo": {}, "routes": {}})
            got = (place.get("cluster"), place.get("min"), in_range(place))
            ok = got == (want_cluster, want_min, want_range)
            if not ok:
                fails += 1
            print("%-6s %-20s -> cluster=%-12s %4s min  in range=%s"
                  % ("PASS" if ok else "FAIL", loc, got[0], got[1], got[2]))
        # The nearer pin can be over the line while the other is inside it.
        drives[("york", 39.96, -75.61)] = 58
        place = resolve_route("West Chester, PA", {"geo": {}, "routes": {}})
        ok = place.get("cluster") == "west_chester" and in_range(place)
        if not ok:
            fails += 1
        print("%-6s %-20s -> %s (%s min)" % ("PASS" if ok else "FAIL", "fallback pin",
                                             place.get("cluster"), place.get("min")))
        for name, ok in [("at the limit", in_range({"cluster": "york",
                                                    "min": CLUSTER_MAX_MINUTES}) is True),
                         ("one over", in_range({"cluster": "york",
                                                "min": CLUSTER_MAX_MINUTES + 1}) is False),
                         ("no cluster", in_range({"min": 10}) is False)]:
            if not ok:
                fails += 1
            print("%-6s %-20s -> %s" % ("PASS" if ok else "FAIL", name, ok))

        # A quota refusal must NOT be cached: the daily window rolls on its own, so the next
        # run has to retry rather than wait out the 24 hour error window. A genuine bad
        # address IS cached, or every run would pay for it again.
        def quota_route(cluster, lat, lon):
            MAP_ERROR["route"] = 'HTTP 403 { "error": "Quota exceeded" }'
            return {"minutes": None, "raw_minutes": None, "miles": None, "geometry": ""}

        route_summary = quota_route
        st_q = {"geo": {}, "routes": {}}
        rec = resolve_route("Lancaster, PA", st_q)
        ok = not st_q["routes"] and "Quota" in (rec.get("error") or "")
        if not ok:
            fails += 1
        print("%-6s %-20s -> cached=%s" % ("PASS" if ok else "FAIL", "quota not cached",
                                           bool(st_q["routes"])))

        def bad_route(cluster, lat, lon):
            MAP_ERROR["route"] = "HTTP 404 no routable point"
            return {"minutes": None, "raw_minutes": None, "miles": None, "geometry": ""}

        route_summary = bad_route
        st_b = {"geo": {}, "routes": {}}
        resolve_route("Lancaster, PA", st_b)
        ok = bool(st_b["routes"])
        if not ok:
            fails += 1
        print("%-6s %-20s -> cached=%s" % ("PASS" if ok else "FAIL", "bad address cached",
                                           bool(st_b["routes"])))

        # Moving a pin must invalidate everything measured against the old one.
        route_summary = fake_route
        st_p = {"geo": {}, "routes": {}}
        resolve_route("Lancaster, PA", st_p)
        rec = st_p["routes"].get("Lancaster, PA") or {}
        ok = rec.get("pins") == pin_stamp() and rec.get("cluster") == "york"
        if not ok:
            fails += 1
        print("%-6s %-20s -> %s" % ("PASS" if ok else "FAIL", "pin stamp stored", rec.get("pins")))
        rec["pins"] = "stale"
        again = resolve_route("Lancaster, PA", st_p)
        ok = again.get("pins") == pin_stamp() and again.get("cluster") == "york"
        if not ok:
            fails += 1
        print("%-6s %-20s -> re-resolved=%s" % ("PASS" if ok else "FAIL", "stale pin ignored",
                                                again.get("min")))

        # The dashboard gate: what decides whether the Pi scrapes at all.
        fresh = {"last_run": now_iso()}
        old = {"last_run": "2020-01-01T00:00:00Z"}
    finally:
        geocode, snap_point, route_summary = saved
    return fails

def selftest() -> int:
    corpus = [
        ("Care Coordinator", "Bala Cynwyd, PA", True),
        ("Community Health Worker - Women's Health", "Wilmington, Delaware", True),
        ("Health Coach", "Wilmington, Delaware", True),
        ("Patient Navigator", "Parkesburg, PA", True),
        ("Community Educator", "Wilmington, Delaware", True),
        ("Population Health Associate", "Bala Cynwyd, PA", True),
        ("Health Educator", "Lancaster, PA", True),
        ("Outreach Coordinator", "York, PA", True),
        ("Quality Improvement Coordinator", "West Chester, PA", True),
        ("Credentialing Specialist", "Philadelphia, PA", True),
        ("Patient Services Coordinator", "King of Prussia, PA", True),
        ("Practice Manager", "Exton, PA", True),
        # negatives
        ("Care Coordinator RN", "Paoli, PA", False),
        ("Registered Nurse - Case Management", "York, PA", False),
        ("Community Health Nurse", "Harrisburg, PA", False),
        ("Health Care Coordinator-Adult Community", "Berwyn, PA", False),  # RN-required in body; title filter can't see that
        ("Clinical Research Coordinator", "West Chester, PA", False),
        ("Research Technician II", "Philadelphia, PA", False),
        ("Medical Assistant", "Exton, PA", False),
        ("Pharmacy Technician", "Wilmington, Delaware", False),
        ("Physical Therapist", "West Chester, PA", False),
        ("Case Manager Nurse", "Ephrata, PA", False),
        ("Director of Population Health", "Philadelphia, PA", False),
        ("Population Health Intern", "Philadelphia, PA", False),
        ("Cook", "York, PA", False),
        # Seniority: not hired into these straight out of a BS
        ("Lead Case Manager", "Philadelphia, PA", False),
        ("Master Improvement Advisor", "Philadelphia, PA", False),
        ("Senior Care Coordinator", "West Chester, PA", False),
        ("Sr. Care Coordinator", "Exton, PA", False),
        ("Principal Program Coordinator", "Media, PA", False),
        # Regression: substring matching used to let these through
        ("Community Health Worker", "United States", False),
        ("Care Coordinator", "", False),
        # URL-based licensed-role detection (title hides it, slug does not)
        ("Care Coordinator, Per Diem", "Paoli, PA", False),
        # Positives that must survive the tighter matching
        ("Community Health Worker", "West Chester, PA", True),
        ("Care Coordinator", "Columbia, PA", True),
        ("Patient Navigator", "Chester, PA", True),
        ("Health Coach", "Bear, DE", True),
        ("Community Outreach Specialist (Full-Time)", "Philadelphia, PA", True),
        ("Referral Coordinator (Bilingual Spanish)", "Philadelphia, PA", True),
        ("Intake & Eligibility Specialist - ELRC 18", "Philadelphia, PA", True),
        # Adjacent health-system doors that a BS + CNA/scribe background clears
        ("Patient Services Representative - Cardiology", "Southeastern PA", True),
        ("Medical Staff Coordinator", "Southeastern PA", True),
        ("Health Information Management Services Coordinator", "Wilmington, Delaware", True),
        ("Disease Intervention Specialist - HIV/STI", "Media, PA", True),
        ("Unit Coordinator", "West Chester, PA", True),
        ("Care Manager II", "Dover, PA", True),
        ("Health Information Management Services Coordinator IV", "Wilmington, Delaware", True),
        ("Trauma Prevention Coordinator", "Northeast Philadelphia, PA", True),
        ("Emergency Preparedness Specialist", "Southeastern PA", True),
        ("Front Desk Coordinator - Primary Care", "West Chester, PA", True),
        # ...but direct-care titles stay out, however they are worded
        ("Nursing Assistant and Unit Secretary - Neuroscience", "York, PA", False),
        ("Patient Care Assistant - Neuro/Trauma", "Southeastern PA", False),
        ("Staffing Office Coordinator FT Night Nursing Admin", "Philadelphia, PA", False),
        ("Radiographer - Urgent Care", "York, PA", False),
        ("Value Analysis Coordinator", "York, PA", False),
        ("911 Dispatcher", "West Chester, PA", False),
        ("Deputy Sheriff III (Sergeant)", "West Chester, PA", False),
        ("Grant Writer", "Southeastern PA", False),
        # Geography is no longer decided here: these titles are fine roles and the run drops
        # them by distance (see the boundary section below).
        ("Social Service Coordinator", "Chambersburg, PA", True),
        ("Care Coordinator", "Waynesboro, PA", True),
        ("Care Coordinator", "Pittsburgh, PA", True),
        ("Care Coordinator", "Princeton, NJ", True),
        ("Care Coordinator", "York, PA", True),
        ("Care Coordinator", "Gettysburg, PA", True),
        ("Care Coordinator", "Ephrata, PA", True),
        ("Benefits Coordinator", "Drexel Hill, PA", True),
    ]
    # Note: the RN-required "Health Care Coordinator" is expected to PASS the title
    # filter (title looks relevant); it is caught by the description screen below.
    expect_rn_limitation = ("Health Care Coordinator-Adult Community", "Berwyn, PA")
    fails = 0
    for title, loc, want in corpus:
        # Cases whose rejection depends on the URL use a synthetic slug.
        url = ""
        if title == "Care Coordinator, Per Diem":
            url = "https://careers.mainlinehealth.org/posting/rn-care-coordinator-per-diem/12345/"
        got, reason = classify(title, loc, url)
        if (title, loc) == expect_rn_limitation:
            status = "KNOWN-LIMIT" if got else "ok"
        else:
            status = "PASS" if got == want else "FAIL"
            if status == "FAIL":
                fails += 1
        print("%-14s want=%-5s got=%-5s %-58s | %s" % (status, want, got, title[:58], reason))

    # Cities read off the posting: a real town is accepted whatever the curated list says,
    # because distance decides later. A region read off the posting has nothing to route to.
    derived_cases = [
        ("Care Coordinator", "Westtown, PA", True),
        ("Social Services Aide III, CYF", "Westtown, PA", True),
        ("Care Coordinator", "Dillsburg, PA", True),
        ("Care Coordinator", "Chambersburg, PA", True),
        ("Care Coordinator", "Pittsburgh, PA", True),
        ("Care Coordinator", "Princeton, NJ", True),
        ("Care Coordinator", "Dover, DE", True),
        ("Care Coordinator", "Southeastern PA", False),
    ]
    print("\n--- derived city (from the posting) ---")
    dvfails = 0
    for title, loc, want in derived_cases:
        got, reason = classify(title, loc, derived=True)
        ok = got == want
        if not ok:
            dvfails += 1
        print("%-6s want=%-5s got=%-5s %-40s | %s" % ("PASS" if ok else "FAIL", want, got, loc[:40], reason))

    print("\n--- description screen (detail_reject) ---")
    detail_corpus = [
        ("Must have Pennsylvania RN license! Melmark Overview", True),
        ("Current and active RN license in the state of Pennsylvania required.", True),
        ("Requires a current US RN License; BSN preferred.", True),
        ("RN licensure required within 6 months of hire.", True),
        ("Master's degree required. MPH preferred.", True),
        ("Current LPN license required.", True),
        ("Bachelor's degree in public health or health education "
         "required. One year of community outreach experience preferred.", False),
        ("Bachelor's degree in Social Work, Psychology, Public Health, or a "
         "related field. Bilingual Spanish a plus.", False),
        ("High school diploma or GED required. Prior healthcare experience preferred.", False),
        ("Bachelor's required. This position reports to the Director of Population Health.", False),
        ("", False),
    ]
    dfails = 0
    for text, want in detail_corpus:
        bad, phrase = detail_reject(text)
        ok = (bad == want)
        if not ok:
            dfails += 1
        print("%-6s want_reject=%-5s got=%-5s %-58s | %s"
              % ("PASS" if ok else "FAIL", want, bad, text[:58], phrase))

    print("\n--- jefferson url parser ---")
    jcases = [
        ("/en-US/ThomasJeffersonExternal/job/Philadelphia-PA/Care-Coordinator_REQ-123456-1",
         "Care Coordinator", "Philadelphia, PA"),
        ("/en-US/ThomasJeffersonExternal/job/Camden-County-NJ/Project-Coordinator_REQ-99",
         "Project Coordinator", "Camden County, NJ"),
        ("/en-US/ThomasJeffersonExternal/job/Montgomery-County-PA/Patient-Navigator_REQ-5",
         "Patient Navigator", "Montgomery County, PA"),
    ]
    jfails = 0
    for href, want_t, want_l in jcases:
        got = parse_jefferson(href, "", "")
        ok = got and got[1] == want_t and got[2] == want_l
        if not ok:
            jfails += 1
        print("%-6s %-58s -> %s | %s" % ("PASS" if ok else "FAIL", href.split("/job/")[1][:58],
                                         got[1] if got else "None", got[2] if got else "None"))

    print("\n--- penn table-layout parser ---")
    table_text = ("Showing 1-13 of 13 results Job ID Job Title Job Category Location Work Type "
                  "348971 Community Health Educator- Per Diem Other West Chester, PA, United States PER DIEM "
                  "347750 Perioperative Clinical Educator Other Philadelphia, PA, United States FT")
    pcases = [
        ("https://careers.pennmedicine.org/jobs/18372008-community-health-educator-per-diem",
         table_text, "Community Health Educator Per Diem", "West Chester, PA, United States"),
        ("https://careers.pennmedicine.org/jobs/18329801-patient-navigator-cardiac-testing",
         "Showing 1-6 of 6 results Job ID Job Title Job Category Location Work Type "
         "345417 Patient Navigator Cardiac Testing NEW Clerical/Administrative Doylestown, PA, United States FT "
         "347690 Patient Navigator Parkesburg Clinical Support Parkesburg, PA, United States FT",
         "Patient Navigator Cardiac Testing", "Doylestown, PA, United States"),
        # A card with no recognised category used to keep the location and eat the title's
        # last words into the location ("Process Improvement Philadelphia, PA, United States").
        ("https://careers.pennmedicine.org/jobs/18196623-master-improvement-advisor",
         "Master Improvement Advisor Business Process Improvement Philadelphia, PA, United States FT "
         "| Process Improvement Philadelphia, PA, United States",
         "Master Improvement Advisor Business Process Improvement", "Philadelphia, PA, United States"),
        ("https://careers.pennmedicine.org/jobs/18000001-care-coordinator-west-chester",
         "Care Coordinator West Chester, PA, United States FT",
         "Care Coordinator", "West Chester, PA, United States"),
    ]
    pfails = 0
    for href, txt, want_t, want_l in pcases:
        got = parse_penn(href, txt, "")
        ok = got and got[1].strip() == want_t and got[2] == want_l
        if not ok:
            pfails += 1
        print("%-6s %-50s -> %r | %r" % ("PASS" if ok else "FAIL", want_t[:50],
                                         got[1] if got else None, got[2] if got else None))

    print("\n--- PHMC (UKG) card parser ---")
    # The card text now carries the real address after " ||| "; the title keeps its posted-date
    # suffix in the DOM, so it must be stripped here rather than by the anchor regex.
    hcases = [
        ("/PUB1002/JobBoard/c8784846-358b-1bec-45e9-f994af5fccee/OpportunityDetail"
         "?opportunityId=5a211a77-290b-42cf-b922-41d0a3ae27bb",
         "Housing Stabilization Specialist ||| Philadelphia, PA 19139, USA",
         "Housing Stabilization Specialist", "Philadelphia, PA 19139, USA"),
        ("/PUB1002/JobBoard/c8784846-358b-1bec-45e9-f994af5fccee/OpportunityDetail"
         "?opportunityId=818f1f84-6ac5-4536-aecd-ec3ce12a9eaa",
         "IBHS Clinician (Fee-for-Service) ||| Norristown, PA 19401, USA",
         "IBHS Clinician (Fee-for-Service)", "Norristown, PA 19401, USA"),
        # No address on the card: the title must still survive, and location stays empty so the
        # source default is used (and flagged as guessed).
        ("/PUB1002/JobBoard/c8784846-358b-1bec-45e9-f994af5fccee/OpportunityDetail"
         "?opportunityId=4ab679e6-a68e-4f9f-bb7f-eae5d3b5f5fb",
         "Case Manager I ||| ", "Case Manager I", ""),
        # An anchor without an opportunity id is not a posting.
        ("/PUB1002/JobBoard/c8784846-358b-1bec-45e9-f994af5fccee?q=", "Search", "", ""),
    ]
    hfails = 0
    for href, txt, want_t, want_l in hcases:
        got = parse_phmc(href, txt, "")
        if not want_t:
            ok = got is None
            shown = got
        else:
            ok = got and got[1] == want_t and got[2] == want_l
            shown = (got[1], got[2]) if got else None
        if not ok:
            hfails += 1
        print("%-6s %-46s -> %r" % ("PASS" if ok else "FAIL", (want_t or "not a posting")[:46], shown))

    # Posted-date normalisation: the date must be right, or the board silently lies.
    from datetime import date as _date
    today = _date(2026, 11, 4)
    dcases = [
        ("Today", "2026-11-04"),
        ("Yesterday", "2026-11-03"),
        ("3 days ago", "2026-11-01"),
        ("30+ days ago", "2026-10-05"),
        ("2 weeks ago", "2026-10-21"),
        ("11/02/2026", "2026-11-02"),
        ("11/2/26", "2026-11-02"),
        ("2026-11-01", "2026-11-01"),
        ("Nov 2, 2026", "2026-11-02"),
        ("Posted Nov 2, 2026", "2026-11-02"),
        ("Sept 30, 2026", "2026-09-30"),
        ("", None),
        (None, None),
        ("whenever", None),
    ]
    printf = "\n%-6s %-28s -> %r"
    print("\n--- posted date parsing ---")
    postfails = 0
    for hint, want in dcases:
        got = parse_posted(hint, today)
        ok = got == want
        if not ok:
            postfails += 1
        print(printf % ("PASS" if ok else "FAIL", str(hint)[:28], got))

    bcases = [
        ("Apply locations Philadelphia, PA posted on Posted Yesterday job requisition id REQ-1", "2026-11-03"),
        ("posted on Posted 2 Days Ago job requisition id REQ-2", "2026-11-02"),
        ("Requisition Number: PROVI007178 Posting Details Posted: October 14, 2026 Full-Time", "2026-10-14"),
        ("Posted Date: 11/02/2026", "2026-11-02"),
        ("Skip to main content | Careers Search All Jobs Saved Jobs", None),
        ("", None),
    ]
    print("\n--- posted date from detail page ---")
    bfails = 0
    for body, want in bcases:
        got = posted_from_body(body, today)
        ok = got == want
        if not ok:
            bfails += 1
            postfails += 1
        print("%-6s %-28s -> %r" % ("PASS" if ok else "FAIL", body[:28].replace("\n", " "), got))

    # A guessed default location must be replaced by the real city on the detail page.
    print("\n--- location from detail page ---")
    lcases = [
        ("Social Service Coordinator Apply Now 112 N 7th St-Chambersburg Hospital, "
         "Chambersburg,PA Part Time Day Shift",
         "Chambersburg, PA"),
        ("Social Service Coordinator 112 N 7th St-Chambersburg Hospital, Chambersburg,PA "
         "... WellSpan Health, York, PA is one of America's Greatest Places to work",
         "Chambersburg, PA"),
        ("Care Coordinator WellSpan York Hospital, York, PA Full Time",
         "York, PA"),
        ("Rehab Liaison 1001 S George St-York Hospital, York,PA",
         "York, PA"),
        ("Helper 4 Spruce St-Ephrata Community Hospital, Ephrata,PA",
         "Ephrata, PA"),
        # Tower publishes the full state name, not the abbreviation
        ("Patient Access Representative Reading, Pennsylvania Full Time", "Reading, PA"),
        ("Patient Services Rep Philadelphia, Pennsylvania 40 hrs", "Philadelphia, PA"),
        # A label sitting in front of the city must not become part of it
        ("Benefits Coordinator Annually Location Drexel Hill, PA Full Time", "Drexel Hill, PA"),
        ("Skip to main content | Careers Search All Jobs", ""),
        ("", ""),
    ]
    lfails = 0
    for body, want in lcases:
        got = location_from_body(body)
        ok = got == want
        if not ok:
            lfails += 1
        print("%-6s %-46s -> %r" % ("PASS" if ok else "FAIL", body[:46].replace("\n", " "), got))

    print("\n--- route card helpers ---")
    # openrouteservice speaks Pelias/GeoJSON for geocoding; the map itself is drawn in the
    # browser from the encoded polyline, so these are the only shapes the Pi has to parse.
    # Real candidates for "Northeast Philadelphia, PA": the locality-only filter used to return
    # exactly this garbage, and picking the first USA hit put the card 50 miles from York.
    ne_philly = {"features": [
        {"properties": {"label": "Castelnovo ne' Monti, Italy", "region": "Emilia-Romagna"},
         "geometry": {"coordinates": [10.4031, 44.4339]}},
        {"properties": {"label": "Northeast Ithaca, NY, USA", "region": "New York",
                        "region_a": "NY", "name": "Northeast Ithaca"},
         "geometry": {"coordinates": [-76.4620, 42.4711]}},
        {"properties": {"label": "Northeast Philadelphia, Philadelphia County, PA, USA",
                        "region": "Pennsylvania", "region_a": "PA", "name": "Northeast Philadelphia"},
         "geometry": {"coordinates": [-75.0290, 40.0642]}},
    ]}
    mcases = [
        ("pick ne philly", _pick_point(ne_philly, "Northeast Philadelphia, PA") == (40.0642, -75.0290),
         _pick_point(ne_philly, "Northeast Philadelphia, PA")),
        # The state and the name both have to line up, or it is not our place.
        ("pick wrong state", _pick_point(ne_philly, "Northeast Philadelphia, DE") is None,
         _pick_point(ne_philly, "Northeast Philadelphia, DE")),
        ("pick empty", _pick_point({"features": []}, "York, PA") is None,
         _pick_point({"features": []}, "York, PA")),
    ]
    # Shortened street suffixes and hyphenated names are the same place.
    street = {"features": [{"properties": {
        "label": "1400 Country Club Road, York, PA, USA", "region": "Pennsylvania",
        "region_a": "PA", "name": "1400 Country Club Road"},
        "geometry": {"coordinates": [-76.7425, 39.9300]}}]}
    mcases.append(("pick street address",
                   _pick_point(street, "1400 Country Club Rd, York, PA 17403") == (39.93, -76.7425),
                   _pick_point(street, "1400 Country Club Rd, York, PA 17403")))
    hyphen = {"features": [{"properties": {
        "label": "Bala-Cynwyd, PA, USA", "region": "Pennsylvania", "region_a": "PA",
        "name": "Bala-Cynwyd"}, "geometry": {"coordinates": [-75.2336, 40.0081]}}]}
    mcases.append(("pick hyphenated",
                   _pick_point(hyphen, "Bala Cynwyd, PA") == (40.0081, -75.2336),
                   _pick_point(hyphen, "Bala Cynwyd, PA")))
    # A same-state decoy must not win just because it shares the first word.
    decoy = {"features": [{"properties": {
        "label": "West Goshen, PA, USA", "region": "Pennsylvania", "region_a": "PA",
        "name": "West Goshen"}, "geometry": {"coordinates": [-75.5700, 39.9700]}}]}
    mcases.append(("pick rejects decoy", _pick_point(decoy, "West Chester, PA") is None,
                   _pick_point(decoy, "West Chester, PA")))
    mcases.append(("state of", _state_of("Northeast Philadelphia, PA") == "PA"
                   and _state_of("Wilmington, Delaware") == "DE"
                   and _state_of("York, PA 17403") == "PA"
                   and _state_of("Southeastern PA") == "PA",
                   [_state_of(x) for x in ("Northeast Philadelphia, PA", "Wilmington, Delaware",
                                           "York, PA 17403", "Southeastern PA")]))
    # The two coordinates involved, measured from Country Club of York.
    york_cc = (39.9276, -76.7452)
    ne_true = (40.0642, -75.0290)      # real Northeast Philadelphia, ~96 mi away
    ne_wrong = (40.4048, -77.4455)     # what the locality-only filter returned, ~50 mi away
    mcases.append(("straight line to NE Philly",
                   abs(_straight_miles(*york_cc, *ne_true) - 96) < 8,
                   round(_straight_miles(*york_cc, *ne_true), 1)))
    # A 59 mi route to a place 50 mi away is plausible, so the length guard stays silent; that
    # is exactly why the fix belongs in _pick_point. It still catches an impossible distance.
    mcases.append(("bad hit is plausible",
                   _route_too_short(59, _straight_miles(*york_cc, *ne_wrong)) is False,
                   round(_straight_miles(*york_cc, *ne_wrong), 1)))
    mcases.append(("impossible route caught", _route_too_short(59, 96.0) is True,
                   _route_too_short(59, 96.0)))
    mcases.append(("normal route allowed", _route_too_short(118, 96.0) is False,
                   _route_too_short(118, 96.0)))

    def decode_polyline(text: str, precision: int = 5) -> list:
        """The exact algorithm the dashboard uses, so the Pi-side test proves the pair agree."""
        factor, idx, lat, lon, out = 10 ** precision, 0, 0, 0, []
        while idx < len(text):
            deltas = []
            for _ in range(2):
                shift, result = 0, 0
                while True:
                    b = ord(text[idx]) - 63
                    idx += 1
                    result |= (b & 0x1F) << shift
                    shift += 5
                    if b < 0x20:
                        break
                deltas.append(~(result >> 1) if result & 1 else (result >> 1))
            lat, lon = lat + deltas[0], lon + deltas[1]
            out.append((lon / factor, lat / factor))
        return out

    # The reference vector from the encoded-polyline spec, decoded back to the original points.
    spec = "_p~iF~ps|U_ulLnnqC_mqNvxq`@"
    want = [(-120.2, 38.5), (-120.95, 40.7), (-126.453, 43.252)]
    got = decode_polyline(spec, 5)
    drift = max(max(abs(a - b) for a, b in zip(p, q)) for p, q in zip(want, got))
    mcases.append(("polyline spec", len(got) == 3 and drift < 1e-5, round(drift, 9)))
    # A route we would really store: York -> West Chester, trimmed to four points.
    live = "ufurFz_msM~@n@"
    mcases.append(("live geometry decodes", len(decode_polyline(live)) == 2,
                   decode_polyline(live)))
    # The stored minutes are corrected for the OSM speed model, so check the correction too.
    mcases.extend([
        ("calibrate 49", calibrate(49) == 40, calibrate(49)),
        ("calibrate 110", calibrate(110) == 90, calibrate(110)),
        ("calibrate floor", calibrate(1) == 1, calibrate(1)),
        ("calibrate none", calibrate(None) is None, calibrate(None)),
    ])
    # A failed render must come back on the next run, a rendered or knowingly skipped one must not.
    settled = [
        ("settled fresh", _map_settled({}) is False),
        ("settled done", _map_settled({"at": "2026-01-01", "min": 42}) is True),
        ("settled no city", _map_settled({"skipped": "no city"}) is True),
        ("settled retry", _map_settled({"err": "HTTP 401 Unauthorized"}) is False),
    ]
    mcases.extend([(n, ok, ok) for n, ok in settled])

    # Real location strings off the board that used to be rejected as un-geocodable.
    lc = [
        ("West Chester, PA, United States", "West Chester, PA", True),
        ("Philadelphia, PA, United States", "Philadelphia, PA", True),
        ("Wilmington, Delaware", "Wilmington, DE", True),
        ("Bala Cynwyd, PA, United States", "Bala Cynwyd, PA", True),
        ("York, PA 17403", "York, PA 17403", True),
        ("Lancaster, PA", "Lancaster, PA", True),
        ("Southeastern PA", "Southeastern PA", False),
        ("Southeastern, PA", "Southeastern, PA", False),
        ("Remote", "Remote", False),
        ("", "", False),
    ]
    for raw, want_norm, want_ok in lc:
        norm = normalize_location(raw)
        mcases.append(("loc %r" % raw[:22], norm == want_norm and geocodable(norm) == want_ok,
                       (norm, geocodable(norm))))
    mfails = 0
    for name, ok, got in mcases:
        if not ok:
            mfails += 1
        print("%-6s %-20s -> %r" % ("PASS" if ok else "FAIL", name, got))

    print("\n--- distance boundary (%d min from either cluster) ---" % CLUSTER_MAX_MINUTES)
    bfails = _boundary_selftest()

    print("\n--- one scan at a time ---")
    try:
        import fcntl  # noqa: F401
        posix_lock = True
    except ImportError:
        posix_lock = False
    lock_tmp = Path(tempfile.mkdtemp(prefix="jw-lock-")) / "test.lock"
    first = acquire_run_lock(lock_tmp)
    second = acquire_run_lock(lock_tmp)
    release_run_lock()
    again = acquire_run_lock(lock_tmp)
    release_run_lock()
    shutil.rmtree(lock_tmp.parent, ignore_errors=True)
    lk_cases = [
        ("acquire", first is True, first),
        # Without fcntl (the Windows dev box) the lock is a no-op by design, so only assert on POSIX.
        ("second blocked", (second is False) if posix_lock else True,
         second if posix_lock else "skipped (no fcntl)"),
        ("reacquire after release", again is True, again),
    ]
    lkfails = 0
    for name, ok, got in lk_cases:
        if not ok:
            lkfails += 1
        print("%-6s %-24s -> %r" % ("PASS" if ok else "FAIL", name, got))


    print("\n%d title failures, %d derived failures, %d detail failures, %d jefferson failures, "
          "%d penn failures, %d posted failures, %d location failures, %d map failures, "
          "%d boundary failures, %d lock failures, %d phmc failures, %d cases total"
          % (fails, dvfails, dfails, jfails, pfails, postfails, lfails, mfails, bfails,
             lkfails, hfails,
             len(corpus) + len(derived_cases) + len(detail_corpus) + len(jcases) + len(pcases)
             + len(dcases) + len(bcases) + len(lcases) + len(mcases)
             + len(lk_cases) + len(hcases) + 8))
    return 0 if (fails == 0 and dvfails == 0 and dfails == 0 and jfails == 0 and pfails == 0
                 and postfails == 0 and lfails == 0 and mfails == 0 and bfails == 0
                 and lkfails == 0 and hfails == 0) else 1


def backfill_posted(st) -> int:
    """One-off pass: read a posted date off every known posting that lacks one.

    Sources that publish a date on the results page already have it; this covers the
    ones (Workday, UltiPro) that only show it on the detail page.
    """
    seen = st.get("seen", {})
    todo = [(k, v) for k, v in seen.items()
            if v.get("url") and not v.get("posted")
            and classify(v.get("title", ""), v.get("location", ""), v.get("url", ""))[0]]
    print("backfill: %d relevant postings with no posted date" % len(todo))
    updates, hits = {}, 0
    for k, v in todo:
        try:
            _body, got = posted_for(v["url"])
        except Exception as e:
            print("  skip %-40s %s" % ((v.get("title") or "")[:40], str(e)[:60]))
            continue
        if not got:
            continue
        v["posted"] = got
        hits += 1
        updates[k] = {"employer": v["employer"], "id": k.split("|", 1)[1],
                      "title": v["title"], "location": v.get("location"),
                      "url": v["url"], "posted": got}
        print("  %-22s %-42s -> %s" % (v["employer"][:22], (v["title"] or "")[:42], got))
    st["seen"] = seen
    save_state(st)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print("found dates for %d of %d" % (hits, len(todo)))
    if updates:
        print("dashboard sync: %s" % push_snapshot(updates, now, full=False))
    return 0


def backfill_location(st) -> int:
    """One-off pass: replace a guessed city with the one printed on the posting.

    Sources whose result cards omit the city carry a `default_location` (WellSpan -> "York,
    PA", Tower -> "Southeastern PA"). The default is only ever a stand-in: a WellSpan job can
    be in Chambersburg or Lancaster, and a Tower job in Philadelphia. Reads the detail page for
    every known posting still carrying a guess and records the real city in `st["locations"]`,
    which every later run applies before `classify()`.
    """
    seen = st.get("seen", {})
    known = st.setdefault("locations", {})
    # State written before this existed has no location_guessed flag, so also treat "the city
    # equals this employer's default" as a guess - that is exactly what the old code stored.
    defaults = {s["name"]: s["default_location"] for s in SOURCES if s.get("default_location")}
    # Only postings that are currently on the board: `seen` holds every posting ever observed
    # (hundreds), and reading a detail page for each would take the better part of an hour.
    todo = [(k, v) for k, v in seen.items()
            if v.get("url") and k not in known
            and (v.get("location_guessed")
                 or v.get("location") == defaults.get(v.get("employer")))
            and classify(v.get("title", ""), v.get("location", ""), v.get("url", ""))[0]]
    print("backfill: %d postings with a guessed city" % len(todo))
    updates, hits, moved, empty = {}, 0, [], 0
    for k, v in todo:
        try:
            body, _posted = posted_for(v["url"])
        except Exception as e:
            print("  skip %-40s %s" % ((v.get("title") or "")[:40], str(e)[:60]))
            continue
        if not body:
            empty += 1
        real = location_from_body(body)
        if not real:
            print("  %-22s %-40s -> no city on page" % (v["employer"][:22], (v.get("title") or "")[:40]))
            continue
        known[k] = real
        hits += 1
        ok, why = classify(v.get("title", ""), real, v.get("url", ""), derived=True)
        print("  %-22s %-38s -> %-20s %s"
              % (v["employer"][:22], (v.get("title") or "")[:38], real, "keep" if ok else "DROP (%s)" % why))
        if ok:
            updates[k] = {"employer": v["employer"], "id": k.split("|", 1)[1],
                          "title": v["title"], "location": real,
                          "url": v["url"], "posted": v.get("posted")}
        else:
            moved.append((v.get("employer"), v.get("title"), real))
    save_state(st)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print("resolved %d of %d" % (hits, len(todo)))
    if empty:
        print("WARNING: %d detail page(s) returned no text - browser problem, not 'no city'"
              % empty)
    if updates:
        print("dashboard sync: %s" % push_snapshot(updates, now, full=False))
    for emp, title, real in moved:
        print("OUT OF CORRIDOR: %s | %s | %s (purge this row on the board)" % (emp, title, real))
    return 0


def push_maps(maps: list, run_at: str) -> str:
    """Upload route cards. Batched: Vercel caps a request body at 4.5 MB."""
    if not WEB_URL or not WEB_TOKEN:
        return "skipped (no web credentials)"
    if not maps:
        return "skipped (no new maps)"
    sent = 0
    for i in range(0, len(maps), 8):
        chunk = maps[i:i + 8]
        try:
            _http_json("POST", WEB_URL + "/api/ingest",
                       {"run_at": run_at, "maps": chunk},
                       {"x-ingest-token": WEB_TOKEN}, timeout=90)
            sent += len(chunk)
        except Exception as e:
            return "FAILED after %d map(s): %s" % (sent, str(e)[:120])
    return "ok (%d map%s)" % (sent, "" if sent == 1 else "s")


def _err_text(e) -> str:
    """An HTTP error body is the only thing that says *why* a provider refused us."""
    if isinstance(e, urllib.error.HTTPError):
        try:
            body = e.read().decode("utf-8", "replace")
            return "HTTP %s %s" % (e.code, " ".join(body.split())[:160])
        except Exception:
            return "HTTP %s" % e.code
    return " ".join(str(e).split())[:200]


# The free openrouteservice plan allows 40 requests a minute, and a backfill of the whole board
# fires two per posting; without this it throttles and the cards silently come back empty.
ORS_MIN_INTERVAL = 1.5
# The OSRM demo server asks callers to stay around one request a second.
OSRM_MIN_INTERVAL = 1.2
_last_ors_call = [0.0]


def _ors_json(method: str, url: str, body=None, headers=None):
    """ORS call with free-tier pacing.

    Only a rate limit (429) is worth waiting out - it clears in seconds. A 403 is the daily
    quota, which will not clear inside a run, so it is raised at once and the caller falls back.
    """
    for attempt in range(2):
        gap = time.time() - _last_ors_call[0]
        if gap < ORS_MIN_INTERVAL:
            time.sleep(ORS_MIN_INTERVAL - gap)
        _last_ors_call[0] = time.time()
        try:
            return _http_json(method, url, body, headers, timeout=30)
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 1:
                time.sleep(20)
                continue
            raise


# Both OSM routers we measured (openrouteservice and OSRM) land 20-25 % above what Google Maps
# reports for these corridors: they price a route from OSM speed limits with a hair cut plus a
# fixed penalty at every turn and signal (ORS gave US-30 41 mph where the limit is 55), while
# Google's "typical" figure is measured from real traffic. Three checks agree on ~0.8 - the
# observed ~10 minute gap, OSRM matching ORS within 5 %, and the US-30 step - so the stored
# minutes are corrected once here. `raw_min` keeps the router's own number for audit.
DRIVE_CALIBRATION = 0.82


def calibrate(minutes):
    """Map a router's free-flow minutes onto something comparable to Google's estimate."""
    if minutes is None:
        return None
    return max(1, int(round(minutes * DRIVE_CALIBRATION)))


def _state_of(loc: str) -> str:
    """"Northeast Philadelphia, PA" -> "PA"; "" when the string carries no state."""
    s = re.sub(r"\s+\d{5}(?:-\d{4})?$", "", (loc or "").strip())
    for abbr, names in _STATE_NAMES.items():
        for name in names:
            # Either "City, PA" or a bare "... PA"; a word boundary keeps "Parade" out.
            if re.search(r"(?:^|[\s,])%s$" % re.escape(name), s, re.I):
                return abbr
    return ""


# Street suffix shorthand, so a query and a label that spell it differently still compare.
_SUFFIX = {
    "rd": "road", "st": "street", "ave": "avenue", "av": "avenue", "blvd": "boulevard",
    "dr": "drive", "ln": "lane", "ct": "court", "hwy": "highway", "pkwy": "parkway",
    "pl": "place", "sq": "square", "ter": "terrace", "cir": "circle", "trl": "trail",
    "n": "north", "s": "south", "e": "east", "w": "west",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _age_hours(stamp) -> float:
    """Hours since an ISO-8601 stamp; 0 when it is missing or unreadable (so callers retry)."""
    try:
        then = datetime.strptime(str(stamp), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return 0.0
    return (datetime.now(timezone.utc) - then).total_seconds() / 3600.0


def _tokens(text) -> list:
    """Lower-case word/number tokens, so "Bala-Cynwyd", "Bala Cynwyd" and "Rd"/"Road" compare."""
    return [_SUFFIX.get(t, t) for t in re.findall(r"[a-z0-9]+", (text or "").lower())]


def _pick_point(data: dict, wanted: str):
    """Choose the geocoder hit that actually is the place we asked for.

    Pelias happily returns a fuzzy match: asking for "Northeast Philadelphia, PA" with a
    locality-only layer filter produced "Northeast Ithaca, NY" and, once filtered to the USA,
    a locality 50 miles from York. So a hit must be in the right state and contain every word of
    the query (after normalising street suffixes) - which still tolerates "1400 Country Club Rd"
    matching "1400 Country Club Road" but rejects the decoy "West Goshen" for "West Chester".
    """
    head = _tokens((wanted or "").split(",")[0])
    state = _state_of(wanted)
    for feat in data.get("features") or []:
        props = feat.get("properties") or {}
        coords = (feat.get("geometry") or {}).get("coordinates") or []
        if len(coords) < 2:
            continue
        region = _tokens("%s %s" % (props.get("region_a") or "", props.get("region") or ""))
        if state and not any(n in region for n in _STATE_NAMES[state]):
            continue
        words = set(_tokens(props.get("label")) + _tokens(props.get("name")))
        if head and not set(head) <= words:
            continue
        return float(coords[1]), float(coords[0])
    return None


def geocode(location: str):
    """"City, ST" -> (lat, lon) via openrouteservice. None when nothing matches convincingly."""
    if not ORS_KEY:
        return None
    try:
        q = urllib.parse.urlencode({
            "api_key": ORS_KEY, "text": location, "size": 5, "boundary.country": "USA",
            "lang": "en",
        })
        got = _pick_point(_ors_json(
            "GET", "https://api.openrouteservice.org/geocode/search?" + q), location)
        if got:
            return got
        MAP_ERROR["geocode"] = "no match for %r in %s" % (location, _state_of(location) or "?")
    except Exception as e:
        MAP_ERROR["geocode"] = _err_text(e)
    return None


def snap_point(lat: float, lon: float) -> tuple:
    """Move a geocoded point onto the road network.

    A city's geocoded centre can sit on water or in a park — openrouteservice puts Wilmington,
    DE in the Delaware River — and directions then refuse it outright, so snap first.
    """
    try:
        data = _ors_json(
            "POST", "https://api.openrouteservice.org/v2/snap/driving-car",
            {"locations": [[lon, lat]], "radius": 2000},
            {"Authorization": ORS_KEY, "Accept": "application/json"})
        got = ((data.get("locations") or [{}])[0] or {}).get("location") or []
        if len(got) >= 2:
            return float(got[1]), float(got[0])
    except Exception as e:
        MAP_ERROR["geocode"] = _err_text(e)
    return lat, lon


# Keyless fallback for when openrouteservice is out of quota. The OSRM demo server is for light
# use, which this is (~15 calls a day, one per new city), and it returns the same precision-5
# polyline, so nothing downstream cares which engine produced a route.
OSRM_URL = "https://router.project-osrm.org/route/v1/driving"


def _osrm_json(path: str):
    gap = time.time() - _last_ors_call[0]
    if gap < OSRM_MIN_INTERVAL:
        time.sleep(OSRM_MIN_INTERVAL - gap)
    _last_ors_call[0] = time.time()
    req = urllib.request.Request(path, headers={
        "User-Agent": "pop-health-jobs/1.0 (personal job board)",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def route_via_osrm(cluster: str, dest_lat: float, dest_lon: float) -> dict:
    """Same shape as route_summary(), from a service with no key and no daily cap."""
    origin = ORIGINS[cluster]
    url = ("%s/%.5f,%.5f;%.5f,%.5f?overview=full&geometries=polyline&alternatives=false&steps=false"
           % (OSRM_URL, origin["lon"], origin["lat"], dest_lon, dest_lat))
    data = _osrm_json(url)
    route = (data.get("routes") or [{}])[0]
    seconds = float(route.get("duration") or 0)
    metres = float(route.get("distance") or 0)
    if not seconds or not route.get("geometry"):
        raise RuntimeError("osrm returned no route")
    raw = int(round(seconds / 60))
    return {
        "minutes": calibrate(raw),
        "raw_minutes": raw,
        "miles": round(metres / 1609.344, 1) if metres else None,
        "geometry": route["geometry"],
        "source": "osrm",
    }


def _out_of_quota(text: str) -> bool:
    return any(t in (text or "") for t in ("Quota", "quota", "429", "403", "Too Many"))


# Set once openrouteservice refuses on quota, so the rest of the run goes straight to OSRM.
ORS_SPENT = [False]


def _straight_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle-ish distance, accurate enough to sanity-check a route length."""
    dlat = (lat2 - lat1) * 69.05
    dlon = (lon2 - lon1) * 69.05 * math.cos(math.radians((lat1 + lat2) / 2.0))
    return math.hypot(dlat, dlon)


def nearest_cluster(lat: float, lon: float) -> str:
    """Whichever cluster pin is closer in a straight line - the only cheap way to choose."""
    return min(ORIGINS, key=lambda k: _straight_miles(
        ORIGINS[k]["lat"], ORIGINS[k]["lon"], lat, lon))


def route_summary(cluster: str, dest_lat: float, dest_lon: float) -> dict:
    """Drive from one cluster pin to a destination, plus the route as an encoded polyline.

    openrouteservice first; if it refuses because the day's quota is spent, the keyless OSRM
    demo answers instead so a run still produces a board. Which engine answered is recorded.
    """
    origin = ORIGINS[cluster]
    empty = {"minutes": None, "raw_minutes": None, "miles": None, "geometry": ""}
    if not ORS_KEY:
        return empty
    if not ORS_SPENT[0]:
        try:
            data = _ors_json(
                "POST", "https://api.openrouteservice.org/v2/directions/driving-car",
                {"coordinates": [[origin["lon"], origin["lat"]], [dest_lon, dest_lat]],
                 "instructions": False},
                {"Authorization": ORS_KEY, "Accept": "application/json"})
            route = (data.get("routes") or [{}])[0]
            summary = route.get("summary") or {}
            seconds = float(summary.get("duration") or 0)
            metres = float(summary.get("distance") or 0)
            raw = int(round(seconds / 60)) if seconds else None
            return {
                "minutes": calibrate(raw),
                "raw_minutes": raw,
                "miles": round(metres / 1609.344, 1) if metres else None,
                # openrouteservice encodes at precision 5; the browser decodes it to draw it.
                "geometry": route.get("geometry") or "",
                "source": "ors",
            }
        except Exception as e:
            MAP_ERROR["route"] = _err_text(e)
            if not _out_of_quota(MAP_ERROR["route"]):
                return empty
            # The quota will not come back during this run: stop asking, use the fallback.
            ORS_SPENT[0] = True
    try:
        return route_via_osrm(cluster, dest_lat, dest_lon)
    except Exception as e:
        MAP_ERROR["route"] = "%s (osrm fallback: %s)" % (MAP_ERROR["route"], _err_text(e))
        return empty


def _remember(routes: dict, loc: str, rec: dict) -> dict:
    """Cache a per-city answer, unless it is a quota/rate refusal that clears on its own.

    A 403 "Quota exceeded" must not be remembered for the retry window below: the moment the
    daily reset lands, the next run should resolve it, not wait a day.
    """
    text = str(rec.get("error") or rec.get("skipped") or "")
    if not any(t in text for t in ("Quota", "quota", "429", "403", "Too Many")):
        routes[loc] = rec
    return rec


def pin_stamp() -> str:
    """Fingerprint of the two pins, so editing one invalidates every cached route."""
    return "|".join("%s:%.4f,%.4f" % (k, v["lat"], v["lon"]) for k, v in sorted(ORIGINS.items()))


def resolve_route(loc, st: dict) -> dict:
    """Route a normalized location from its cluster pin, caching the answer per location.

    Every posting in a city shares one route, so this runs per distinct city (a dozen or so),
    not per job. Returns a record with `cluster`, `minutes` and `geometry`, or `error`/`skipped`.
    """
    routes = st.setdefault("routes", {})
    stamp = pin_stamp()
    cached = routes.get(loc) or {}
    if cached and cached.get("pins") not in (None, stamp):
        # The pins moved since this was measured: the drive no longer means anything.
        cached = {}
    if cached and (cached.get("at") or cached.get("error")):
        # An error is retried once a day; a real answer is reused for good.
        if cached.get("error") and _age_hours(cached.get("at")) > 24:
            cached = {}
        else:
            return cached
    if not loc or not geocodable(loc):
        return _remember(routes, loc, {"skipped": "no city", "at": now_iso()})

    geo = st.setdefault("geo", {})
    coords = geo.get(loc)
    if not coords:
        coords = geocode(loc)
        if not coords:
            return _remember(routes, loc,
                             {"skipped": MAP_ERROR["geocode"] or "not found", "at": now_iso()})
        coords = snap_point(coords[0], coords[1])
        geo[loc] = list(coords)

    cluster = nearest_cluster(coords[0], coords[1])
    route = route_summary(cluster, coords[0], coords[1])
    if not route["minutes"] or not route["geometry"]:
        # A near-equidistant address can be over the line from its nearest pin but inside it
        # from the other one, so try the second pin once before giving up.
        other = [k for k in ORIGINS if k != cluster][0]
        alt = route_summary(other, coords[0], coords[1])
        if alt["minutes"] and alt["geometry"]:
            cluster, route = other, alt
        if not route["minutes"] or not route["geometry"]:
            return _remember(routes, loc,
                             {"error": MAP_ERROR["route"] or "directions failed", "at": now_iso()})
    elif route["minutes"] > CLUSTER_MAX_MINUTES:
        other = [k for k in ORIGINS if k != cluster][0]
        alt = route_summary(other, coords[0], coords[1])
        if alt["minutes"] and alt["minutes"] < route["minutes"]:
            cluster, route = other, alt

    straight = _straight_miles(
        ORIGINS[cluster]["lat"], ORIGINS[cluster]["lon"], coords[0], coords[1])
    if _route_too_short(route["miles"], straight):
        return _remember(routes, loc,
                         {"error": "%s mi route beats the %.0f mi straight line"
                                   % (route["miles"], straight), "at": now_iso()})

    routes[loc] = {
        "at": now_iso(),
        "pins": stamp,
        "cluster": cluster,
        "label": ORIGINS[cluster]["label"],
        "min": route["minutes"],
        "raw": route["raw_minutes"],
        "mi": route["miles"],
        "geometry": route["geometry"],
        "src": route.get("source") or "ors",
    }
    return routes[loc]


def _route_too_short(route_miles, straight_miles) -> bool:
    """A road distance below the straight line between its ends cannot be real.

    Catches a reversed or scrambled coordinate, not a wrong-but-nearby geocode: a route to a
    place 50 miles away that reports 59 miles is geographically plausible, so only the
    geocoder's own state/name check can reject that one.
    """
    return bool(route_miles and straight_miles > 5 and route_miles < straight_miles * 0.9)


def _map_settled(rec: dict) -> bool:
    """Rendered, or knowingly skipped. Anything else is retried on the next run."""
    return bool(rec.get("skipped") or rec.get("at"))


def place_of(job: dict, st: dict) -> dict:
    """The cluster verdict for one posting: which cluster, how far, or why it was dropped."""
    return resolve_route(normalize_location(job.get("location")), st)


def in_range(place: dict) -> bool:
    """Inside the 50 minute boundary of one of the two clusters - the only geography rule."""
    return bool(place.get("cluster")) and (place.get("min") or 10 ** 6) <= CLUSTER_MAX_MINUTES


def _place_reason(place: dict) -> str:
    """How to say why a posting is not on the board, for --dump and the run log."""
    if place.get("min"):
        return "distance: %d min from %s" % (place["min"], place.get("label") or "?")
    return "distance: %s" % (place.get("error") or place.get("skipped") or "unresolved")


def build_maps(jobs: dict, st: dict, now: str) -> tuple[list, dict, dict]:
    """Turn already-resolved places into route cards, and say what got dropped.

    No routing happens here: `resolve_route` has done it once per city. Returns the cards to
    push, the minutes per job (for the Telegram alert) and a tally of why the rest went.
    """
    if not has_map_keys():
        return [], {}, {"unresolved": len(jobs)}
    done = st.setdefault("maps_done", {})
    out, drive, dropped = [], {}, {}
    for key, job in jobs.items():
        place = place_of(job, st)
        if not place.get("cluster"):
            reason = ("out of range" if place.get("min") else "unresolved")
            dropped[reason] = dropped.get(reason, 0) + 1
            continue
        if not in_range(place):
            dropped["out of range"] = dropped.get("out of range", 0) + 1
            continue
        rec = done.get(key) or {}
        # Re-push when the answer changed (a whole cluster switch, a corrected city), not only
        # for postings that never had a card.
        if (_map_settled(rec) and rec.get("cluster") == place["cluster"]
                and rec.get("min") == place["min"]):
            drive[key] = place["min"]
            continue
        out.append({
            "employer": job["employer"],
            "job_id": str(job["id"]),
            "drive_min": place["min"],
            "drive_mi": place["mi"],
            "cluster": place["cluster"],
            "origin_label": place["label"],
            # Encoded polyline (precision 5): the dashboard decodes it and draws the line over
            # OpenStreetMap tiles, so nothing is rendered or stored as an image.
            "geometry": place["geometry"],
        })
        drive[key] = place["min"]
        done[key] = {"at": now, "min": place["min"], "mi": place["mi"],
                     "raw": place.get("raw"), "cluster": place["cluster"]}
    return out, drive, dropped


def check_origin() -> int:
    """Re-geocode each pinned cluster point and report how far each pin has drifted."""
    if not has_map_keys():
        print("needs ORS_KEY in ~/.hermes/.env")
        return 1
    rc = 0
    for key, origin in ORIGINS.items():
        got = geocode(origin["address"])
        if not got:
            print("%-13s could not geocode %r" % (key, origin["address"]))
            rc = 1
            continue
        lat, lon = got
        dlat = (lat - origin["lat"]) * 111_320
        dlon = (lon - origin["lon"]) * 111_320 * math.cos(math.radians(origin["lat"]))
        metres = math.hypot(dlat, dlon)
        print("%-13s pinned %.5f, %.5f | geocode %.5f, %.5f | drift %.0f m  (%s)"
              % (key, origin["lat"], origin["lon"], lat, lon, metres, origin["address"]))
        if metres >= 500:
            rc = 1
    return rc


def check_maps() -> int:
    """Prove the map key and both cluster pins work, without touching the board or state."""
    if not has_map_keys():
        print("missing in ~/.hermes/.env: ORS_KEY")
        print("route cards stay off until it is set - nothing else is affected")
        return 1
    rc = 0
    print("provider         openrouteservice (geocode + directions), x%g calibration"
          % DRIVE_CALIBRATION)
    print("boundary         %d min from either cluster pin" % CLUSTER_MAX_MINUTES)
    sample = geocode("Lancaster, PA")
    print("geocode sample   Lancaster, PA -> %s" % (sample,))
    if not sample:
        print("  FAILED: cannot resolve a sample city")
        if MAP_ERROR["geocode"]:
            print("  reason: %s" % MAP_ERROR["geocode"])
        return 1
    snapped = snap_point(sample[0], sample[1])
    for key, origin in ORIGINS.items():
        route = route_summary(key, snapped[0], snapped[1])
        inside = (route["minutes"] or 10 ** 6) <= CLUSTER_MAX_MINUTES
        print("route %-13s %s min / %s mi -> %s (straight %s)"
              % (key, route["minutes"], route["miles"],
                 "in range" if inside else "outside",
                 origin["label"]))
        if not route["minutes"] or not route["geometry"]:
            print("  FAILED: no duration/geometry from %s" % key)
            if MAP_ERROR["route"]:
                print("  reason: %s" % MAP_ERROR["route"])
            rc = 1
    picked = resolve_route("Lancaster, PA", {"geo": {}, "routes": {}})
    print("resolve_route    Lancaster, PA -> %s cluster, %s min (expected York)"
          % (picked.get("cluster"), picked.get("min")))
    if picked.get("cluster") != "york" or not in_range(picked):
        print("  FAILED: Lancaster should belong to York and be inside the boundary")
        rc = 1
    print("OK" if rc == 0 else "see failures above")
    return rc


def backfill_maps(st, relevant: dict) -> int:
    """Re-resolve and re-push route cards for the whole board, without re-scraping.

    Also the switch-over path: it re-applies the 50 minute boundary and the cluster labels to
    postings that were pushed before either existed.
    """
    if not has_map_keys():
        print("needs ORS_KEY in ~/.hermes/.env - nothing to do")
        return 1
    now = now_iso()
    # Every known posting, not just the ones without a card: build_maps() decides what to push
    # by comparing against what is stored, which is how the switch to two clusters reaches
    # cards that were written when there was a single origin.
    print("maps: resolving %d known postings" % len(relevant))
    maps, _drive, dropped = build_maps(relevant, st, now)
    save_state(st)
    print("route cards: %d to push, %d dropped (%s)"
          % (len(maps), sum(dropped.values()),
             ", ".join("%s %d" % kv for kv in sorted(dropped.items())) or "none"))
    # Snapshot first: job_maps has a foreign key onto jobs, so a card for a posting the board
    # has never seen would be skipped.
    in_range_jobs = {k: v for k, v in relevant.items() if in_range(place_of(v, st))}
    print("board: %d of %d postings inside %d min of a cluster; syncing snapshot"
          % (len(in_range_jobs), len(relevant), CLUSTER_MAX_MINUTES))
    print("dashboard sync: %s" % push_snapshot(in_range_jobs, now, full=True))
    if maps:
        print("dashboard sync: %s" % push_maps(maps, now))
    return 0


def relevant_from_state(st) -> dict:
    """Rebuild the current board from state, without re-scraping.

    Only used by --backfill-maps, which needs employer/id/location/title and nothing else.
    """
    locs = st.get("locations", {})
    out = {}
    for key, v in (st.get("seen") or {}).items():
        if not v.get("url"):
            continue
        employer, _, jid = key.partition("|")
        loc = locs.get(key) or v.get("location") or ""
        if not classify(v.get("title", ""), loc, v["url"], derived=key in locs)[0]:
            continue
        out[key] = {
            "employer": employer,
            "id": jid,
            "title": v.get("title", ""),
            "location": loc,
            "url": v["url"],
            "posted": v.get("posted"),
        }
    return out


def main() -> int:
    args = sys.argv[1:]
    if "--selftest" in args:
        return selftest()

    st = load_state()
    if "--backfill-posted" in args:
        return backfill_posted(st)
    if "--backfill-location" in args:
        return backfill_location(st)
    if "--check-origin" in args:
        return check_origin()
    if "--check-maps" in args:
        return check_maps()
    if "--backfill-maps" in args:
        return backfill_maps(st, relevant_from_state(st))

    # One trigger, one scan: the daily cron runs this and every source is scraped. The lock is
    # only there to stop a hand-run from overlapping the cron.
    if not acquire_run_lock():
        if "--verbose" in args:
            print("job watch: skipping, another scan is already running")
        return 0

    found, errors = collect(st.get("source_health", {}))
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if not found and errors:
        print("⚠️ Job watch FAILED: all sources unreachable (%d errors). First: %s"
              % (len(errors), errors[0]))
        return 1

    # If a large share of sources failed, that is an infrastructure problem worth shouting about.
    total_urls = sum(
        (s["paginate"]["max"] if s.get("paginate") else len(s["urls"])) for s in SOURCES
    )
    health_warnings, health = check_source_health(found, errors, st.get("source_health", {}))
    st["source_health"] = health

    relevant, rejected = {}, {}
    verified_loc = st.get("locations", {})
    for key, job in found.items():
        # A city read off a detail page beats a source default, permanently.
        from_posting = key in verified_loc
        if from_posting:
            job["location"] = verified_loc[key]
            job["location_guessed"] = False
        ok, reason = classify(job["title"], job["location"], job.get("url", ""), derived=from_posting)
        (relevant if ok else rejected)[key] = dict(job, reason=reason)

    seen = st.get("seen", {})
    # Geography, and it is the whole of the geography rule: route each candidate city once from
    # its nearer cluster pin (results are cached per city), then keep only what is inside
    # CLUSTER_MAX_MINUTES. Anything further is dropped no matter how good the role is.
    places = {}
    for key in list(relevant):
        place = place_of(relevant[key], st)
        places[key] = place
        if not in_range(place):
            rejected[key] = dict(relevant.pop(key), reason=_place_reason(place))

    # A posting that is relevant now but was never *announced* should still alert, even if
    # we recorded it earlier (seen holds every posting, relevant or not, so a title or
    # location edit upstream could otherwise leave it permanently suppressed).
    # Jobs already marked applied / not-applicable on the dashboard are also skipped.
    suppressed = fetch_suppressed()
    new_keys = [k for k in relevant
                if not seen.get(k, {}).get("alerted")
                and not seen.get(k, {}).get("detail_rejected")
                and k not in suppressed]

    if "--dump" in args:
        print("=== RELEVANT (%d) ===" % len(relevant))
        for k, j in sorted(relevant.items(), key=lambda kv: kv[1]["title"]):
            place = places.get(k) or {}
            print("  [%s] %-50s | %-22s | %-5s %3s min | %s" % (
                j["employer"][:14], j["title"][:50], j["location"][:22],
                (place.get("label") or "?")[:5], place.get("min"),
                j.get("posted") or "?"))
        print("\n=== NEW since last run (%d) ===" % len(new_keys))
        for k in new_keys:
            print("  NEW:", relevant[k]["title"], "|", relevant[k]["employer"])
        print("\n=== REJECTED (%d) ===" % len(rejected))
        lim = len(rejected) if "--dump-all" in args else 40
        for k, j in sorted(rejected.items(), key=lambda kv: kv[1]["title"])[:lim]:
            print("  (%s) %-50s | %s" % (j["reason"][:18], j["title"][:50], j["location"][:22]))
        print("\n=== ERRORS (%d) ===" % len(errors))
        for e in errors[:10]:
            print("  ", e)

    # record everything we saw, so future runs only alert on genuinely new keys
    for key, job in found.items():
        entry = seen.get(key) or {"first_seen": now}
        entry.update({"title": job["title"], "employer": job["employer"],
                      "location": job["location"], "url": job["url"], "last_seen": now})
        if job.get("location_guessed"):
            entry["location_guessed"] = True
        else:
            entry.pop("location_guessed", None)
        if job.get("posted"):
            entry["posted"] = job["posted"]
        seen[key] = entry
    st["seen"] = seen
    st["runs"] = st.get("runs", 0) + 1
    st["last_run"] = now
    st["last_counts"] = {"found": len(found), "relevant": len(relevant),
                         "rejected": len(rejected), "new": len(new_keys),
                         "errors": len(errors)}

    # Detail screen: only for NEW matches (a handful per week), so this is cheap. The same
    # fetch doubles as the only chance to read a posted date for sources whose results pages
    # omit one, and it is where a guessed city gets corrected - a multi-site system's cards
    # carry no city, so "York, PA" is the source default and Chambersburg would slip through.
    # Runs BEFORE the dashboard push so a wrong location never reaches the board.
    verified, dropped = [], []
    for k in sorted(new_keys):
        job = relevant[k]
        if len(verified) >= 8:
            verified.append(k)
            continue
        try:
            body, found_posted = posted_for(job["url"])
        except Exception:
            body, found_posted = "", None
        if not job.get("posted") and found_posted:
            job["posted"] = found_posted
            st["seen"].setdefault(k, {})["posted"] = found_posted
        if job.get("location_guessed"):
            real = location_from_body(body)
            if real:
                job["location"] = real
                job["location_guessed"] = False
                # Persist, or the next run re-derives the guess from the card and overwrites it.
                st.setdefault("locations", {})[k] = real
                ok, why = classify(job["title"], real, job.get("url", ""), derived=True)
                if ok:
                    # The real city can be a county away from the guessed one, so the drive
                    # decision has to be made again on it.
                    place = place_of(job, st)
                    places[k] = place
                    if not in_range(place):
                        ok, why = False, _place_reason(place)
                if not ok:
                    dropped.append((k, "real location %s (%s)" % (real, why)))
                    st["seen"].setdefault(k, {})["location_rejected"] = real
                    relevant.pop(k, None)
                    continue
        bad, phrase = detail_reject(body)
        if bad:
            dropped.append((k, phrase))
            st["seen"].setdefault(k, {})["detail_rejected"] = phrase
            relevant.pop(k, None)
        else:
            verified.append(k)
    new_keys = verified
    save_state(st)

    # Route cards for the whole board, not just the new matches: places are already resolved
    # (and cached per city), so this pushes a card only where the answer actually changed -
    # which is exactly what the switch to two clusters needs on the first run.
    maps, drive_min, _map_dropped = build_maps(relevant, st, now)
    save_state(st)

    baseline = (not st.get("baseline_done")) or ("--baseline" in args)
    if baseline:
        # A baseline means "everything currently live is already known" - mark the
        # relevant ones as announced so the next run does not flood her with them.
        for k in relevant:
            seen[k]["alerted"] = True
            seen[k]["alerted_at"] = now
        st["seen"] = seen
        st["baseline_done"] = True
        st["baseline_at"] = now
        st["web_sync"] = push_snapshot(relevant, now, full=False)
        save_state(st)
        if "--baseline" in args:
            print("baseline recorded: %d postings (%d relevant) at %s"
                  % (len(found), len(relevant), now))
            print("dashboard sync: %s" % st["web_sync"])
            for w in health_warnings:
                print("  ⚠️", w)
        return 0

    # `full` only when every source answered AND every city routed: a partial scrape or a
    # provider outage must not retire the board (an unresolved place looks out of range).
    place_errors = [k for k, p in places.items() if p.get("error")]
    # A source that returned nothing it usually returns is broken, not empty. Retiring the board
    # on that run would drop real postings - PHMC's switch to a web-component board looked
    # exactly like "no new jobs" and would have retired ten live rows.
    broken = [w for w in health_warnings
              if "returned 0 postings" in w or "possible partial breakage" in w]
    st["web_sync"] = push_snapshot(
        relevant, now, full=(not errors and not place_errors and not broken))
    if place_errors:
        print("⚠️ %d city/cities could not be routed - nothing retired this run (%s)"
              % (len(place_errors), places[place_errors[0]].get("error", "")[:60]))
    if broken:
        print("⚠️ %d source(s) look broken - nothing retired this run (%s)"
              % (len(broken), broken[0][:80]))
    if maps:
        # After the job rows exist: job_maps has a foreign key onto jobs.
        st["web_sync_maps"] = push_maps(maps, now)
    save_state(st)

    if not new_keys:
        if dropped:
            print("job watch: %d new match(es) screened out (%s)"
                  % (len(dropped), dropped[0][1]))
        if health_warnings:
            print("⚠️ Job watch problem%s\n" % ("" if len(health_warnings) == 1 else "s"))
            for w in health_warnings:
                print("• %s" % w)
        if str(st.get("web_sync", "")).startswith("FAILED"):
            print("⚠️ Dashboard sync %s" % st["web_sync"])
        _write_run_log(now, [], errors, health_warnings, len(found), len(relevant))
        return 0

    lines = ["🔔 %d new job match%s\n" % (len(new_keys), "" if len(new_keys) == 1 else "es")]
    for i, k in enumerate(sorted(new_keys, key=lambda x: relevant[x]["employer"])[:10], 1):
        j = relevant[k]
        lines.append("%d. %s\n   🏥 %s" % (i, j["title"], j["employer"]))
        if j["location"]:
            lines.append("   📍 %s" % j["location"])
        if drive_min.get(k):
            place = places.get(k) or {}
            lines.append("   🚗 %d min from %s" % (drive_min[k], place.get("label") or "?"))
        lines.append("   🔗 %s\n" % j["url"])
    if len(new_keys) > 10:
        lines.append("…and %d more (run with --dump for the full list)" % (len(new_keys) - 10))
    if health_warnings:
        lines.append("⚠️ Source problem%s this run:" % ("" if len(health_warnings) == 1 else "s"))
        for w in health_warnings:
            lines.append("• %s" % w)
    print("\n".join(lines))

    _write_run_log(now, new_keys, errors, health_warnings, len(found), len(relevant))
    for k in new_keys:
        st["seen"][k]["alerted"] = True
        st["seen"][k]["alerted_at"] = now
    save_state(st)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print("⚠️ Job watch crashed: %s" % str(e)[:200])
        sys.exit(1)

