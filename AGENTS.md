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
core/db.py               SQLite: raw jobs, dedupe, runs, enrichment/cache/spend, scrape_health
core/scrape_health.py    parser checks + persistent error-channel alert dedupe
core/enrichment_worker.py whole-cycle OpenRouter batches (default disabled)
core/delivery_worker.py   independent classified Telegram delivery
core/pipeline_monitor.py  local heartbeat/queue/budget alerts
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
   regex + JSONDecoder.raw_decode (`_extract_state`) — NOT `page.evaluate`
   (evaluate silently failed under stealth isolated contexts).
4. **Wuzzuf pagination** is a page index: `?q=&start=0`, `start=1`, …
   (15 jobs/page). Stop condition: first `external_id` already in
   `seen_ids` (default sort is by date). Job id = first hyphen-separated component of the
   slug: `/jobs/p/<id>-<slug>`.
5. **LinkedIn login**: `page_action` runs right after DOMContentLoaded, BEFORE
   an active session's `/login -> /feed` redirect lands (scrapling's `wait`
   only starts after page_action) — `_wait_for_landing` must run before judging
   the URL, and every failure path re-checks `/feed` (`_fail`); all saved
   login_failure snapshots up to Oct 2026 were the Feed page. `wait_for_url`
   needs `wait_until="commit"` (default waits for `load`). A visible sign-in
   error locks logins until the credentials change (salted fingerprint in
   login_state) or `--reset-login`; checkpoints never wipe the profile; other
   failures get at most one wipe per streak (core/login_state.py).
   Fetch `https://www.linkedin.com/login`, fill with
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
(now `True` in `.env` — re-enabled Oct 2026 with Telegram-assisted login:
anonymous sessions 403-redirect to `/account/login?...&from=bot-detection-anonymous`,
so the board signs in by email code — run drives the form, user replies/DMs
the 6-digit code to the bot, getUpdates long-poll consumes it; session
persists in the `indeed_profile` volume, re-login only on expiry with a
30-min cooldown. See INDEED.md "Login"), `HEADLESS`,
`DATA_DIR`, `MARKUP_DIR`, `CHROME_DEBUG_PORT=9222`,
`CHECKPOINT_WAIT_SECONDS`, `MAX_LOGIN_RETRIES`, `WUZZUF_SEARCH_URL`,
`*_PROFILE_DIR`, `TZ` (compose: `${TZ:-Africa/Cairo}`).

## Current state / how things were last verified
- Runtime pins updated 2026-10-04: Scrapling 0.4.15, Playwright/Patchright 1.63.0.
  Scrapling now reuses tabs: keep page setup idempotent and diagnostics bounded.
  Sync/async CDP default-context cookies and live samples from all three boards
  passed upgrade checks. Upstream caps repeated Cloudflare solve attempts at
  three; this is not an overall deadline for every wait in the solver.
- Docker hosting verified end-to-end on this machine: ofelia fires every
  6 min → one-shot `rtjobs` container → LinkedIn (logged-in session in the
  `chrome_profile` volume) + Wuzzuf scrape → SQLite + Telegram.
- Job-family classification and delivery enabled 2026-10-04 at 16:42 Cairo.
  First production batch: 13/13 new jobs classified and delivered to 11 families;
  all three boards completed `ok`. Worker service is `enrichment`, separate from
  Chrome; both enable flags are true in private `.env`. See docs/DEPLOYMENT.md.
- CDP live attach verified: while a run is in progress,
  `curl http://localhost:9222/json/version` on the host returns Chrome's
  DevTools info (open localhost:9222 / chrome://inspect to drive it —
  this is how checkpoints/2FA get solved manually). Endpoint is only up
  while a run is active.
- Wuzzuf: 15 jobs/page, enriched from SSR state, deduped, saved, notified.
- Indeed: single sort=date search page (~15 jobs, no pagination — login-gated),
  JSON blobs only (no CSS selectors): `window.mosaic.providerData["mosaic-provider-jobcards"]`
  for cards + follow-up `/viewjob?jk=` fetch per NEW jobkey for the description
  (`window._rootProps.preloadedVJData` on current standalone pages; older
  `window._initialData` -> `hostQueryExecutionResult.data.jobData.results[0].job` —
  NOTE the search page's two-pane blob uses the `autoOpenTwoPaneViewjobResponse.body.`
  prefix instead; ld+json is the fallback). `pubDate` is normalized to midnight —
  always prefer `createDate`. Verified live: logged-in scraping works (email-code
  login via Telegram assist, session in `indeed_profile` volume), CF solved via
  profile clearance, detail cap `_MAX_DETAIL_FETCHES=10`/run (snippet placeholder
  when skipped). Login flow details in INDEED.md "Login" (esp. the `;jsessionid`
  400 trap and the post-submit OAuth wait).
- LinkedIn: logged-in scraping verified (pages of 25, detail panels,
  dedupe against `seen_ids`); `posted_at` matches host local time.
- Docker: python:3.13-slim + real Chrome + xvfb-run, `init: true`,
  `network_mode: host` on the scraper; volumes `chrome_profile`,
  `wuzzuf_profile`, `scraper_data`; `./markup` bind-mounted to
  `/data/markup`.

## Offline testing (do this after ANY parsing/selector change)
Run `.venv/bin/python scripts/verify_offline.py` for the complete offline checks.
It uses temporary SQLite files, dummy credentials and mocked HTTP; intentional
broken-markup cases must alert for all three boards without sending real messages.

```python
# fixtures: markup/wuzzuf/wazzuf_guide.txt (public card DOM and SSR fields),
#           markup/linkedin/manual_login_{en,ar}.html (sanitized login pages)
raw = open("markup/wuzzuf/wazzuf_guide.txt", encoding="utf-8").read()
html = raw[raw.find("<!DOCTYPE"):]
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
- DB timestamps are UTC strings `YYYY-MM-DD HH:MM:SS` via `core/clock.py`
  (`clock.now_str/after/age_seconds`; `to_local` for anything shown to the
  user). The ONE exception is `jobs.posted_at`: local wall time, because it is
  displayed and analytics bucket it by local hour. Never use `datetime.now()`
  for a stored/compared timestamp: Cairo DST repeats an hour each October.
  Existing local rows were converted once (`PRAGMA user_version` 1).

## Enrichment and parser alerts (2026-10-03)
- Save raw data first with `boards.base.persist_jobs`; queued AI work is a row in
  `job_enrichments` in the same SQLite database. Never overwrite raw descriptions
  with AI output. Schema migration does not enqueue historical jobs.
- `ENRICHMENT_ENABLED=false` preserves single-channel delivery in `main.py` after
  Chrome closes. The worker owns classified delivery; both `ENRICHMENT_ENABLED` and the separate
  `CLASSIFIED_DELIVERY_ENABLED` flag must be true before posting classified jobs.
- Model/key: `CLASSIFIER_MODEL=openai/gpt-6-luna`; `OPENROUTER_API_KEY` accepts
  the existing `OPENROUTER_API` alias. Prompt/schema/taxonomy live in
  `markup/enrichment/`; channel IDs live only in `TELEGRAM_CHANNELS_JSON` in `.env`.
  API endpoints are configured by `OPENROUTER_BASE_URL` and `TELEGRAM_API_BASE_URL`.
  The job_family_v2 specification in EXPAND supersedes all old
  department/industry/Gulf/DeepSeek routing plans. No keyword-only classifier bypasses extraction.
- Cache keys include full relevant source content, model and prompt/schema
  version. Input caching is provider-managed and measured via usage details.
  Never cache fallback as successful classification. Reserve spend before calls.
- All spiders use `ScrapeHealth`: record checks during parsing, report once after
  the spider finishes. Errors alert TELEGRAM_FAILURE_CHAT_ID (TELEGRAM_TEST_ID
  alias). Repeated unresolved issues are suppressed across restarts; failed
  alert delivery is retried next observation. A later good card cannot erase an
  earlier failure in the same run. An unobserved check is not a recovery.
- Boards pass their `ScrapeHealth` instance into `scrape` and finish the run with
  `health.status`/`health.error`. Failed parser checks mean `degraded`, not `ok`,
  while valid partial jobs are still saved. LinkedIn's captured loading shell
  (`search.loading_shell` selector) is a loading failure, not proof of redesign.
- LinkedIn has bounded search/detail recovery and pre-navigation diagnostics.
  Keep `page_setup` async/awaitable. Do not retry login/checkpoint or HTTP
  401/403/429 responses. Require the requested job's title link before parsing a
  detail panel; otherwise the previous card's content can be assigned a new ID.
- User direction 2026-10-04: after staging tests, clean/push and enable production
  classification AND channel delivery. There are 25 named families plus `other`.
  Route only by job_family; employer_sector is analytics-only. All 26 channel
  IDs and bot posting permissions passed the read-only audit on 2026-10-04.
  Never enable delivery based only on bot permissions.
- `.env.example` contains blank credentials/channel values and safe defaults.
  Never add deployment IDs, credentials, exports or authenticated captures to Git.
  Parser fixtures contain only public job fields; runtime snapshots remain ignored.
- The user removed legacy `dataanalysis/` to start fresh. Current analytics use
  `core/analytics.py` and `scripts/report_enrichment.py`; do not recreate old
  department-based extraction, backfills or charts. Raw production jobs remain.
- Preserve explicit title seniority: validation must not recalculate it from
  experience. Require valid family/specialization pairs and Low/Other review flags.
  Old industry rows become obsolete, not reinterpreted or automatically requeued.
- Check required structures and source text, not optional salary/recruiter fields.
  All-seen pages are healthy. Indeed detail-cap snippets are deliberate partial
  data, not proof of a structural failure. Preserve cards after failed requests.
- Snapshots strip script tags, but hidden `<code>` elements can retain embedded
  account JSON. Keep them local; never paste account state into Telegram or
  commit authenticated page captures. Use minimal fixtures for offline checks.

## Whole-cycle batching and local monitoring (2026-10-04)
- User requires every uncached job from one completed scrape cycle in ONE
  completion request, with no 25-job cap. `scrape_batches` links all boards;
  `main.py` owns the cycle boundary and scraper lock. Never classify mid-cycle.
- `enrichment_requests` records shared usage once. Validate exact output IDs and
  cardinality before atomic publication. No automatic single-job splitting.
- Network, provider, schema and budget failures stay pending; never fabricate
  Other. Retry indefinitely with bounded delay; reserve cost before each call.
- `delivery` is separate from `enrichment`; both flags still gate posting.
  Each worker owns its process lock. `monitor` independently sends deduplicated
  local alerts; external outage monitoring is explicitly deferred by the user.
- Run `scripts/verify_pipeline.py` (also included in verify_offline.py) for the
  >25-job single-request test, cycle boundaries, failures, cost accounting,
  concurrent delivery and alert deduplication. See docs/PIPELINE.md.

- Whole-cycle update deployed at 23:06 Cairo (application `771d456`). First live
  cycle: 8 jobs in one completion request, 8 correctly delivered, all boards ok,
  monitor healthy. Image `rtjobs-scraper:production-whole-batch-20261004`; prior
  `production-family-v2` is retained. See docs/DEPLOYMENT.md for exact timings.

## Immediate queue notifications
- After committing a complete scrape cycle, notify `enrichment`; after committing
  validated results, notify `delivery` via `core/wakeup.py`. Notifications must
  never precede commits or block producers. SQLite remains the durable queue.
- Workers bind the Unix socket in the shared data volume under their existing
  process lock, before the startup scan. Clear hints BEFORE scanning SQLite to
  avoid losing a commit between an empty scan and the wait. Keep bounded recovery
  polling, retry deadlines and whole-cycle batching; never send jobs in a hint.
- `scripts/verify_wakeup.py` tests real IPC between processes, commit visibility,
  immediate delivery, missed notifications, restart, backoff and shutdown. It is
  included in the full offline suite and needs permission for local Unix sockets.

- Immediate notifications deployed at 23:21 Cairo (`d1d8fb2`), image
  `production-immediate-dispatch-20261004`. First live verification: cycle
  completed and its 4-job request started in the same recorded second; results
  saved 14 seconds later and all 4 delivered by the following second.

## Latency, travel and budget lessons (2026-10-08)
- Travel (laptop suspend/disconnect, Oct 7-8) stranded dead keep-alive sockets
  in the enrichment worker's long-lived `requests.Session`: every cycle's
  first classify attempt failed `ConnectionError`, the 2-min retry succeeded.
  `Enricher` now discards pooled connections after any ConnectionError
  (`_discard_pooled_connections`); timeouts keep their budget reservation (may
  still be billed), connection-level failures release it. Previously every
  failed attempt kept its ~$0.02 reservation and ~45 flaky attempts leaked
  ~$0.95 of the $1 daily budget, deferring 200 jobs to midnight.
- Latency instrumentation is mandatory infrastructure, not optional logging:
  `core/timing.py` (`stage`/`timed`/`record`) writes one `latency_events` row
  + one parseable `LATENCY source=.. stage=.. seconds=..` INFO line per stage
  (chrome cold start, login/checkpoint, search pages, detail panels, human
  delays, persist, enrichment/delivery queue pickup, classify request,
  publish, delivery batch). Recording is best-effort and must never raise
  into the pipeline. Aggregate with `scripts/report_latency.py` (stage
  stats, saved->delivered per job via `jobs.notified_at`, scheduler-grid
  boot delay); it has offline coverage in `scripts/verify_pipeline.py`.
- Scraper logs persist in the volume now (`LOG_FILE=/data/logs/scraper.log`
  on the one-shot service) so run history survives `docker start` cycles.
