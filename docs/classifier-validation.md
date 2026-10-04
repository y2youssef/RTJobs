# Job-family v2 validation — 2026-10-04

During staging, implementation and testing completed with enrichment and classified
delivery disabled. The subsequent cleanup/production request authorizes both;
see the deployment notes for its outcome. During these tests the production scraper
and its schedule were not replaced. All 26
channel IDs, titles and bot posting permissions passed the [read-only audit](channel-audit.md).

## Contract and persistence

`scripts/verify_offline.py` covers all 26 family destinations, family/sector
independence, valid specializations, title seniority preservation, fractional
experience, missing fields, exact batch IDs, result-cache isolation, retry/budget
fallbacks, migration, delivery disabled, malformed mappings and analytics. Existing
LinkedIn/Wuzzuf/Indeed parsing, browser and alert regression checks also pass.

The same suite passed inside the separately built Python 3.13 image
`rtjobs-scraper:job-family-v2` (`138afff0b340`), with networking disabled and no
production volumes mounted. `pip check` found no broken requirements. The local
Python 3.14 compile checks and `git diff --check` passed too. The test image was
not assigned to the scheduled production container.

A SQLite backup of production contained 38,651 raw jobs. Two migration passes
preserved job count, total description length, notification count and queue size;
SQLite quick_check passed. Three existing jobs were then explicitly queued only
in that disposable copy. Production historical jobs were not queued or rewritten.

## Live model checks

Model: `openai/gpt-6-luna` via the configured OpenRouter key.
Prompt/schema/taxonomy version: `cd32295f1c18c612`.

The initial 18-case run chose the intended job family for every case. It passed
17/18 exact assertions. The remaining assertion expected a generic management
specialization despite the fixture explicitly describing country P&L ownership;
the returned `country_regional_management` was consistent with that evidence.
The fixture was split into an unambiguous General Manager case and a Country
Manager case. Both targeted follow-up checks passed. The current 19-case fixture
set is therefore covered by 17 unchanged passing cases plus those two checks.

Coverage includes cross-sector professions, medical/property/technical sales,
DevOps, HSE, construction versus factory maintenance, recruitment and outsourcing,
explicit title seniority, fractional experience, tools versus generic skills,
language requirement/proficiency and unspecified salary currency/period.

Three actual saved postings also passed strict schema validation:

| Source / posting | Job family | Specialization | Employer sector |
|---|---|---|---|
| LinkedIn / Sales Engineer | sales_business_development | technical_presales | manufacturing_industrial |
| Wuzzuf / Senior Accountant | accounting_finance_banking | general_accounting | real_estate_property |
| Indeed / Senior Shopify Developer | software_engineering | frontend | fmcg_consumer_goods |

These are targeted checks, not an accuracy estimate for the full corpus. The
three-row analytics preview is explicitly labelled with its low coverage.

The read-only report scanned 29,186 postings within its 30-day window in 0.891s
with 10.31 MiB peak Python allocations under tracemalloc. The database file's
modification time stayed unchanged. Only three rows had v2 enrichment, so this
measurement does not predict memory use for a fully enriched corpus or include
the optional Plotly HTML rendering.

Provider-reported costs: $0.0052882 initial fixtures, $0.000483275 management
follow-up, $0.0016381 real-posting previews; total **$0.007409575**. The initial
fixture run reported 115,430 cached input tokens. Cache hits depend on the provider;
the SQLite result cache is independent and was verified offline.

No Telegram messages were sent by these checks. Channel checks call only getMe,
getChat and getChatMember, so they do not consume Indeed login-code updates.

## Local artifacts

- `/tmp/rtjobs-job-family-channel-check.json`
- `/tmp/rtjobs-job-family-evaluation.json`
- `/tmp/rtjobs-management-evaluation.json`
- `/tmp/rtjobs-family-validation-7084ag_c/live-preview.json`
- `/tmp/rtjobs-family-validation-7084ag_c/analytics.json`
- `/tmp/rtjobs-family-validation-7084ag_c/dashboard.html`
- `/tmp/rtjobs-family-offline.log`
- `/tmp/rtjobs-family-container-check.log`
- `/tmp/rtjobs-family-validation-7084ag_c/analytics-check.json`

Raw database copies and per-posting reports stay outside version control.
