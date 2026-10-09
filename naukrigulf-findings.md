# NaukriGulf board — recon findings (2026-10-08, no code written)

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

## 3. Still unknown (must be captured before implementation)

1. **The exact XHR endpoint + query params** (open the target URL in a CDP
   session, filter Fetch/XHR, copy the search request as cURL — redact
   cookies). Is it stable across runs, or tokenized per session?
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

## 4. Implementation checklist (repo conventions, for the build phase)

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

## 5. Suggested next step (15 min, no code)

One manual CDP session against the target URL: record the XHR search
request (URL, params, response shape — one sample job), note pagination
and date format, save a sanitized fixture to `markup/naukrigulf/`. That
unblocks the full implementation in one pass.

## 6. Effort / risk estimate

- Likely ~1 focused session: Wuzzuf is the closest template (gated site,
  blob/JSON parsing, profile persistence); subtract LinkedIn's login and
  Indeed's detail-fetch complexity.
- Main risks: (a) protection stricter than Wuzzuf's CF (unknown until the
  first live run); (b) XHR endpoint session-bound (would force DOM/blob
  parsing instead); (c) 3-min cadence rate limits (unproven — watch the
  first hour live).
