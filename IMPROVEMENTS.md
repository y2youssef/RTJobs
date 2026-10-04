# Improvements & Refactoring Plan

Status: **done 2026-09-04 — all S/XS items shipped (A1-A4, B1-B4, C2-C5); only C1 (pytest, M) remains as future work.
Follow-up implementation 2026-10-03: D1–D8 addressed below; D9/D10 remain future work.**
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
  `requirements.txt` (updated 2026-10-04): `scrapling[all]==0.4.15`,
  `playwright==1.63.0`, `patchright==1.63.0`,
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

## D. Follow-up audit 2026-09-04 (updated 2026-10-03)

Bugs first, then direct perf wins. File refs are `path:line`.

- [x] **D1 — Chrome-launch failure leaves `runs` stuck at `running`** (S)
  **2026-10-03:** Implemented: every board records Chrome-launch failures as error/interrupted. Verified offline for all three boards.

  Original finding:
  `boards/linkedin/__init__.py:62`, `boards/wuzzuf/__init__.py:42-46`,
  `boards/indeed/__init__.py:47-50`: `chrome_session(...)` sits OUTSIDE the
  `try` that calls `_finish()`. If `launch_cdp_chrome` raises (stale lock,
  slow start, port busy), no `finish_run` ever executes. Fix: move the
  `with chrome_session(...)` inside the `try` (or wrap launch with its own
  `start_run`/`finish_run`).

- [x] **D2 — Unbounded growth: index + `seen_ids` retention** (S)
  **2026-10-03:** Implemented: partial indexes for pending notifications, WAL, and indexed page-ID lookups instead of loading all historical IDs. Permanent dedupe history is deliberately retained; no 90-day pruning.

  Original finding:
  Removing the prune (see Extra above) leaves `load_seen_ids`
  (`core/db.py:106`, full-table load 3×/run) and `get_unnotified`
  (`core/db.py:125`, `WHERE notified=0 AND source=? ORDER BY posted_at` with
  no index) scaling linearly with DB age. Fix: `CREATE INDEX IF NOT EXISTS
  idx_jobs_notified_source ON jobs(notified, source)` (+ index on
  `posted_at`), and decide a `seen_ids` retention policy (e.g. keep 90 days)
  or document that unbounded growth is accepted.

- [x] **D3 — Cap the Telegram resend backlog** (S)
  **2026-10-03:** Implemented: bounded batches, per-channel limits, fair channel selection, persistent attempt/backoff times and frozen delivery destinations.

  Original finding:
  `boards/base.py:37` fetches ALL unnotified; `core/telegram.py:70-99`
  leaves failures unnotified. One outage/bad-token run means every later run
  re-sends the whole backlog (3 attempts each) while the table keeps growing
  (see D2). Fix: cap per-run notify batch (e.g. oldest 50) and/or add a
  `notify_attempts` backoff column.

- [x] **D4 — Mark Indeed snippet-only jobs as truncated** (XS)
  **2026-10-03:** Implemented: description_truncated/source and detail_status distinguish full details from capped, unavailable or failed fetches. Failed requests retain the search card.

  Original finding:
  `boards/indeed/scraper.py:400-403`: when `_MAX_DETAIL_FETCHES=10` is hit,
  jobs are yielded with `description=snippet` and no marker — indistinguishable
  from full descriptions. Fix: set `extra["description_truncated"] = True`
  (or `"detail_skipped"`) on that path.

- [x] **D5 — Small hardening batch** (XS)
  **2026-10-03:** Implemented: normalized blocklist sources, optional-field and URL handling, pooled Telegram HTTP connections, token-safe request-error logging. Token rotation still requires process/container recreation: moving URL construction alone would not reload imported config.

  Original finding:
  `core/telegram.py:16` builds `_API` once at import (token rotation needs a
  restart — build the URL inside `_send`); `core/blocklist.py:57-63` doesn't
  normalize the `source` arg (works today only because callers pass lowercase
  — add `source = _normalize(source)`); `core/telegram.py:87-91` indexes
  `job['title'/'company'/'link']` directly (`None` link renders as
  `[View](None)`, `)` in a URL breaks the Markdown link — use `.get()` +
  URL-safe handling).

- [x] **D6 — Guard `patch_no_load_wait` against re-wrapping** (XS)
  **2026-10-03:** Implemented: idempotent sync/async guards; repeated async setup remains awaitable. Verified both contracts offline.

  Original finding:
  `core/browser.py:51-86`: `page_setup` runs per fetch and wraps
  `page.goto`/`wait_for_load_state` again each time (nesting accumulates over
  ~20 Wuzzuf pages + Indeed details). Idempotent today; add a
  `_no_load_patched` flag and early-return.

- [x] **D7 — C-speed JSON blob parsing** (S, perf)
  **2026-10-03:** Implemented: direct JSONDecoder.raw_decode. Local Python 3.14 fixture benchmark (median of five batches of 20): Wuzzuf 23.00 → 0.71 ms (32.3×); Indeed 17.53 → 1.06 ms (16.6×). These measure blob parsing, not total scrape time.

  Original finding:
  `boards/indeed/scraper.py:63-109` (`_extract_balanced_json`) and its twin
  `boards/wuzzuf/scraper.py:46-89` walk multi-MB HTML char-by-char in Python,
  ~11× per Indeed run (1 search + 10 details). Fix: replace brace-balancing
  with `json.JSONDecoder().raw_decode(html, idx=start)` — same semantics,
  ~10-50× faster.

- [x] **D8 — Batch SQLite writes** (S, perf)
  **2026-10-03:** Implemented: raw jobs, dedupe IDs and optional enrichment rows commit once per board. Each successful Telegram delivery is still acknowledged immediately to minimize duplicates after crashes.

  Original finding:
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

## E. Parser integrity, enrichment and error alerts (2026-10-03)

- Wuzzuf requirements were present in source HTML but discarded by `_enrich`.
  Production SQLite/source comparison confirmed this was an extraction omission,
  not evidence of an internet outage. Descriptions now retain both sections;
  salary details, education, keywords and work roles are also preserved.
- Indeed supports current `_rootProps.preloadedVJData`, older `_initialData`
  layouts and JobPosting JSON-LD. Description success no longer prevents metadata
  extraction. Failed detail requests no longer silently discard the search card.
- LinkedIn now retains available employer industry and about text. Its sampled
  live descriptions were intact. This was a sample audit, not exhaustive coverage.
- `core/scrape_health.py` checks missing structures, identities, descriptions and
  source-text preservation. New episodes alert the existing error channel with
  sanitized snapshot evidence; state and alert acknowledgements persist in
  `scrape_health`. Healthy checks reset only the checks actually observed.
  Optional salary/recruiter fields and all-seen pages do not trigger alarms.
  Follow-up: parser failures now mark runs `degraded`, preserving valid partial
  data. The 22:30 LinkedIn incident on 2026-10-03 showed only its startup shell
  after navigation/card timeouts; the 22:36 run recovered and saved three jobs.
  Alerts now identify that loading state and the failed operation explicitly.
  On 2026-10-04, added bounded search/detail recovery and transport/HTTP/JS
  diagnostics, including errors before a search callback ever executes. The
  earlier search-fetch incident logged ERR_NETWORK_CHANGED on LinkedIn and then
  Wuzzuf. Network failure and missing markup are separate diagnostic possibilities.
  An isolated live check injected one startup-shell response: the bounded reload
  recovered 25 cards and parsed a 2,949-character job description. The skipped
  detail ID 4473452654 now returns a different title/company in both LinkedIn's
  DOM and embedded job entity, so the original posting was not backfilled from
  that later response.
- Upgraded Scrapling 0.4.12 to 0.4.15 and Playwright/Patchright to 1.63.0 at the
  user's request. Upstream caps repeated Cloudflare solve attempts at three and
  fixes locale-sensitive challenge detection; this does not guarantee that a
  challenge will clear or that every internal wait has a wall-clock deadline.
  The existing CDP/default-context patch remains necessary. Isolated checks
  verified sync/async tab reuse, repeated setup and cookie continuity, followed
  by one full live description from each board (LinkedIn 2,479 characters,
  Wuzzuf 3,515, Indeed 4,564). Those live requests did not encounter a challenge;
  they verify compatibility, not a reproduced Cloudflare fix. No test messages
  or audit jobs were sent to production.
  Deployment at 11:19 Cairo time completed an all-healthy three-board run and
  saved/notified five new jobs. Backed up 38,227 jobs before replacement; rollback
  image is `rtjobs-scraper:before-scrapling-upgrade-20261004`. Routing stays off.
- The original `core/enrichment_worker.py` introduced an optional SQLite queue,
  22 Egypt industry routes, input/result caching, spend reservations, retries
  and explicit fallbacks. The industry contract is superseded by section F below.
  Three live preview calls returned valid results for $0.00205708 total; the
  second/third calls each reported 3,909 cached input tokens. No test Telegram
  messages were sent. Three samples are not an accuracy evaluation.
- Normal Telegram delivery now runs after Chrome closes. Analytics-only plotting
  dependencies moved to `requirements-analysis.txt` to reduce scraper image size.
- Installed Chrome 154.0.8037.92 differs from container Chrome 151.0.7922.108.
  CDP screenshots and screencast frames were healthy. A frontend mismatch/loading
  issue remains plausible, not proven; `scripts/inspect_chrome.py` prints a
  fresh installed-frontend URL as a workaround.

D9 remains open: pacing changes and browser stderr diagnostics need separate
validation. D10 remains deferred: multiple concurrent Chrome processes would
increase peak RAM. A separate enrichment worker adds no browser. Historical
missing requirements and capped Indeed details are not automatically backfilled.

Verification: `scripts/verify_offline.py` (including intentional markup changes
for all three boards), Python compile checks, container-runtime checks, live
LinkedIn/Indeed sample parsing, and migration on a copy of the 37,748-job DB.

## F. Professional-family classifier reset (2026-10-04)

- Implemented 25 professional families plus `other`, valid family-specific
  specializations, confidence/review flags and independent employer-sector metadata.
  Routing selects only `job_family`. Removed generic skills from the extraction
  schema and the global competencies chart from the local historical dashboard.
- Replaced the prompt/schema/taxonomy together. Strict jobs-array validation
  checks identities, cardinality, specialization, ranges and review invariants.
  Explicit title seniority is preserved rather than overwritten by experience.
- Added the separate, default-off `CLASSIFIED_DELIVERY_ENABLED` switch. Delivery
  startup rejects incomplete or duplicate channel mappings. All 26 actual IDs,
  names and bot posting permissions passed read-only Telegram checks, including
  the newly supplied General Management channel. See [audit](docs/channel-audit.md).
- Migrated a copy of the 38,651-job production database twice without changing
  raw rows, descriptions, notification counts or queuing historical jobs.
  Previous industry results are retained as obsolete; cache keys include the new
  contract so incompatible cached results cannot be reused.
- Added read-only SQLite analytics and a standalone family × sector dashboard.
  Aggregation streams compact rows without descriptions; output includes coverage,
  independent salary-bound benchmarks and explicit disclosure denominators.
  Provider failure fallbacks do not count as missing source facts.
- Offline checks pass. Live model checks cover 19 current curated cases and one
  saved job from each source. The initial management specialization assertion
  was clarified into separate general/country management cases; both passed.
  Total model cost across these checks was $0.007409575, with provider input-cache
  hits recorded. See [validation notes](docs/classifier-validation.md).
- During staging, enrichment and classified delivery stayed disabled. The working
  production scraper was not replaced and no backfill or test messages were sent.

## G. Repository cleanup and production activation (2026-10-04)

- The user subsequently authorized pushing the repository and activating both
  production classification and job-family channel delivery for newly scraped jobs.
- Deployment channel IDs moved to `TELEGRAM_CHANNELS_JSON` in private `.env`.
  Removed the checked-in channel-map asset and IDs from current/archived docs.
  Credentials use environment variables; API base URLs are configurable there too.
  Added a blank `.env.example` and `scripts/check_repository.py` for local checks.
- Minimized three large source captures to public job cards/fields, removing
  session/account/tracking data. Before/after parser output matched; regression
  checks passed. Generated exports moved under ignored `exports/`.
- Removed legacy `dataanalysis/` at the user's request and wrote a fresh
  [analytics guide](docs/ANALYTICS.md) for the new SQLite contract. Raw production
  jobs are preserved; historical jobs are not automatically reclassified.
- Production activation verified at 16:42–16:46 Cairo: migration preserved 38,794
  jobs; all three boards completed healthy; all 13 new jobs were classified and
  delivered to their expected destinations across 11 families. First-batch cost
  $0.00602995; 81,480 provider-cached input tokens. Worker and scheduler remain
  running. See [deployment record](docs/DEPLOYMENT.md).
