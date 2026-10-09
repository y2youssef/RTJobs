# NaukriGulf board — recon findings (2026-10-08/09, no code written)

Target: `https://www.naukrigulf.com/jobs-in-egypt?freshness=1&xz=1_3_5`
Scope of this doc: what is known before implementation, and what must still
be captured. Nothing here is implemented yet.

## 1. Bot protection: curl-hostile, assume bot-managed

Probed read-only from the dev machine on 2026-10-08:

| Probe | Result |
|---|---|
| `GET` with full Chrome 151 UA over HTTP/2 | **Stream reset** (`HTTP/2 INTERNAL_ERROR`, 0 bytes, 0.7s) |
| `HEAD` with Chrome UA over HTTP/1.1 | **Timeout**, 0 bytes in 30s (connection held open) |
| `GET /robots.txt` | Empty (blocked or silently dropped) |
| Edge IP `23.46.189.71` | Akamai range |

Interpretation (inference, not proven): TLS/HTTP-fingerprint filtering with
tarpitting — the classic Akamai Bot Manager posture. `curl` and any plain
HTTP client are therefore **not viable** for this board, same lesson as
Wuzzuf's Cloudflare. Consequences:

- The board must run through the headful stealth Chrome over CDP, with
  `solve_cloudflare=True`-equivalent handling if a challenge appears, and a
  **persistent profile volume** (`naukrigulf_profile`) so clearance/session
  cookies survive across runs — exactly the Wuzzuf/Indeed model.
- First live run from a new IP (and especially a datacenter IP) may face a
  challenge page; plan one manual CDP solve, then the session persists.
- Do NOT attempt direct API calls from workers/enrichment — same rule as
  other boards: all fetching happens inside the browser's lifetime.

## 2. Page data model (operator intel, unverified in code)

Per operator observation of DevTools: **full job descriptions arrive in
network-tab payloads**, so no card-clicking/detail-panel hydration is needed
(unlike LinkedIn's per-card detail panels, and unlike Indeed's per-jobkey
`/viewjob` follow-ups).

Implication for spider design: one search-page fetch yields everything —
parse the XHR JSON (preferred: structured, no DOM fragility) or fall back to
an embedded SSR blob / DOM. No second navigation per job. This should make
NaukriGulf the cheapest board per cycle after Wuzzuf.

## 3. Search API captured (independent browser session, 2026-10-09)

Reproduced with a separate automation-driven browser (production Chrome
untouched). The search XHR is:

```http
GET /spapi/jobapi/search?Experience=&Freshness=1&Keywords=&KeywordsAr=
  &Limit=30&Location=egypt&LocationAr=&Offset=0&SortPreference=
  &breadcrumb=1&clusterSelected=1&geoIpCityName=Tanta&geoIpCountryName=Egypt
  &locationId=&nationality=&nationalityLabel=&pageNo=1&seo=1
  &showBellyFilters=true&showSponsoredJobs=true&srchId=&topEmployer=true
  &xz=1_3_5
Accept: application/json
```

- **Pagination: `Limit=30` + `Offset=0` + `pageNo=1`** — 30 jobs/page,
  offset-based. First-page-only default fits naturally.
- `Freshness=1` + `Location=egypt` mirror the page URL params; `xz=1_3_5`
  is passed through (experience buckets — exact mapping unverified).
- Custom headers required: `appid: 205`, `systemid: 2323`,
  `clientid/client-type/device-type: desktop`, `version: v1`,
  `accept-format: strict`, plus page `Referer`. The page JS self-reports
  `puppeteer: false` (header and cookie) — the edge verifies this
  independently (see §4).
- Geo params (`geoIpCityName/Tanta`, `aka_location=Country=EG`) derive from
  the egress IP; explicit `Location=egypt` should dominate, but verify from
  a non-EG IP (e.g. the future Oracle region) before assuming.
- `isJsLoggedIn=false`: search is public, **no login needed** (confirmed).

## 4. Protection: Akamai Bot Manager, escalating soft-block (observed)

Sequence in the automation-driven browser (fresh profile, no stealth):

1. Document `GET` → **200**, shell renders.
2. Search/dropdown/analytics XHRs → **`net::ERR_HTTP2_PROTOCOL_ERROR`**
   (stream reset); page shows *"Oops! Something went wrong"*.
3. Reload → document itself **reset** (`ERR_HTTP2_PROTOCOL_ERROR`).

Same H2-reset signature as the `curl` probes in §1 — but note the operator's
real browser receives the descriptions fine. Corroborating signals:

- `POST /akam/13/pixel_*` → 200: the **Akamai BM sensor beacon** runs and
  the edge issued `ak_bmsc` / `bm_sv` cookies — behavioral fingerprinting
  is engaged, not just a static challenge.
- `ERR_NETWORK_CHANGED` appeared once mid-load, so a transient network
  flap cannot be excluded as a contributor; the consistent cross-client
  pattern (curl + automation browser, 5+ resets) still points at
  fingerprint-based filtering first.

Consequences (strengthen §1): the shell is served to collect sensor data,
then API/stream access is withdrawn — the standard BM soft-block. The
production stealth stack (real Chrome binary, persistent profile with
clearance, `webdriver=false`, human pacing — the Wuzzuf model) is the
tool built for exactly this; a successful sensor history in the profile
is likely what separates the operator's working browser from a fresh one.
Expect the first live run to need observation and possibly one manual
session before the profile is trusted.

## 4b. Root cause of the automation failures (isolated 2026-10-09)

Same host and same egress IP as the operator's working browser, so IP
reputation is ruled out — the difference is purely client-side:

- **`navigator.webdriver=true`** in the automation-driven sessions (real
  browsers report `false`). This is the primary tell Akamai's sensor
  reads. Everything else looked normal (plugins 5, mimeTypes 2, real
  screen/TZ/CPU values, UA Chrome/155 Linux).
- **Fresh profile, no trust history**: the edge issued sensor cookies
  (`ak_bmsc`, `bm_sv`) but never the `_abck` validation cookie — the
  sensor reported, the verdict was "automation", trust was never granted.
- **Escalation observed live**: document 200 (shell must load so the
  sensor JS runs) → search/dropdown/analytics XHRs reset → reload resets
  even the document. The edge caches the verdict per client.
- The page's own `puppeteer:false` self-report (header + cookie) disagrees
  with the sensor verdict — that mismatch can only hurt.

Why the production stack should pass where these sessions failed: real
Chrome binary (not CDP-driven Chromium defaults), a persistent profile
that accumulates successful sensor history across runs, stealth patches
(`webdriver=false`, the `patch_no_load_wait`/context discipline in
`core/browser.py`), headed rendering under Xvfb, and human pacing —
i.e. every property the flagged sessions lacked.

Operational caution: repeated failing visits from one flagged client can
spill onto shared egress-IP reputation and break the operator's working
browser too. Recon probing from automation is now STOPPED for this site;
further contact should be the production stealth stack (or the operator's
own browser) only.

## 5. Still unknown (must be captured before implementation)
2. **Pagination scheme** (page index? cursor? `start=`?) and total-result
   counts — decides first-page-only shape (repo default) and any stop rule.
3. **Job identity rule**: job URL shape and which component is the stable
   external ID (cf. Wuzzuf's first-slug-component rule).
4. **Posted-date format** in the payload (absolute? relative? timezone?) —
   decides `posted_at` parsing; `posted_at` stays local wall time per clock
   convention, everything operational goes UTC via `core/clock.py`.
5. **Description encoding** (HTML? plaintext? truncated with "read more"?),
   and which fields exist (company, location, salary, experience, role).
6. **Rate limiting / challenge behavior** on repeat hits at 3-min cadence.
7. No login is expected (public site) — confirm no auth redirect like
   Indeed's bot-detection login.

## 6. Implementation checklist (repo conventions, for the build phase)

- `boards/naukrigulf/`: `scraper.py` (`NaukriGulfJobSpider(Spider)`:
  `configure_sessions` + `manager.add` + `sid=` routing, per-request
  `page_action`, `parse` yields job dicts) and `__init__.py`
  (`NaukriGulfBoard(JobBoard)`: `name/title/enabled/profile_dir`,
  `scrape(cdp, record)`; `before_browser()` only if pre-Chrome checks exist).
- Selectors (if DOM is used at all) in `markup/naukrigulf/selectors.json`
  via `load_board_selectors()` — never in code. If the board is JSON-only
  like Indeed, keep the file as `{}` for parity plus a comment.
- Job dict shape: `source="naukrigulf"`, `external_id, title, company,
  posted_at, description, link, extra(dict), scraped_at`.
- `ScrapeHealth("naukrigulf")`: checks for fetch, payload presence, card
  identity, job fields; `health.report()` once; snapshots on suspicious
  pages via `core/markup.py`. New check names must not collide with the
  per-source alert dedupe.
- `persist_jobs()` from `boards.base` (blocklist + atomic save + queue).
- `config.py`: `NAUKRIGULF_ENABLED` (default false until verified live),
  `NAUKRIGULF_SEARCH_URL`, `NAUKRIGULF_PROFILE_DIR`, wired through
  `.env.example` (blank, never secrets).
- `main.py`: append to `BOARDS`. `core/pipeline_monitor.py`: add to the
  board tuple (it is currently hardcoded to three boards).
- `docker-compose.yaml`: `naukrigulf_profile` volume on the scraper
  service (same pattern as the other two profiles).
- Offline fixtures in `markup/naukrigulf/` (sanitized: public job fields
  only, never authenticated captures) + coverage in
  `scripts/verify_offline.py` (extraction, dedupe, broken-markup alerts).
- `LATENCY` stages via `core/timing.py` (`search_page`, `persist_jobs`;
  per-detail stages only if detail fetches exist — expected: none).
- AGENTS.md: one Gotchas entry for whatever is actually hard-won here
  (protection behavior, identity rule, date format).
- Dockerfile: no change expected (system Chrome + Xvfb already present).

## 7. Suggested next step (15 min, no code)


Capture ONE successful search response (real browser with trusted sensor
history, or the stealth stack once built): response shape — one sample job
— plus the posted-date format, then save a sanitized fixture to
`markup/naukrigulf/`. That unblocks the full implementation in one pass.
(The automation-driven session above never got a 200 on the API, so the
response schema is the one remaining capture item.)

## 8. Effort / risk estimate

- Likely ~1 focused session: Wuzzuf is the closest template (gated site,
  blob/JSON parsing, profile persistence); subtract LinkedIn's login and
  Indeed's detail-fetch complexity.
- Main risks: (a) protection stricter than Wuzzuf's CF (unknown until the
  first live run); (b) XHR endpoint session-bound (would force DOM/blob
  parsing instead); (c) 3-min cadence rate limits (unproven — watch the
  first hour live).
