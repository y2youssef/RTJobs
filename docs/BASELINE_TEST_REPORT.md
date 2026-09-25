# RTJobs Baseline Test Report

Baseline: 2026-09-25, Africa/Cairo. Validation was offline/non-destructive. No scraper, browser, Telegram/healthcheck request, production DB, profile change, dependency install, Docker build, branch, commit, or push occurred.

## Environment

| Item | Observed |
|---|---|
| Host | Windows PowerShell |
| Python | 3.14.5 |
| pip | 26.1.1 |
| Docker CLI | 27.3.1 |
| Compose CLI | 2.29.7 |
| Docker daemon | Unavailable/not running |
| Local .venv / .env | Absent / absent |
| Present relevant packages | requests 2.34.2, python-dotenv 1.2.2 |
| Missing runtime packages | Scrapling, Playwright, Patchright |
| Missing analysis packages | matplotlib, seaborn, plotly |
| Missing QA tools | pytest, ruff, mypy |

Docker targets Python 3.13; guidance mentions local Python 3.14. Syntax passed on 3.14.5. Browser/runtime operation on this bare host was not established.

## Results

Executed validation summary: 4 check groups passed (syntax plus three core groups), 0 assertion failures, 2 fixture checks blocked before assertions, and 0 skipped. The repository's automated test count is 0 passed / 0 failed / 0 skipped because no suite exists.

    Automated tests:       NOT CONFIGURED (no tests directory/config)
    Offline fixture tests: BLOCKED (Scrapling missing before parser execution)
    Python syntax:         PASS (21/21 Python files parsed with ast.parse)
    Core checks:           PASS (sanitizer/blocklist, SQLite, Telegram fallback)
    Lint:                  NOT CONFIGURED / NOT RUN (ruff absent)
    Type check:            NOT CONFIGURED / NOT RUN (mypy absent)
    Format check:          NOT CONFIGURED
    Application start:     NOT RUN (would scrape/persist/notify)
    Compose validation:    BLOCKED (.env absent)
    Docker build:          NOT RUN (would install/download; daemon unavailable)

No test assertion failed. Wuzzuf/Indeed parser attempts ended with ModuleNotFoundError: scrapling, an environment/dependency issue rather than a demonstrated parser defect.

## Evidence

### Repository/Git checks

- Initial git status was clean.
- Branch/upstream comparisons were all 0/0.
- git fsck exited 0 with no output.

### Syntax

A read/parse-only pathlib + ast.parse loop checked all 21 Python files. It generated no pycache. Result: AST_OK files=21. This proves grammar validity only, not import/runtime correctness.

### Core offline checks

Sanitizer/blocklist:

- A void meta tag did not swallow the body.
- Script content was removed.
- Exact =Turing blocked Turing, not Manufacturing.
- Substring Alignerr blocked Alignerr Inc.

SQLite schema in memory:

- Created jobs, login_state, runs, seen_ids.
- Duplicate (source, external_id) with INSERT OR IGNORE left one job.
- No explicit secondary indexes exist (unique indexes are SQLite-internal).

Telegram with locally faked HTTP/sleep:

- First response simulated 400 with MarkdownV2.
- Retry simulated 200.
- Retry payload omitted parse_mode as required.
- No network call occurred.

### Fixture parser attempts

Tracked fixtures:

- markup/wuzzuf/wazzuf_guide.txt
- markup/indeed/first_page.html
- markup/indeed/newjob_sample.html
- markup/linkedin/manual_login_en.html
- markup/linkedin/manual_login_ar.html

Documented Wuzzuf/Indeed checks were attempted with dummy env values, but source module import requires Scrapling. Both stopped before assertions. This session did not reconfirm 15 Wuzzuf entities/jobs or 15 Indeed cards/detail extraction. AGENTS documentation reports prior verification, but that is historical evidence, not a current result.

### Docker

docker compose config --quiet stopped because required env_file .env is absent. This does not establish invalid YAML. Docker daemon inspection failed because the Windows Docker pipe was unavailable. No existing image could be used for isolated fixture tests.

## Intended commands and safety

| Command | Purpose | This assessment |
|---|---|---|
| python -m venv .venv | environment | Not run; creates files |
| pip install -r requirements.txt | dependencies | Not run by instruction |
| scrapling install | browser support | Not run by instruction |
| python main.py | DB + scrape + Telegram | Not run; external/production effects |
| python main.py --reset-login | reset cooldown | Not run; DB mutation |
| docker compose up -d --build | build/schedule live work | Not run |
| docker compose logs -f scraper | logs | Not useful without daemon/container |
| python scripts/dbtojson.py | export DB | Not run; reads DB/overwrites jobs.json |
| pytest / ruff / mypy | QA | No configuration/dependencies |

## Existing test architecture

There is no automated test architecture. Current assurance consists of captured fixtures, ad-hoc snippets in AGENTS.md, pure extraction helpers, runtime diagnostic snapshots, historical operational notes, and an open IMPROVEMENTS.md task to add pytest.

No unit, integration, end-to-end, database, Telegram, parser-regression, or CI tests are tracked. There are no mocks/golden assertions. Production selector JSON doubles as runtime config; captured HTML is the only fixture corpus.

## Coverage assessment

Testable but unautomated:

- Wuzzuf state/job/timestamp/HTML extraction.
- Indeed balanced JSON, search/detail, JSON-LD fallback.
- LinkedIn header/time parsing and login selectors.
- Sanitizer/blocklist.
- SQLite insert/dedup/unnotified behavior.
- Telegram escaping/retries/marking.
- Login retry/cooldown with fake DB/clock.

Weak/absent:

- Source-specific extra contracts.
- persist_and_notify and its attempted-save count.
- Pagination URL/stop rules.
- Chrome launch audit failure (known D1).
- CDP cookie/profile behavior, Cloudflare, login/checkpoint, signals, scheduler, Docker startup.
- Current-site selector smoke checks.
- Any default-safe test isolation from real Telegram/DB/profiles.

## Failure classification

| Finding | Classification |
|---|---|
| Missing Scrapling/Playwright/Patchright | Environment/dependency |
| .env absent; Compose cannot resolve env_file | Environment/config |
| Docker daemon unavailable | Environment |
| No test/lint/type setup | Repository assurance gap |
| Fixture assertions not reached | Environment consequence, not proven defect |
| D1-D10 in IMPROVEMENTS | Pre-existing documented debt |

## Recommended regression baseline before Categories

1. Add an offline pytest suite with dummy config and temporary/in-memory SQLite.
2. Lock Wuzzuf/Indeed fixture counts and representative fields.
3. Test LinkedIn header/time parsing and EN/AR login selectors.
4. Assert shared job keys and all source-specific extra keys.
5. Test blocklist global/source/substring/exact cases.
6. Test save_job, seen_ids, uniqueness, unnotified ordering, notification marking.
7. Test Telegram escaping, 400 fallback, 429 behavior, failed-send persistence.
8. Test persist_and_notify using fake DB/Telegram.
9. Test pagination stop/URL logic and known failure paths.
10. Add migration tests from the exact current four-table schema before production changes.
11. Add Categories contract/taxonomy/classification/fallback/persistence/routing tests.
12. Keep live browser tests opt-in with separate credentials and non-production outputs.

Suggested future commands once intentionally configured:

    python -m pytest -q
    python -m pytest -q tests/unit tests/fixtures
    python -m pytest -q -m integration

The default suite must never send Telegram messages or touch operational DB/profile paths.