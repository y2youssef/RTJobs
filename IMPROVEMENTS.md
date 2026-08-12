# Improvements & Refactoring Plan

Status: **pending — agreed scope, not yet implemented.**
Generated from a full-repo audit on 2026-08-10. Effort: XS <15min, S <1h, M few hours.

## Key finding from the audit

The missing company names on LinkedIn are **NOT a selector problem**. The
`company_missing` snapshot shows the top card full of `ghost-company`
shimmer placeholders (19 ghost markers) — we scrape the detail panel before
LinkedIn hydrates the company name. Fix is a **wait**, not a selector change
(A1 below). The selector fallback added earlier stays as belt-and-braces.

## A. Correctness & robustness (do these first)

- [ ] **A1 — Company-name hydration wait** (S)
  `boards/linkedin/scraper.py::_scrape_card`: after the detail panel loads,
  `wait_for_selector(".job-details-jobs-unified-top-card__company-name:not(:empty)",
  timeout≈5s)` (proceed on timeout) before extracting fields. Verify with the
  existing fixture `markup/linkedin/snapshots/company_missing/*.html`.

- [ ] **A2 — Pin versions** (S)
  `requirements.txt`: freeze the working set (`pip freeze` from `.venv`),
  at minimum `scrapling[all]==0.4.12`, `playwright==…`, `patchright==…`.
  `docker-compose.yaml`: `mcuadros/ofelia:latest` → pinned tag/digest.
  Risk being pinned against: ofelia 0.4.x drops the leading-seconds cron
  field (scheduling silently dies), and scrapling internals are
  monkey-patched (`install_cdp_default_context_patch`, `patch_no_load_wait`)
  so an upgrade can silently break them. Optionally guard the patches with
  a scrapling version assert.

- [ ] **A3 — SIGTERM handling in `main.py`** (S)
  `docker stop` sends TERM (10s grace); handle it: stop the spider, close
  Chrome via `stop_chrome`, finish the run row. Root-cause fix for
  zombie-Chrome / stale `Singleton*` locks — then `kill_zombie_chrome` /
  `clean_locks` become a true safety net, not routine plumbing.

- [ ] **A4 — `WUZZUF_ENABLED` env toggle** (XS)
  `boards/wuzzuf/__init__.py` has `enabled = True` hardcoded; mirror
  LinkedIn's env-driven toggle for symmetry.

## B. Ops / repo hygiene

- [ ] **B1 — Delete legacy artifacts** (XS)
  Repo root: `example.html`, `login.html`, `linkedin_jobs.db`,
  `linkedin_jobs.json`, `jobs.json`, `dbtojson.py` (move to `scripts/` if
  still used), dev leftovers `chromeprofile/` (106MB) + `wuzzufprofile/`
  (20MB), and the wrong-page fixture
  `markup/linkedin/bug_not_reading_company_name_when_no_icon.html`
  (it captured the jobs HOME page, not a detail panel). All are untracked
  but still get `COPY . .`'d into the Docker image.

- [ ] **B2 — Dead code** (XS)
  `config.py:CHROME_ARGS` (unused since boards launch Chrome themselves);
  stale `--no-sandbox` comment in `Dockerfile`.

- [ ] **B3 — README refresh** (S)
  Fix the "7b" section numbering; CDP section: port mapping is gone
  (`network_mode: host` now); troubleshooting table: add xauth / init /
  HOME / Singleton-lock rows (or point at AGENTS.md).

- [ ] **B4 — healthchecks.io dead-man ping** (S)
  One HTTP call at the end of a successful `main.py` run + one env var
  (`HEALTHCHECK_URL`). Covers the silent "runs stopped entirely" case that
  Telegram failure alerts can't catch.

## C. Structure & code quality

- [ ] **C1 — pytest suite** (M)
  Promote the session's ad-hoc scripts into `tests/` (offline, fixtures in
  `markup/`): `_extract_state`, `_extract_jobs`, `sanitize_html`,
  blocklist matching, pagination URL builders, `login_state`, Telegram
  payload fallback, + the CDP cookie-persistence integration test.
  Best defense against "the site changed" regressions.

- [ ] **C2 — Dedup board boilerplate** (S)
  The save/filter/notify loop is copy-pasted in both boards →
  `boards/base.py: persist_and_notify(source, items)`.

- [ ] **C3 — `ChromeService` helper** (S)
  `core/browser.py`: bundle launch/stop/lock-cleanup/health-check; both
  boards duplicate the same ~5-line block.

- [ ] **C4 — Config validation at startup** (XS)
  `config.py` currently raises bare `KeyError` on missing env; list all
  missing vars in one friendly message.

- [ ] **C5 — `print` → `logging`** (S)
  Levels + one format; cleaner `docker logs ofelia` output.

## D. Open design decisions (owner's call)

- **Telegram batching**: 50+ new jobs = 50 messages + repeated 429 waits.
  Options: one digest message per run, per-run cap, or leave as-is.
- **Per-board schedule asymmetry**: both boards fire every 6 min; LinkedIn
  carries login-risk, so a slower cadence (e.g. 15–30 min) may be safer.
  Would need a second ofelia job targeting one board (main.py flag like
  `--only linkedin|wuzzuf`).
- **Upstream**: file a scrapling issue/PR so `cdp_url` connects reuse the
  browser's default context instead of `new_context()` — the only path to
  deleting `install_cdp_default_context_patch` entirely.
