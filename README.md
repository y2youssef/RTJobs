# RTJobs

Headful job-board scraper (LinkedIn + Wuzzuf + Indeed) that runs on a schedule in Docker, persists jobs to SQLite (no cap — all jobs retained via `seen_ids` dedupe), and notifies you on Telegram — jobs on one channel, failures on a separate alert channel. Boards can be toggled via env (`LINKEDIN_ENABLED`, `WUZZUF_ENABLED`, `INDEED_ENABLED`).

## Architecture

```
main.py                  orchestrator: runs every enabled board
config.py                single source of truth (env vars)
core/
  db.py                  SQLite: jobs, seen_ids, runs, login_state
  telegram.py            job notifications + failure alerts
  markup.py              sanitized HTML snapshots for selector debugging
  login_state.py         retry/cooldown/profile-wipe logic
  human.py               human-like delays
boards/
  base.py                JobBoard interface (add new sites here)
  linkedin/              login state machine + scrapling Spider
  wuzzuf/                Cloudflare-protected SSR scraper (solve_cloudflare)
markup/<site>/
  selectors.json         ALL CSS selectors per site (edit here, not in code)
  snapshots/<kind>/      dated HTML snapshots (auto-pruned to 20/kind)
```

## 1. Environment variables (`.env`)

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
MAX_SNAPSHOTS_PER_KIND=20
KILL_CHROME_ON_START=false    # docker: true
HEALTHCHECK_URL=              # optional dead-man ping (healthchecks.io) at end of each run
LINKEDIN_ENABLED=true
WUZZUF_ENABLED=true
INDEED_ENABLED=false          # see INDEED.md
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

## 5. LinkedIn login behavior

- **Session active** (feed/jobs URL) → scrape directly.
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
   `core.db.save_job`).
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
| Silent "runs stopped" (no jobs, no alerts) | Set `HEALTHCHECK_URL` (healthchecks.io) — `main.py` pings it on success; configure dead-man alert there. |
| Full `jobs` table growing large | By design now uncapped (no `MAX_JOBS` prune) — use `DELETE FROM jobs WHERE ...` or rotate `rtjobs.db` volume if needed. `seen_ids` keeps dedupe forever. |
