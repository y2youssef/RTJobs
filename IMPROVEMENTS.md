# Improvements & Refactoring Plan

Status: **done 2026-09-04 — all S/XS items shipped (A1-A4, B1-B4, C2-C5); only C1 (pytest, M) remains as future work.**
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
