# RTJobs Repository Handover

Baseline: 2026-09-25, Africa/Cairo. This describes the repository as inspected, not the broader intended LT Jobs product.

## Executive status

- The repository calls itself RTJobs, not LT Jobs.
- It is a Python job-board scraper and Telegram notifier, not a general intake, ranking, CV-matching, application-routing/tracking, API, or dashboard platform.
- Implemented sources: LinkedIn, Wuzzuf, Indeed.
- Enabled boards run sequentially in one process. SQLite is the durable store. Jobs go individually to one Telegram jobs channel.
- The shared job contract is an untyped dictionary.

## Git state at reconnaissance start

| Item | Observed |
|---|---|
| Current branch | reconnaissance |
| HEAD | d2dcb1be8a43cabcbcafc6daa2fda32aaf6127d4 |
| Subject | linkedin detail header parsing, exact-match blocklist, audit section D, expand plan |
| Initial tree | Clean; no staged, unstaged, untracked, or present ignored operational files |
| Local branches | main, reconnaissance |
| Remote refs | origin/main, origin/reconnaissance; origin/HEAD -> origin/main |
| Tracking/divergence | Same-named upstreams; all comparisons 0/0 |
| Tags | None |
| Remote | origin -> https://github.com/y2youssef/RTJobs.git |
| Unmerged/local-only work | None found; every observed branch ref equals HEAD |
| Merge history | None in the five-commit history |
| Integrity | git fsck reported no issues |

The checkout was cloned and switched from main to reconnaissance on 2026-09-25. No fetch was performed, so origin statements refer to clone-time remote-tracking refs.

    d2dcb1b linkedin detail header parsing, exact-match blocklist, audit section D, expand plan
    b2c3da9 improvements batch: hydration fix, pinning, SIGTERM, logging + uncapped DB
    1cff9f9 added indeed
    ca46ced fixed cdp
    c6e80b6 RTJobs: headful LinkedIn + Wuzzuf job scraper

At assessment start, reconnaissance and main were equally current. HEAD is safe as a code preservation point. These new reports are intentionally uncommitted and must be preserved before switching if they should be in the baseline.

## Repository map

    main.py                     sequential orchestrator; --reset-login
    config.py                   environment configuration
    requirements.txt            pinned Python dependencies
    Dockerfile                  Python 3.13 + Chrome + Xvfb/xauth
    docker-compose.yaml         one-shot scraper + six-minute ofelia scheduler
    README.md / AGENTS.md       operator and implementation guidance
    INDEED.md                   Indeed reverse-engineering notes
    IMPROVEMENTS.md             shipped work plus unresolved D1-D10
    EXPAND.md                   future classifier/country/channel proposal
    boards/base.py              ABC, selector loading, persist-and-notify
    boards/linkedin/            login state machine + detail spider
    boards/wuzzuf/              DOM + embedded SSR-state spider
    boards/indeed/              embedded JSON search/detail spider
    core/browser.py             Chrome/CDP lifecycle and Scrapling patches
    core/db.py                  SQLite schema/persistence
    core/telegram.py            job/failure notifications
    core/blocklist.py           company filter
    core/markup.py              sanitized snapshots
    core/login_state.py         LinkedIn retry/cooldown
    core/human.py               human-like delays
    core/log.py                 stdout + rotating logging
    markup/                     selectors, blocklist, captured fixtures
    scripts/dbtojson.py         manual jobs JSON export
    docs/                       reconnaissance reports

Absent: tests, migrations, package metadata, frontend, API, queue, CI, and .env.example. Ignored operational paths include .env, databases, browser profiles, logs, snapshots, and experiments; none was present.

## Stack

| Area | Actual implementation |
|---|---|
| Runtime | Python; Docker 3.13, inspected host 3.14.5 |
| Scraping/browser | Scrapling 0.4.12, Playwright/Patchright 1.61.0, real Chrome/CDP |
| HTTP | Requests 2.34.2 |
| Database | sqlite3; no ORM or migrations |
| Config | python-dotenv + os.environ |
| Scheduler | ofelia 0.3.22 every six minutes |
| Container | Compose, Xvfb/xauth, init:true/tini |
| Logging | stdlib stdout + rotating file |
| Parsing | selectors, regex, JSON/brace balancing, HTMLParser |
| Telegram | Direct Bot API; no SDK |
| Analysis-only | matplotlib/seaborn/plotly; no tracked consumer |
| QA | No framework/linter/formatter/type checker |
| Queue/cache/AI/CI | None |

## Entry points

1. python main.py initializes logging/signals/DB and runs enabled boards.
2. python main.py --reset-login mutates LinkedIn retry state and exits.
3. Docker runs xvfb-run -a python main.py; ofelia starts the exited container every six minutes.
4. python scripts/dbtojson.py manually exports rtjobs.db to jobs.json.

No bot poller, server, worker, source-specific CLI, or development server exists.

## Job lifecycle

    main.py
      -> enabled JobBoard.run()
      -> runs audit + source Chrome profile/CDP
      -> Scrapling discovery/parser
      -> shared job dictionary
      -> exact source/external_id seen check
      -> persist_and_notify
           -> company blocklist
           -> jobs + seen_ids
           -> all unnotified source rows
           -> Telegram per row
           -> notified=1 on success
      -> finalize runs

Shared behavior:

- db.init_db creates missing objects. A missing selector file exits startup.
- Spiders load all seen IDs. LinkedIn/Wuzzuf stop pagination after duplicates.
- Blocked companies are marked seen but never saved/notified.
- save_job uses INSERT OR IGNORE and one connection per job.
- persist_and_notify increments new_count even if insertion was ignored; it counts attempted accepted items, not verified inserts.
- All unnotified rows for a source are sent oldest-first by text posted_at.
- Suspicious pages become sanitized ignored snapshots; failures alert when possible.

LinkedIn checks cooldown/profile state, uses a login session with manual CDP checkpoint wait, then scans pages of 25 up to start 75. It clicks new cards, waits for hydrated detail data, and extracts description/header/hiring-manager data. Login redirects abort.

Wuzzuf uses Cloudflare solving and persistent profile. DOM supplies order/IDs; window.Wuzzuf.initialStoreState.job.collection supplies rich data. It paginates start=0..19 and stops at a seen ID.

Indeed uses Cloudflare solving and one sort-by-date page. Cards come from window.mosaic.providerData. Up to ten new jobs fetch viewjob details from window._initialData with JSON-LD fallback; later jobs retain an unmarked snippet-only description.

## Source inventory

| Source | Files | Approach / data | Default state | Dedup and failures | Evidence/status |
|---|---|---|---|---|---|
| LinkedIn | boards/linkedin/login.py, scraper.py, __init__.py | Login-gated, persistent Chrome profile, CDP/manual 2FA; cards plus hydrated detail panel | Enabled | external job ID; duplicate stops pages; login cooldown/checkpoint/redirect alerts and snapshots | Implemented; repository docs claim prior live operation, not live-tested this session |
| Wuzzuf | boards/wuzzuf/scraper.py, __init__.py | Cloudflare-solving browser; DOM order plus embedded SSR collection; up to 20 pages | Enabled | numeric slug ID; duplicate stops pages; empty/failed snapshots and board alert | Implemented; tracked fixture exists, current fixture execution blocked by missing Scrapling |
| Indeed | boards/indeed/scraper.py, __init__.py | Cloudflare-solving browser; one embedded-JSON search page plus up to 10 new detail pages | Disabled by config default | jobkey; no pagination; challenge/empty/detail snapshots and board alert | Implemented; AGENTS claims prior live use with env override, current fixture execution blocked |
| Telegram/company sites/other boards | None | No ingestion adapter | Absent | N/A | Planned/broader context only |

There are no partially implemented fourth sources. NaukriGulf and Bayt appear only in EXPAND.md as future disabled-board proposals. Telegram is output-only, not a source.

## Job model

No typed model exists. All sources emit source, external_id, title, company, posted_at, description, link, extra, scraped_at. SQLite stores the first two as NOT NULL, all fields as TEXT except integer id/notified, and extra as JSON text. Timestamps are local strings; source output is usually minute precision while DB fallback has seconds.

Extra fields:

- LinkedIn: hiring_manager_name, hiring_manager_role, detail_location, workplace, job_type.
- Wuzzuf: location, tags, salary, career_level, workplace, work_types, experience_years, vacancies, expire_at.
- Indeed: location, snippet, salary object, job_types, sponsored, latitude, longitude.

Wuzzuf parses requirements but discards them. Shapes/names differ across sources (job_type/job_types/work_types, detail_location/location, salary string/object).

## Storage and deduplication

core/db.py creates:

- jobs: unique (source, external_id), shared fields, extra, notified.
- seen_ids: primary key (source, external_id), never pruned.
- runs: source/status/count/error/timestamps.
- login_state: key/value retry state.

No explicit secondary indexes, foreign keys, users, categories, CVs, applications, or schema versions exist. Dedup is exact/source-local only: no URL, title/company, hash, fuzzy, or cross-source matching.

## Filtering/classification

Only company blocking exists: JSON global/per-source entries, normalized case-insensitive substring match, with =name exact match. Blocked jobs are permanently marked seen.

No role, keyword, seniority, experience, location, subscription, user, or category filters exist. Egypt search URLs give implicit geography. Wuzzuf/Indeed tags/job types are metadata only.

EXPAND.md proposes department classification and country/department routing. Its proposed modules, cache, OpenRouter integration, env names, and services do not exist. login.py _classify concerns login-page state, not jobs.

## Telegram/output

One jobs chat and one optional failure chat. Job messages show title, company, time, optional LinkedIn manager, and link. MarkdownV2 is escaped; 400 retries once without parse_mode; 429 honors retry_after. Success sets notified. No category/source/role/location routing, topics, queue, batch cap, or multi-channel map exists.

## Configuration

Observed names only; values/secrets were not inspected:

- Runtime: DATA_DIR, MARKUP_DIR, profile dirs, HEADLESS, CHROME_DEBUG_PORT, KILL_CHROME_ON_START, TZ.
- Sources: LINKEDIN_ENABLED/EMAIL/PASSWORD, CHECKPOINT_WAIT_SECONDS, MAX_LOGIN_RETRIES, WUZZUF_ENABLED/SEARCH_URL, INDEED_ENABLED/SEARCH_URL.
- Output: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_TEST_ID, TELEGRAM_FAILURE_CHAT_ID, HEALTHCHECK_URL.
- Diagnostics: MAX_SNAPSHOTS_PER_KIND, LOG_LEVEL, LOG_FILE.

Only Telegram token/jobs chat are import-validated. README wording calls more values required than code does. LinkedIn search is hard-coded; Wuzzuf/Indeed are overridable. Selectors/blocklist are bind-mounted JSON. Categories could use JSON/env for a small installation, but no structured config validation/versioning exists.

## Install/run

    python -m venv .venv
    activate environment
    pip install -r requirements.txt
    scrapling install
    python main.py
    docker compose up -d --build
    docker compose logs -f scraper

No separate build/lint/type/format/test command exists. main.py scrapes/persists/notifies; --reset-login mutates DB; dbtojson overwrites output, so none is a harmless validation command.

## Documentation discrepancies/maturity

- Requested name LT Jobs conflicts with repository name RTJobs.
- README architecture omits Indeed, blocklist, browser, and logging modules.
- README requirement wording differs from import validation.
- README volume summary omits actual Wuzzuf/Indeed profile volumes.
- INDEED.md retains a “To verify” heading despite implementation/fixtures and historical live claims.
- EXPAND.md is future intent, not implemented architecture.
- requirements references analysis use with no tracked consumer.

Maturity: operational scraper with browser hardening and weak automated assurance. Broader LT Jobs layers are absent.

## Debt/risk

P0:

- No corrupt repository state or mandatory code repair was found.
- Preserve reports and settle Category versus Department, cardinality, storage/migration, and fallback semantics before coding.

P1:

- No automated suite; parser fixture imports blocked here by missing Scrapling/Playwright.
- No migration/versioning mechanism for persistent SQLite.
- Untyped/inconsistent extra.
- Known D1: Chrome launch can leave runs stuck at running.
- Unbounded seen_ids, no query indexes, per-row writes, unlimited notification backlog.
- Indeed truncated descriptions unmarked.
- Attempted-save count may overstate inserts.
- No cross-source dedup.

P2: documentation cleanup, nonessential metadata normalization, duplicate brace-parser cleanup, unused analysis dependencies.

## Preservation recommendation

No branch/tag/commit/switch was executed. Review and preserve the reports first:

    git status --short --branch
    git add docs/REPO_HANDOVER.md docs/BASELINE_TEST_REPORT.md docs/CATEGORIES_IMPACT_ANALYSIS.md
    git commit -m "docs: capture pre-categories baseline"
    git branch archive/pre-categories
    git tag -a pre-categories-baseline -m "Pre-categories baseline"
    git switch -c feature/job-categories

This includes reports in the baseline. If reports must remain uncommitted, point branch/tag explicitly at d2dcb1be8a43cabcbcafc6daa2fda32aaf6127d4; the untracked reports would not be protected.