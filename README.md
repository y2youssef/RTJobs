# RTJobs

Headful job-board scraper (LinkedIn + Wuzzuf + Indeed) that runs on a schedule in Docker, persists jobs to SQLite (no cap — all jobs retained via `seen_ids` dedupe), and notifies you on Telegram — jobs on one channel, failures on a separate alert channel. Boards can be toggled via env (`LINKEDIN_ENABLED`, `WUZZUF_ENABLED`, `INDEED_ENABLED`).

## Architecture

```
main.py                  orchestrator: runs every enabled board
config.py                single source of truth (env vars)
core/
  db.py                  SQLite: jobs, seen_ids, runs, login_state
  classify.py            strict OpenRouter extraction and local validation
  enrichment_worker.py   whole-cycle classification in one model request
  delivery_worker.py     independent Telegram delivery
  pipeline_monitor.py    local pipeline alerts
  telegram.py            job notifications + failure alerts
  markup.py              sanitized HTML snapshots for selector debugging
  login_state.py         retry/cooldown/profile-wipe logic
  human.py               human-like delays
boards/
  base.py                JobBoard interface (add new sites here)
  linkedin/              login state machine + scrapling Spider
  wuzzuf/                Cloudflare-protected SSR scraper (solve_cloudflare)
  indeed/                email-code login + embedded JSON scraper
markup/enrichment/       prompt, JSON schema, taxonomy, evaluation fixtures
markup/<site>/
  selectors.json         ALL CSS selectors per site (edit here, not in code)
  snapshots/<kind>/      dated HTML snapshots (auto-pruned to 20/kind)
```

## 1. Environment variables (`.env`)

Copy [.env.example](.env.example) to `.env`, then fill credentials and every
`TELEGRAM_CHANNELS_JSON` value. Production channel IDs, account addresses and API
keys belong only in `.env`, which is excluded from Git and Docker image builds.
`OPENROUTER_BASE_URL` and `TELEGRAM_API_BASE_URL` configure API endpoints.

```
# Required
TELEGRAM_TOKEN=...
TELEGRAM_CHAT_ID=...          # job notifications
TELEGRAM_TEST_ID=...          # used as the failure channel (or override):
TELEGRAM_FAILURE_CHAT_ID=...  # optional, wins over TELEGRAM_TEST_ID

LINKEDIN_EMAIL=...
LINKEDIN_PASSWORD=...

# Optional (defaults shown)
HEADLESS=false                # docker sets this via compose
DATA_DIR=.                    # docker: /data
CHROME_DEBUG_PORT=9222        # remote debugging port
CHECKPOINT_WAIT_SECONDS=600   # pause for manual 2FA/checkpoint solve
MAX_LOGIN_RETRIES=3
LINKEDIN_NAVIGATION_RETRY_DELAY_SECONDS=3 # three navigation attempts, with a pause
LINKEDIN_SEARCH_RECOVERY_ATTEMPTS=1       # extra reload after an unrendered search (0-2)
LINKEDIN_SEARCH_RECOVERY_TIMEOUT_SECONDS=30
LINKEDIN_DETAIL_RECOVERY_TIMEOUT_SECONDS=20 # one longer detail retry
MAX_SNAPSHOTS_PER_KIND=20
KILL_CHROME_ON_START=false    # docker: true
HEALTHCHECK_URL=              # optional external heartbeat sent by monitor only when pipeline checks pass
LINKEDIN_ENABLED=true
WUZZUF_ENABLED=true
INDEED_ENABLED=false          # see INDEED.md
INDEED_EMAIL=                 # Indeed sign-in address (emailed codes); see INDEED.md "Login" section
LOG_LEVEL=INFO                # debug/info/warning/error
LOG_FILE=./logs/rtjobs.log    # docker: /data/logs/rtjobs.log (rotating 5×5MB); empty to disable file log
```

## 2. Run locally (visible browser window)

```bash
python -m venv .venv                  # first time only
source .venv/bin/activate.fish             # Linux/macOS — Windows: .venv\Scripts\activate
pip install -r requirements.txt
scrapling install                     # browser deps, if first time
python main.py
```

A Chrome window opens, logs you in (or reuses the saved profile), scrapes
LinkedIn, saves jobs, and posts new ones to Telegram.

## 3. Run in Docker (scheduled, headful via Xvfb)

```bash
docker compose up -d          # builds image, starts ofelia scheduler
docker compose logs -f scraper
```

- `ofelia` triggers the scraper container **every 6 minutes** (edit
  `ofelia.job-run.scraper.schedule` in `docker-compose.yaml`).
- Chrome runs headful on a virtual display (`xvfb-run`) with remote debugging
  enabled.
- Persisted in named volumes: `chrome_profile` (login session), `scraper_data`
  (SQLite DB). `./markup` on the host is bind-mounted for offline debugging.

## 4. Step into the live scrape (CDP)

While a run is in progress (Chrome is only up during a run):

1. Open `http://localhost:9222` in a browser, or `chrome://inspect` → "Remote
   target" (or any CDP client — `curl http://localhost:9222/json/version` to
   confirm). Works because `docker-compose.yaml` uses `network_mode: host` — the
   container's loopback **is** the host's loopback (a bridge `ports:` mapping
   would not reach it).
2. You can watch, drive, and interact with the real browser — this is also how
   you solve LinkedIn checkpoints/2FA manually.
3. Port is bound to `127.0.0.1` only. To reach it from another machine,
   SSH-tunnel: `ssh -L 9222:localhost:9222 user@host`.
4. If `curl` fails outside a run, that's expected — Chrome is one-shot per
   scheduled invocation (`docker start rtjobs`). Check `docker logs ofelia` for
   the next fire time.

If your installed Chrome opens a black/blank inspector but **inspect fallback**
works, use that fallback or run `python scripts/inspect_chrome.py` while a
scrape is active. Paste its `devtools://` URL into your Chrome address bar to
use the installed browser's own frontend. The script also prints the remote
Chrome version and its advertised frontend URL. Generate a fresh URL after
each browser/tab restart. A blank inspector alone does not establish that
the scraped page or Xvfb is black; check a CDP screenshot before changing
rendering flags.

## 5. LinkedIn login behavior

- **Session active** (redirect to `/feed`) → scrape directly.
- **Login page** → human-like fill (EN + AR), verify routing after submit.
- **Checkpoint/2FA** → alert sent to the failure channel; the run **pauses up
  to `CHECKPOINT_WAIT_SECONDS`** so you can solve it via CDP; solved → resumes.
- **Failures** → consecutive-failure counter with escalating cooldowns
  (5m → 15m → 30m). At `MAX_LOGIN_RETRIES` the failure channel gets an alert,
  runs are skipped while blocked, and after the cooldown the profile is wiped
  for a fresh login.
- To clear the cooldown manually (e.g. after fixing a bug locally):
  `python main.py --reset-login`

## 6. Telegram channels

- **Jobs channel** (`TELEGRAM_CHAT_ID`): one message per new job, MarkdownV2,
  retries with 429 backoff.
- **Failure channel** (`TELEGRAM_TEST_ID` / `TELEGRAM_FAILURE_CHAT_ID`):
  login failures, checkpoints, mid-scrape session expiry, board crashes, and
  missing-selector errors — each with snapshot path and CDP hint.

Parser checks also alert this channel when expected cards, IDs, JSON blobs,
titles or descriptions disappear, or attempted detail requests lose jobs.
Wuzzuf additionally checks that source requirements survive extraction. Messages
include the failed check, counts/sample IDs and a sanitized snapshot path when
available. These symptoms can indicate markup changes, incomplete loading or
access problems; the alert does not assume a particular cause.

The `scrape_health` table records each check and its last successful alert.
One unresolved issue produces one alert across scheduled runs; failed alert
delivery is retried. A successful observation resets that check so a recurrence
alerts again. Recovery is logged. All-seen pages are normal; optional salary or
recruiter fields are not required. The checks detect common silent failures,
not every possible semantic change in a site's data.
Runs with failed parser checks are recorded as `degraded` with their diagnostic
details, even if the spider completes and valid partial jobs are saved. LinkedIn
alerts distinguish its startup/loading shell from an absent job layout and report
whether waiting for cards or scrolling the list failed.
LinkedIn navigation keeps three bounded attempts with a three-second pause.
An unrendered search gets one additional reload; a slow detail gets one longer
retry and must show the requested job's title link. Login/checkpoint pages and
HTTP 401/403/429 stop recovery. Alerts retain transport codes, HTTP errors or
pending-request evidence from LinkedIn and its asset host, without request URLs,
query strings, cookies or bodies. Successful recovery is logged without a failure
alert. These checks cannot always identify whether the local network or the remote
server caused a connection failure.

## 7. When the site changes (markup resilience)

- Every selector lives in `markup/<site>/selectors.json` — fix selectors
  there, no code change.
- Snapshots are saved automatically for: login failures, checkpoints, empty
  search pages, login-redirects — sanitized and pruned to 20 per kind under
  `markup/<site>/snapshots/`.
- Reference files in `markup/linkedin/` (e.g. `manual_login_en.html`) are
  optimized versions of real captured markup — use them to re-derive selectors
  offline against the actual DOM.

## 8. Blocking companies

Edit `markup/blocked_companies.json` (bind-mounted — no rebuild needed):

```json
{
  "*":        ["blocks every source"],
  "linkedin": ["Company Name"],
  "wuzzuf":   ["Company Name"],
  "indeed":   ["Company Name"]
}
```

Matching is case-insensitive substring, so `"alignerr"` also blocks
`"Alignerr Inc."`. Blocked jobs are dropped before saving/notification and
marked seen so they're never re-scraped.

## 9. Adding a new board

1. Subclass `JobBoard` in `boards/<site>/` with `name`, `run()` returning the
   number of new jobs saved (job dicts follow the shape `source, external_id,
   title, company, posted_at, description, link, extra` and are persisted via
   `boards.base.persist_jobs`). `main.py` handles delivery after Chrome closes,
   or delegates it to the optional enrichment worker.
2. Create `markup/<site>/selectors.json`.
3. Register in `BOARDS` in `main.py`; keep `enabled = False` until ready.

Wuzzuf notes: it's Cloudflare-protected, so its spider uses
`solve_cloudflare=True` (first request solves the challenge automatically)
and a persistent profile (`wuzzufprofile/`) so the clearance cookie survives
between runs. Pages are server-side rendered — 15 jobs per page, pagination
via `start=N` (page index). Job descriptions/requirements and exact
timestamps come from the embedded SSR state (`window.Wuzzuf`), parsed
straight out of the page HTML (`_extract_state`) and merged over the
DOM-extracted card data. Reference markup: `markup/wuzzuf/wazzuf_guide.txt`.

## 10. Logs

Logs go to **both** `stdout` (visible via `docker logs scraper` / `docker logs ofelia`) and a **rotating file** at `LOG_FILE` (default `DATA_DIR/logs/rtjobs.log` → in Docker `scraper_data:/data` → `/data/logs/rtjobs.log`). Keeps 5×5 MB. Set `LOG_LEVEL=DEBUG` for verbose, `LOG_FILE=""` to disable file logging. Locally check `./logs/rtjobs.log`.

## Optional Egypt classification and enrichment

The current [specification](EXPAND.md) uses 25 professional job families plus
`other` (26 categories). Routing uses `classification.job_family` only;
`employer_sector` is analytics metadata. `TELEGRAM_CHANNELS_JSON` in private `.env`
contains all 26 verified family-to-channel mappings, including General Management;
see the [channel audit](docs/channel-audit.md).

The strict jobs-array output includes family/specialization/confidence/review,
employer sector/posting entity, seniority, requirements, tools, certifications,
languages, work conditions, compensation and explicit candidate constraints.
Generic skills fields were removed. Title seniority takes precedence over years.
Both `ENRICHMENT_ENABLED` and `CLASSIFIED_DELIVERY_ENABLED` default to false.
After staging tests, the user authorized production classification and channel
delivery. Activation is explicit in the deployment environment, not public defaults.
The [production deployment record](docs/DEPLOYMENT.md) documents the verified
rollout, active settings and rollback image.

Scraping always saves the original parsed job to `jobs` first. When enabled,
the same transaction creates a `job_enrichments` row in the **same SQLite DB**.
Completing a scrape cycle immediately notifies the browser-free classifier,
which sends every uncached new job together in **one OpenRouter completion request**, without a
25-job cutoff. Results are validated together and saved individually alongside
the raw jobs. Committed results immediately notify a separate delivery worker,
which posts ready jobs while the classifier handles later cycles. The scraper keeps its six-minute schedule.
See [the pipeline guide](docs/PIPELINE.md) for timing, retries and local monitoring.

Two caches reduce cost:

- **Provider input cache:** the shared prompt, taxonomy and schema stay stable
  before each job's changing content. OpenRouter's provider can reuse that prefix.
  The three-job live preview reported 0, 3,909 and 3,909 cached input tokens;
  hits are provider-dependent. See [OpenRouter prompt caching](https://openrouter.ai/docs/guides/best-practices/prompt-caching).
- **SQLite result cache:** validated outputs are keyed by full relevant input,
  model and prompt/schema version. Identical content skips the API entirely,
  including after worker restarts; a changed description invalidates the key.

Configuration (all defaults are in `config.py`):

```dotenv
ENRICHMENT_ENABLED=false
CLASSIFIED_DELIVERY_ENABLED=false
OPENROUTER_API_KEY=           # existing OPENROUTER_API is accepted too
CLASSIFIER_MODEL=openai/gpt-6-luna
CLASSIFIER_DAILY_BUDGET_USD=1
CLASSIFIER_TIMEOUT_SECONDS=300
CLASSIFIER_MAX_OUTPUT_TOKENS=2200  # allowance per job, scaled for the whole batch
CLASSIFIER_MAX_INPUT_CHARS=0      # preserve full relevant source input
CLASSIFIER_ALERT_AFTER_FAILURES=3
CLASSIFIER_RETRY_MAX_SECONDS=3600
ENRICHMENT_POLL_SECONDS=30  # fallback for missed notifications and retries
NOTIFY_BATCH_SIZE=50
NOTIFY_PER_CHANNEL_LIMIT=20
DELIVERY_POLL_SECONDS=2    # fallback; saved results notify delivery immediately
TELEGRAM_CHANNELS_JSON=       # required complete JSON family-to-ID map for classified delivery
```

Before each paid request, the worker reserves a conservative amount against its
local-day budget, then reconciles it with returned usage cost. Failed or timed-out
requests retain their reservation because billing is uncertain. Retries use
persistent backoff, without a terminal fallback. Network/provider failures remain
pending and budget exhaustion waits until the next local day. Neither is routed
to Other. Full relevant input is preserved by default. Provider-limit or malformed
responses keep the whole outstanding batch pending; no silent splitting into
single-job requests. Request cost is recorded once in `enrichment_requests`.

Preview a JSON list of raw jobs without saving those jobs or sending Telegram
messages; use a scratch data directory because the spend ledger is still recorded:

```bash
DATA_DIR=/tmp/rtjobs-preview MARKUP_DIR="$PWD/markup" LOG_FILE='' \
  python -m core.enrichment_worker --preview /path/jobs.json --limit 3 \
  --output /tmp/rtjobs-preview-report.json
```

When activation is explicitly requested, set both `ENRICHMENT_ENABLED=true` and
`CLASSIFIED_DELIVERY_ENABLED=true` in `.env` for the scraper and workers, verify every
channel ID/title and bot posting permission, then run:

```bash
docker compose --profile enrichment up -d --build
docker compose logs -f enrichment delivery monitor
```

Check permissions beforehand with `.venv/bin/python scripts/check_channels.py`.
It compares channel IDs/titles, detects missing or duplicate mappings, checks bot
posting permissions and sends no messages.
Add the configured bot to each channel as an administrator with Post Messages.
`chat not found` can mean the bot cannot access the channel or its ID is wrong.

Deploy between scraper runs: stop the scheduler and let an active scraper finish
before recreating it, then restart the scheduler. A browser-free worker can also
run once with `python -m core.enrichment_worker --once --no-send` for inspection.
Do not enable the scraper's flag without starting the worker, or new jobs will
wait in the queue. Stop both workers and recreate the scraper with the flag false
to return to legacy delivery. Pending enriched deliveries then use the legacy
channel unless a destination was already saved by an earlier attempt.

Historical raw rows are not automatically queued or rewritten. Old industry
enrichments are retained as `obsolete` and never routed as new job families. Destination is saved
on the first delivery attempt, acknowledgement follows each success, and failed
deliveries have persistent backoff. Telegram has no idempotency key: a crash after
acceptance but before the local acknowledgement can still cause a duplicate.
Worker logs expose `result_cache`, input/cached/write token counts and cost;
`job_enrichments.usage_json` retains the returned usage breakdown.

## Verification and current limits

Run `.venv/bin/python scripts/verify_offline.py` for fixture-based parsing,
SQLite migration, caching, routing, retries and browser-patch checks. It uses
temporary databases, dummy credentials and blocks HTTP requests. Analytics-only
packages are in `requirements-analysis.txt`; the scraper image installs only
`requirements.txt`.

The 2026-10-04 runtime update pins Scrapling 0.4.15 and Playwright/Patchright
1.63.0. Browser checks verified tab reuse, idempotent setup and persistent CDP
cookies; isolated live samples returned full descriptions on all three boards.
The upstream Cloudflare retry fix is included, but those requests did not
encounter a challenge, so they do not prove that every access block is resolved.
Deployed at 11:19 Cairo time on 2026-10-04: all three boards completed with
`ok` run status; five new LinkedIn jobs were saved and Telegram-acknowledged.
The pre-upgrade backup preserved 38,227 jobs, and the enrichment queue stayed
empty. Rollback image: `rtjobs-scraper:before-scrapling-upgrade-20261004`.

Live checks on 2026-10-03 used isolated copies of the authenticated browser
profiles and sent no test Telegram messages. Wuzzuf's separate requirements are
now retained; LinkedIn keeps available employer industry/about text; Indeed
supports current `_rootProps` pages and merges salary/location/employment data
even when a description is already present. Existing historical omissions need
a separate backfill. Indeed still reads one search page and fetches at most ten
new detail pages per run; skipped or failed details retain their cards with
`description_truncated=true` and a `detail_status` reason. This is not exhaustive
coverage of every listing on either site.

Deployment on 2026-10-03: parser fixes and error alerts are running in Docker;
the first run completed LinkedIn/Wuzzuf/Indeed successfully with all observed
checks healthy. Migration preserved 37,919 existing jobs without queuing history.
Industry routing has been replaced by the job-family implementation. Production
classification and family delivery were subsequently authorized on 2026-10-04;
passing a channel check alone does not activate them.

## 11. Troubleshooting

| Symptom | Fix |
|---|---|
| `curl localhost:9222` fails (no run) | Expected — Chrome only runs during a scrape. Wait for next `ofelia` tick (`docker logs ofelia`) or `docker start rtjobs`. Inside a run it should respond. |
| `curl localhost:9222` fails (mid-run) | Check `docker logs scraper` for Chrome launch errors; verify `network_mode: host` and that port 9222 is free. |
| `xvfb-run` hangs forever | Missing `xauth` package or PID 1 `SIGUSR1` issue — image installs `xauth` and `docker-compose.yaml` sets `init: true` (tini). See `AGENTS.md:6a-b`. |
| Chrome SIGTRAP/crash on start | No writable `HOME` — image sets `HOME=/home/scraper` (AGENTS.md:6c). |
| `profile appears to be in use` | Stale `Singleton*` lock after kill — `KILL_CHROME_ON_START=true` + `core/browser.py:launch_cdp_chrome(clean_locks=True)` clears it (AGENTS.md:6d). Also handled by `init: true` + SIGTERM grace. |
| Times in DB off by hours | Container TZ defaults to UTC — compose pins `TZ=Africa/Cairo` (AGENTS.md:6e). |
| `docker stop` leaves zombies | Fixed by `main.py` SIGTERM handler (`stop_chrome` + `finish_run` interrupted) + `init: true`. `kill_zombie_chrome` is now safety-net only. |
| No failure alerts | Check `TELEGRAM_TEST_ID`/`TELEGRAM_FAILURE_CHAT_ID` is a chat the bot can post to. |
| Stuck "blocked" state | `python main.py --reset-login` (or: `sqlite3 /data/rtjobs.db "UPDATE login_state SET value='0' WHERE key IN ('retry_count','blocked_until','max_retries_alerted');"`) |
| Chrome won't start in container | Profile lock from a crash: `KILL_CHROME_ON_START=true` handles it; otherwise `docker compose down && docker compose up -d`. |
| Tab spinner spins forever / `Page.goto: Timeout ... waiting until "load"` | scrapling waits for the browser `load` event, which LinkedIn never fires (hanging tracker/CDN resources — your normal Chrome hides this via extensions/adblock). Already handled: navigations wait for `domcontentloaded` instead and heavy resources are dropped (see `core/browser.py:patch_no_load_wait`). |
| Login keeps failing after site change | Check the newest `markup/linkedin/snapshots/login_failure/*.html` and update `selectors.json`. |
| Silent "runs stopped" (no jobs, no alerts) | Check the local `monitor` service. For host-wide outages, configure an external service through `HEALTHCHECK_URL`; the monitor pings only when pipeline checks pass. |
| Full `jobs` table growing large | By design now uncapped (no `MAX_JOBS` prune) — use `DELETE FROM jobs WHERE ...` or rotate `rtjobs.db` volume if needed. `seen_ids` keeps dedupe forever. |

Job-family analytics are computed outside the model by `core/analytics.py`.
The fresh [analytics guide](docs/ANALYTICS.md) covers the current schema, SQLite
queries, report generation, coverage and interpretation. Legacy `dataanalysis/`
was removed at the user's request.
Run this on a migrated database for a read-only report and a standalone
family × sector/tools/certifications dashboard:

```bash
python scripts/report_enrichment.py --db PATH --output report.json --html dashboard.html
```

Coverage is explicit;
missing salary, currency, work setup and employer sector are never invented.
