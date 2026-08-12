# AGENTS.md — RTJobs

## What this is
Headful job-board scraper. Scrapes LinkedIn (login-gated) and Wuzzuf
(Cloudflare-gated) via [scrapling](https://scrapling.readthedocs.io) stealth
browser sessions, persists jobs to SQLite, posts new jobs to one Telegram
channel and failure alerts to another. Scheduled in Docker via ofelia
(every 6 min). Chrome runs headful under Xvfb with CDP on port 9222 for
live debugging / manual 2FA solves.

## Commands
```bash
source .venv/bin/activate.fish      # shell is fish; venv is Python 3.14
python main.py                      # run all enabled boards
python main.py --reset-login        # clear LinkedIn retry/cooldown state
python -m py_compile <files...>     # no linter/typechecker configured — compile check + offline tests are the verification loop
docker compose up -d --build        # scheduled container run (ofelia)
docker compose logs -f scraper
```
There is NO test framework. Verification = ad-hoc offline scripts that run
extraction/parsing functions against the fixture files in `markup/` (see
"Offline testing" below).

## Architecture
```
main.py                  orchestrator; BOARDS list; --reset-login
config.py                ALL env config (single source of truth)
core/db.py               SQLite: jobs, seen_ids, runs, login_state
core/telegram.py         notify_jobs (jobs channel) / notify_failure (alert channel)
core/markup.py           sanitized HTML snapshots -> markup/<site>/snapshots/<kind>/
core/login_state.py      LinkedIn retry counter + escalating cooldown (5m/15m/30m)
core/browser.py          patch_no_load_wait — see Gotchas #1
core/human.py            random human-like delays
boards/base.py           JobBoard ABC + load_board_selectors()
boards/linkedin/         login.py (state machine) + scraper.py (Spider)
boards/wuzzuf/           scraper.py (Spider, solve_cloudflare=True)
boards/indeed/           scraper.py (Spider, solve_cloudflare=True) — see INDEED.md
markup/<site>/selectors.json   ALL CSS selectors live here, never in code
```
Job dict shape everywhere: `source, external_id, title, company, posted_at,
description, link, extra(dict), scraped_at`.

## Gotchas (hard-won — read before touching browser code)
1. **scrapling waits for the browser `load` event** on every navigation
   (`page.goto(wait_until="load")` default + `_wait_for_page_stability`).
   LinkedIn/Wuzzuf never fire `load` (hanging trackers) → every fetch times
   out. Fix: `core/browser.py:patch_no_load_wait` is passed as `page_setup`
   to every session. CONTRACT: scrapling's **async** sessions do
   `await params.page_setup(page)` and `await page.goto(...)` — the patch
   detects async pages via `inspect.iscoroutinefunction(page.goto)` and must
   return a coroutine + install `async def` wrappers. Sync sessions get sync
   wrappers and `None`. Verified in
   `.venv/lib/python3.14/site-packages/scrapling/engines/_browsers/_stealth.py`.
2. **Chrome lifecycle + CDP attach**: WE launch Chrome
   (`core/browser.py:launch_cdp_chrome`) with `--remote-debugging-port` —
   never let scrapling launch it: playwright forces
   `--remote-debugging-pipe`, which DISABLES the HTTP DevTools endpoint
   (localhost:9222 would serve nothing). Boards pass `cdp_url=` to every
   session, and `install_cdp_default_context_patch()` replaces the cdp
   branch of scrapling's `start()` to reuse `browser.contexts[0]` —
   scrapling's own path calls `new_context()`, which is isolated from the
   profile's cookies (would silently drop the LinkedIn session / Wuzzuf
   cf_clearance every run). CAREFUL: after a `new_context()` call the
   contexts list is reordered and index 0 becomes the ISOLATED one — grab
   contexts[0] straight after `connect_over_cdp`. The container needs
   `network_mode: host` because Chrome binds CDP to loopback and
   docker-proxy can't forward to a container-loopback listener.
3. **Wuzzuf data comes from the SSR blob**, not just the DOM:
   `window.Wuzzuf.initialStoreState.job.collection` (full entities: HTML
   description/requirements, exact `postedAt` `MM/DD/YYYY HH:MM:SS`,
   salary, career level…). It is parsed from `page.content()` with marker
   regex + brace balancing (`_extract_state`) — NOT `page.evaluate`
   (evaluate silently failed under stealth isolated contexts).
4. **Wuzzuf pagination** is a page index: `?q=&start=0`, `start=1`, …
   (15 jobs/page). Stop condition: first `external_id` already in
   `seen_ids` (default sort is by date). Job id = numeric prefix of the
   slug: `/jobs/p/<id>-<slug>`.
5. **LinkedIn login**: fetch `https://www.linkedin.com/login`, fill with
   human-like typing (EN+AR aware), then `_verify_routing` waits
   event-based via `page.wait_for_url` for `(feed|/jobs|checkpoint|security_verification)`
   — do NOT put `login` in that pattern (current URL matches it instantly).
   Logged-in detection (`_FEED_OK`) is `/feed` ONLY — guests get redirected
   to `/jobs` too, so matching `/jobs` treats a guest session as logged in
   (this bug shipped once). Checkpoint/2FA → alert + pause up to
   `CHECKPOINT_WAIT_SECONDS` for a manual solve over CDP. Profile wipe
   (after max retries + cooldown) must happen BEFORE the browser starts
   (`LinkedInBoard.run`), never inside a page_action of a live Chrome. Same
   for `kill_zombie_chrome()` — inside a page_action `pkill -f chrome`
   kills the running browser itself (shipped once).
6. **Container Chrome startup chain** (each one shipped as a bug):
   a. `xvfb-run` needs the `xauth` package (not part of `xvfb`).
   b. `xvfb-run` hangs forever when it is PID 1 (SIGUSR1 readiness
      handshake) → compose needs `init: true` (tini).
   c. Chrome core-dumps (SIGTRAP) at startup if the user has no writable
      HOME — `useradd -r` doesn't create one; Dockerfile sets
      `HOME=/home/scraper`.
   d. After a killed Chrome the profile keeps `Singleton*` lock files →
      next launch fails with "profile appears to be in use";
      `kill_zombie_chrome` removes them (container mode only).
   e. Container TZ defaults to UTC → posted_at/scraped_at off by 3h;
      compose pins `TZ=Africa/Cairo`.
7. **ofelia**: `latest` is the 0.3.x line — cron strings NEED the leading
   seconds field (`"0 */6 * * * *"`). `job-run` labels must be on the
   **ofelia** container itself (target-container labels only work for
   `job-exec`); with `container: rtjobs` and no image it does
   `docker start` on the exited one-shot container (same volumes/env), so
   keep `delete` false. Logs of scheduled runs go to `docker logs ofelia`.
8. **`markup.py` sanitizer**: skip-tags must not count depth for void
   elements (`meta`, `link`, …) or the first `<meta>` swallows the whole
   document (this bug shipped once and corrupted all snapshots).
9. Telegram messages are MarkdownV2 — all dynamic text through
   `escape_md`; a 400 response triggers one plain-text retry (and the
   retry must OMIT `parse_mode` from the payload, not send it as null).
10. `.env` must NEVER be committed (it was once — the old remote was
    replaced with a single fresh root commit; treat secrets as rotated).
    It is gitignored; untracked.

## Env vars (see config.py / README for defaults)
Required: `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`, `TELEGRAM_TEST_ID`
(failure channel; override: `TELEGRAM_FAILURE_CHAT_ID`),
`LINKEDIN_EMAIL`, `LINKEDIN_PASSWORD`.
Notable: `LINKEDIN_ENABLED` (currently `True` in `.env`), `INDEED_ENABLED`
(now `True` in `.env`), `HEADLESS`,
`DATA_DIR`, `MARKUP_DIR`, `CHROME_DEBUG_PORT=9222`,
`CHECKPOINT_WAIT_SECONDS`, `MAX_LOGIN_RETRIES`, `WUZZUF_SEARCH_URL`,
`*_PROFILE_DIR`, `TZ` (compose: `${TZ:-Africa/Cairo}`).

## Current state / how things were last verified
- Docker hosting verified end-to-end on this machine: ofelia fires every
  6 min → one-shot `rtjobs` container → LinkedIn (logged-in session in the
  `chrome_profile` volume) + Wuzzuf scrape → SQLite + Telegram.
- CDP live attach verified: while a run is in progress,
  `curl http://localhost:9222/json/version` on the host returns Chrome's
  DevTools info (open localhost:9222 / chrome://inspect to drive it —
  this is how checkpoints/2FA get solved manually). Endpoint is only up
  while a run is active.
- Wuzzuf: 15 jobs/page, enriched from SSR state, deduped, saved, notified.
- Indeed: single sort=date search page (~15 jobs, no pagination — login-gated),
  JSON blobs only (no CSS selectors): `window.mosaic.providerData["mosaic-provider-jobcards"]`
  for cards + follow-up `/viewjob?jk=` fetch per NEW jobkey for the description
  (`window._initialData` -> `hostQueryExecutionResult.data.jobData.results[0].job` —
  NOTE the search page's two-pane blob uses the `autoOpenTwoPaneViewjobResponse.body.`
  prefix instead; ld+json is the fallback). `pubDate` is normalized to midnight —
  always prefer `createDate`. Verified live: CF solved via persistent profile,
  detail cap `_MAX_DETAIL_FETCHES=10`/run (snippet placeholder when skipped).
- LinkedIn: logged-in scraping verified (pages of 25, detail panels,
  dedupe against `seen_ids`); `posted_at` matches host local time.
- Docker: python:3.13-slim + real Chrome + xvfb-run, `init: true`,
  `network_mode: host` on the scraper; volumes `chrome_profile`,
  `wuzzuf_profile`, `scraper_data`; `./markup` bind-mounted to
  `/data/markup`.

## Offline testing (do this after ANY parsing/selector change)
```python
# fixtures: markup/wuzzuf/wazzuf_guide.txt (curl capture containing the SSR blob),
#           markup/linkedin/manual_login_{en,ar}.html (sanitized login pages)
raw = open("markup/wuzzuf/wazzuf_guide.txt", encoding="utf-8").read()
html = raw[raw.find("<!DOCTYPE"):]          # guide starts with curl noise
from boards.wuzzuf.scraper import _extract_state, _extract_jobs
from boards.base import load_board_selectors
entities = _extract_state(html)              # expect 15
jobs, dup = _extract_jobs(html, load_board_selectors("wuzzuf"), set(), entities)
# expect: 15 jobs, non-empty description, extra.career_level, posted_at like '2026-08-09 16:48'
```

```python
# fixtures: markup/indeed/first_page.html (search page capture),
#           markup/indeed/newjob_sample.html (one viewjob page capture)
from boards.indeed.scraper import _extract_jobs, _extract_detail
search = open("markup/indeed/first_page.html", encoding="utf-8").read()
jobs, seen, missing = _extract_jobs(search, set())
# expect: 15 jobs, missing=False, posted_at from createDate (pubDate is midnight-normalized)
view = open("markup/indeed/newjob_sample.html", encoding="utf-8").read()
desc, extra = _extract_detail(view)
# expect: non-empty desc, extra['latitude']/['longitude']
```
Set dummy env before importing config in test scripts:
`TELEGRAM_TOKEN=x TELEGRAM_CHAT_ID=1 TELEGRAM_TEST_ID=2 DATA_DIR=<tmp> MARKUP_DIR=<repo>/markup`.

## Conventions
- Selectors: ALWAYS in `markup/<site>/selectors.json`, loaded via
  `load_board_selectors(site)`; missing file = fail loudly at startup.
- Company blocklist: `markup/blocked_companies.json` (bind-mounted, so
  live-editable without rebuild). Keyed by source + `"*"` for all sources;
  case-insensitive substring match (`core/blocklist.py`). Blocked jobs are
  `db.mark_seen`'d (never re-scraped) but never saved/notified.
- Snapshots via `markup.save_snapshot(site, kind, html)` on every
  suspicious page (login failure, checkpoint, empty results, redirects).
- Failures -> `telegram.notify_failure(subject, detail, snapshot, hint)`;
  never crash silently.
- Spiders: scrapling `Spider` subclass, `configure_sessions` + `manager.add`
  + `sid=` routing, per-request `page_action` for in-page work, `parse`
  yields job dicts and follow-up `Request`s.
- Docstrings/comments are used throughout — keep that style when editing.
- DB timestamps are local time strings `YYYY-MM-DD HH:MM(:SS)`; snapshot
  filenames are UTC. Don't mix formats.
