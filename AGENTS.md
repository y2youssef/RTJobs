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
2. **Wuzzuf data comes from the SSR blob**, not just the DOM:
   `window.Wuzzuf.initialStoreState.job.collection` (full entities: HTML
   description/requirements, exact `postedAt` `MM/DD/YYYY HH:MM:SS`,
   salary, career level…). It is parsed from `page.content()` with marker
   regex + brace balancing (`_extract_state`) — NOT `page.evaluate`
   (evaluate silently failed under stealth isolated contexts).
3. **Wuzzuf pagination** is a page index: `?q=&start=0`, `start=1`, …
   (15 jobs/page). Stop condition: first `external_id` already in
   `seen_ids` (default sort is by date). Job id = numeric prefix of the
   slug: `/jobs/p/<id>-<slug>`.
4. **LinkedIn login**: fetch `https://www.linkedin.com/login`, fill with
   human-like typing (EN+AR aware), then `_verify_routing` waits
   event-based via `page.wait_for_url` for `(feed|/jobs|checkpoint|security_verification)`
   — do NOT put `login` in that pattern (current URL matches it instantly).
   Checkpoint/2FA → alert + pause up to `CHECKPOINT_WAIT_SECONDS` for a
   manual solve over CDP. Profile wipe (after max retries + cooldown) must
   happen BEFORE the browser starts (`LinkedInBoard.run`), never inside a
   page_action of a live Chrome.
5. **`markup.py` sanitizer**: skip-tags must not count depth for void
   elements (`meta`, `link`, …) or the first `<meta>` swallows the whole
   document (this bug shipped once and corrupted all snapshots).
6. Telegram messages are MarkdownV2 — all dynamic text through
   `escape_md`; a 400 response triggers one plain-text retry.
7. `.env` must NEVER be committed (it was once — secrets are in git
   history; treat them as rotated). It is gitignored; untracked.

## Env vars (see config.py / README for defaults)
Required: `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`, `TELEGRAM_TEST_ID`
(failure channel; override: `TELEGRAM_FAILURE_CHAT_ID`),
`LINKEDIN_EMAIL`, `LINKEDIN_PASSWORD`.
Notable: `LINKEDIN_ENABLED` (currently `false` in `.env` — LinkedIn is
parked, Wuzzuf runs alone), `HEADLESS`, `DATA_DIR`, `MARKUP_DIR`,
`CHROME_DEBUG_PORT=9222`, `CHECKPOINT_WAIT_SECONDS`, `MAX_LOGIN_RETRIES`,
`WUZZUF_SEARCH_URL`, `*_PROFILE_DIR`.

## Current state / how things were last verified
- Wuzzuf end-to-end verified: 15 jobs/page scraped, enriched from SSR state
  (descriptions, exact timestamps), deduped, saved, notified.
- LinkedIn login flow verified up to credential submit; scraping path is
  disabled via env until re-enabled deliberately.
- Docker: python:3.13-slim + real Chrome + xvfb-run; volumes
  `chrome_profile`, `wuzzuf_profile`, `scraper_data`; `./markup`
  bind-mounted to `/data/markup`; CDP mapped to 127.0.0.1:9222.

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
Set dummy env before importing config in test scripts:
`TELEGRAM_TOKEN=x TELEGRAM_CHAT_ID=1 TELEGRAM_TEST_ID=2 DATA_DIR=<tmp> MARKUP_DIR=<repo>/markup`.

## Conventions
- Selectors: ALWAYS in `markup/<site>/selectors.json`, loaded via
  `load_board_selectors(site)`; missing file = fail loudly at startup.
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
