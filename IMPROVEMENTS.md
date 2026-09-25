# Improvements & Refactoring Plan

Status: **done 2026-09-04 — all S/XS items shipped (A1-A4, B1-B4, C2-C5); only C1 (pytest, M) remains as future work.
New follow-up audit 2026-09-04 added as section D (all TODO).**
Generated from a full-repo audit on 2026-08-10. Effort: XS <15min, S <1h, M few hours.

## Key finding from the audit

The missing company names on LinkedIn are **NOT a selector problem**. The
`company_missing` snapshot shows the top card full of `ghost-company`
shimmer placeholders (19 ghost markers) — we scrape the detail panel before
LinkedIn hydrates the company name. Fix is a **wait** (A1), not a selector change.
Implemented as hydration poll + card `artdeco-entity-lockup__subtitle` fallback
in `boards/linkedin/scraper.py:162-230` and `markup/linkedin/selectors.json:15`.
Also removed `MAX_JOBS=10000` prune (now uncapped, `seen_ids` handles dedupe).

## A. Correctness & robustness (do these first)

- [x] **A1 — Company-name hydration wait** (S) — DONE 2026-09-04
  `boards/linkedin/scraper.py::_scrape_card` now polls live `detail_company_loc`
  for non-empty `text_content()` up to ~6s (instead of fixed sleep) + falls back
  to card `artdeco-entity-lockup__subtitle` and live detail via Playwright if
  `Selector(html)` is stale. Verified 18/20 `company_missing` snapshots recover;
  2 genuinely empty (confidential) remain empty.

- [x] **A2 — Pin versions** (S) — DONE 2026-09-04
  `requirements.txt`: `scrapling[all]==0.4.12`, `playwright==1.61.0`, `patchright==1.61.2`,
  `requests==2.34.2`, `python-dotenv==1.2.2`. `docker-compose.yaml`: `mcuadros/ofelia:0.3.22`
  (was `latest`; 0.4.x drops leading-seconds cron). Digest noted in compose comment.

- [x] **A3 — SIGTERM handling in `main.py`** (S) — DONE 2026-09-04
  Installs `SIGTERM`/`SIGINT` handler that raises `SystemExit`, boards wrap
  runs with `_finish` guard + `SystemExit` → `interrupted` status, and
  `finally: stop_chrome` always runs. `init: true` (tini) remains required.

- [x] **A4 — `WUZZUF_ENABLED` env toggle** (XS) — DONE 2026-09-04
  `config.py:WUZZUF_ENABLED` + `boards/wuzzuf/__init__.py:enabled = WUZZUF_ENABLED`
  (was hardcoded `True`). Also added `HEALTHCHECK_URL` (B4) and friendly
  telegram env validation (C4).

## B. Ops / repo hygiene

- [x] **B1 — Delete legacy artifacts** (XS) — DONE 2026-09-04
  Deleted `example.html`, `login.html`, `linkedin_jobs.db`, `linkedin_jobs.json`,
  `jobs.json`, moved `dbtojson.py` → `scripts/dbtojson.py`, deleted
  `markup/linkedin/bug_not_reading_company_name_when_no_icon.html` (4.1M wrong page).
  Added `.dockerignore` to keep `chromeprofile`/`wuzzufprofile`/`indeedprofile`,
  `markup/*/snapshots`, `dataanalysis`, `jobnetworkminiapp` out of image; updated
  `.gitignore` (`jobs.json`, `dataanalysis`, `jobnetworkminiapp`).

- [x] **B2 — Dead code** (XS) — DONE 2026-09-04
  Removed `config.py:CHROME_ARGS` (unused) and fixed `Dockerfile` comment
  (`--no-sandbox` is still needed; comment now says non-root limits blast radius).

- [x] **B3 — README refresh** (S) — DONE 2026-09-04
  Fixed `7`/`7b` → `7`/`8`/`9`/`10`, CDP section now documents `network_mode: host`
  (no `ports:`), expanded troubleshooting with xauth/init/HOME/Singleton/TZ/SIGTERM/
  healthcheck rows, added `WUZZUF_ENABLED`/`INDEED_ENABLED`/`HEALTHCHECK_URL` to env
  docs, noted uncapped `jobs` table.

- [x] **B4 — healthchecks.io dead-man ping** (S) — DONE 2026-09-04
  `config.py:HEALTHCHECK_URL` + `main.py` `requests.get(HEALTHCHECK_URL, timeout=10)`
  at end of successful run (non-fatal on failure). Covers silent scheduler death.

## C. Structure & code quality

- [ ] **C1 — pytest suite** (M) — TODO
  Promote the session's ad-hoc scripts into `tests/` (offline, fixtures in
  `markup/`): `_extract_state`, `_extract_jobs`, `sanitize_html`,
  blocklist matching, pagination URL builders, `login_state`, Telegram
  payload fallback, + the CDP cookie-persistence integration test.
  Best defense against "the site changed" regressions.

- [x] **C2 — Dedup board boilerplate** (S) — DONE 2026-09-04
  Added `boards/base.py:persist_and_notify(source, items)` and wired all three
  boards (`linkedin`, `wuzzuf`, `indeed`) to it.

- [x] **C3 — `ChromeService` helper** (S) — DONE 2026-09-04
  Added `core/browser.py:chrome_session(profile_dir, port, ...)` context manager
  wrapping `launch_cdp_chrome`/`stop_chrome`; boards now `with chrome_session(...) as cdp:`.

- [x] **C4 — Config validation at startup** (XS) — DONE 2026-09-04
  `config.py` now `os.environ.get` for telegram vars and raises a single
  `RuntimeError` listing all missing keys instead of bare `KeyError`.

- [x] **C5 — `print` → `logging`** (S) — DONE 2026-09-04
  Added `core/log.py:setup_logging()` (stdout + rotating file `DATA_DIR/logs/rtjobs.log`
  5×5 MB, `LOG_LEVEL`/`LOG_FILE` env, idempotent) and migrated all boards/core to
  `logger = logging.getLogger(__name__)` with `info/warning/error` levels. `main.py`
  calls `setup_logging()` first; `docker logs` now shows timestamped, leveled output
  and `logs/` persists via `scraper_data:/data` volume.

## Extra — done alongside this batch but not in original audit
- Removed `MAX_JOBS=10000` pruning: `config.py:MAX_JOBS` and `core/db.py` prune
  `DELETE ... LIMIT ?` removed. `jobs` table is now uncapped; dedupe via
  `seen_ids` forever, `jobs.json` legacy removed.

## D. Follow-up audit 2026-09-04 (TODO — from second code review)

Bugs first, then direct perf wins. File refs are `path:line`.

- [ ] **D1 — Chrome-launch failure leaves `runs` stuck at `running`** (S)
  `boards/linkedin/__init__.py:62`, `boards/wuzzuf/__init__.py:42-46`,
  `boards/indeed/__init__.py:47-50`: `chrome_session(...)` sits OUTSIDE the
  `try` that calls `_finish()`. If `launch_cdp_chrome` raises (stale lock,
  slow start, port busy), no `finish_run` ever executes. Fix: move the
  `with chrome_session(...)` inside the `try` (or wrap launch with its own
  `start_run`/`finish_run`).

- [ ] **D2 — Unbounded growth: index + `seen_ids` retention** (S)
  Removing the prune (see Extra above) leaves `load_seen_ids`
  (`core/db.py:106`, full-table load 3×/run) and `get_unnotified`
  (`core/db.py:125`, `WHERE notified=0 AND source=? ORDER BY posted_at` with
  no index) scaling linearly with DB age. Fix: `CREATE INDEX IF NOT EXISTS
  idx_jobs_notified_source ON jobs(notified, source)` (+ index on
  `posted_at`), and decide a `seen_ids` retention policy (e.g. keep 90 days)
  or document that unbounded growth is accepted.

- [ ] **D3 — Cap the Telegram resend backlog** (S)
  `boards/base.py:37` fetches ALL unnotified; `core/telegram.py:70-99`
  leaves failures unnotified. One outage/bad-token run means every later run
  re-sends the whole backlog (3 attempts each) while the table keeps growing
  (see D2). Fix: cap per-run notify batch (e.g. oldest 50) and/or add a
  `notify_attempts` backoff column.

- [ ] **D4 — Mark Indeed snippet-only jobs as truncated** (XS)
  `boards/indeed/scraper.py:400-403`: when `_MAX_DETAIL_FETCHES=10` is hit,
  jobs are yielded with `description=snippet` and no marker — indistinguishable
  from full descriptions. Fix: set `extra["description_truncated"] = True`
  (or `"detail_skipped"`) on that path.

- [ ] **D5 — Small hardening batch** (XS)
  `core/telegram.py:16` builds `_API` once at import (token rotation needs a
  restart — build the URL inside `_send`); `core/blocklist.py:57-63` doesn't
  normalize the `source` arg (works today only because callers pass lowercase
  — add `source = _normalize(source)`); `core/telegram.py:87-91` indexes
  `job['title'/'company'/'link']` directly (`None` link renders as
  `[View](None)`, `)` in a URL breaks the Markdown link — use `.get()` +
  URL-safe handling).

- [ ] **D6 — Guard `patch_no_load_wait` against re-wrapping** (XS)
  `core/browser.py:51-86`: `page_setup` runs per fetch and wraps
  `page.goto`/`wait_for_load_state` again each time (nesting accumulates over
  ~20 Wuzzuf pages + Indeed details). Idempotent today; add a
  `_no_load_patched` flag and early-return.

- [ ] **D7 — C-speed JSON blob parsing** (S, perf)
  `boards/indeed/scraper.py:63-109` (`_extract_balanced_json`) and its twin
  `boards/wuzzuf/scraper.py:46-89` walk multi-MB HTML char-by-char in Python,
  ~11× per Indeed run (1 search + 10 details). Fix: replace brace-balancing
  with `json.JSONDecoder().raw_decode(html, idx=start)` — same semantics,
  ~10-50× faster.

- [ ] **D8 — Batch SQLite writes** (S, perf)
  `core/db.py:75-103` + `boards/base.py:23-29`: `save_job` opens a
  connection, does 2 inserts, commits, closes *per job*; `mark_notified`
  (`core/db.py:138`) the same per notification. Fix: one connection +
  `executemany` + single commit per board (pairs with the D2 index).

- [ ] **D9 — LinkedIn pacing + Indeed blob wait** (M, perf, needs live verify)
  `boards/linkedin/scraper.py:124,146-180`: 2-4s pause/card + settles + up to
  ~6s hydration poll ≈ 2.5-6 min/page × 4 pages, exceeding the 6-min ofelia
  window (overlapping schedules no-op on `docker start` of a running
  container). Shrink pauses and/or navigate straight to `/jobs/view/{id}/`
  instead of click+hydrate. `boards/indeed/scraper.py:347`: replace the fixed
  `wait_for_timeout(2500)` with a marker poll (`wait_for_function` on the
  mosaic blob, ~10s) — faster on good runs, fewer false-empty runs on slow
  ones. Also capture Chrome stderr on launch failure (`core/browser.py:137`
  uses `DEVNULL`, so "exited early with code N" has no diagnostics).

- [ ] **D10 — One container per board (fixes P4)** (M)
  YES — this is the structural fix for P4 ("three sequential Chrome cold
  starts per run on the same port"). Today one `rtjobs` container runs
  LinkedIn → Wuzzuf → Indeed sequentially: 3 profile loads + CDP handshakes
  in series, and LinkedIn's per-card sleeps (D9) delay the other two boards
  while Telegram sends (0.3s + RTT each) block the next board.
  Design sketch:
  - Same image, three services (`scraper-linkedin`, `scraper-wuzzuf`,
    `scraper-indeed`) with `command: ["xvfb-run", "-a", "python", "main.py",
    "--board", "<name>"]` — requires adding a `--board` CLI flag to `main.py`
    (keep default = all boards for local runs).
  - Separate profile volumes (already exist) + separate CDP ports
    (e.g. 9222/9223/9224, one `CHROME_DEBUG_PORT` per service) so live CDP
    attach still works per board; drop the shared-port assumption in
    `chrome_session` callers.
  - Shared `scraper_data:/data` volume for the SQLite DB → enable WAL mode
    (`PRAGMA journal_mode=WAL`) so concurrent board writers don't hit
    `database is locked` (keep the 30s `timeout` in `get_db`); per-board log
    files (`LOG_FILE`) or a log prefix per service.
  - Separate ofelia `job-run` entries (labels on the ofelia container, same
    pattern as today) so each board gets its own schedule/cadence — e.g.
    LinkedIn every 6 min, Wuzzuf/Indeed on their own interval — plus separate
    `HEALTHCHECK_URL`s per board for per-board dead-man pings.
  - Keep `*_ENABLED` env toggles as a second layer (compose `profiles:` or
    just not scheduling a service also works).
  Costs: ~3× Chrome RAM (~300-500 MB each, they run in parallel now),
  SQLite concurrency to get right (WAL + single-writer batches from D8),
  Telegram message ordering across boards becomes non-deterministic (fine —
  each message is self-contained). Do D2/D8 first so the shared DB is ready,
  then split the compose file.
