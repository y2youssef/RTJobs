# RTJobs analytics guide — job_family_v2

Use the current SQLite database as the source of truth. Scrapers preserve the
original posting in `jobs`; the enrichment worker saves one normalized result
in `job_enrichments`. The LLM extracts individual postings. Python and SQLite
calculate all counts, rates, distributions and trends.

The previous `dataanalysis/` extraction and department reports were removed.
Do not reuse their labels, inferred values or backfill scripts for this pipeline.

## 1. Keep profession and employer business separate

`classification.job_family` identifies the professional audience and selects the
Telegram channel. There are 25 named families plus `other`.
`classification.specialization` belongs to that family.
`classification.employer_sector` describes the employer or identified client and
is an independent analytics dimension. Missing sector stays null.

| Posting | Job family | Employer sector, when supported |
|---|---|---|
| Accountant at a software company | accounting_finance_banking | technology_telecom |
| Backend engineer at a bank | software_engineering | banking_finance_insurance |
| Medical sales representative | sales_business_development | healthcare_pharma |

The canonical values live in [taxonomy.json](../markup/enrichment/taxonomy.json).
The [schema](../markup/enrichment/schema.json) defines result fields; the
[prompt](../markup/enrichment/prompt.txt) defines extraction decisions. Never
derive employer sector from job family or use sector as a routing category.

## 2. Understand the tables and lifecycle

| Table | Purpose |
|---|---|
| `jobs` | Original title, employer, source, external ID, description, dates, URL and source `extra` JSON |
| `job_enrichments` | State, schema/model version, independent classification axes, normalized `result_json`, usage and errors |
| `enrichment_cache` | Reusable validated result keyed by relevant input content and model/contract version |
| `enrichment_spend` | Daily request reservations reconciled with reported model costs |
| `scrape_batches` | Start/end boundaries for one cycle across all enabled boards |
| `enrichment_requests` | One row per whole-batch request, submitted IDs and shared usage/cost |
| `pipeline_state` / `pipeline_alerts` | Worker heartbeats, provider pauses and deduplicated local alerts |
| `seen_ids` | Source-specific scrape deduplication |
| `runs` / `scrape_health` | Scraping outcomes and observed parser-health episodes |

New jobs enter `pending`, then become `ready` after whole-response validation.
Provider/network failures stay pending; exhausted budget waits until the next
local day. New failures never produce an Other classification. Historical
`fallback` rows remain auditable; undelivered current-schema fallbacks retry. Old
industry-schema results become `obsolete`; they are not silently reinterpreted.
Migration does not queue the historical corpus. Successful delivery updates
`jobs.notified` and `jobs.notified_at`; delivery status does not decide analytics eligibility.

For batch request costs, sum `enrichment_requests.usage_json` once per request.
Individual new enrichment rows reference the request ID and batch size; they do
not duplicate shared usage. Old per-job usage belongs to the pre-batch rollout.

For extracted market statistics, include only:

```sql
e.schema_version = 'job_family_v2' AND e.state = 'ready'
```

Count pending, fallback and obsolete rows separately. A provider fallback is not
evidence that the source omitted salary, education or another requirement.
`needs_review` is retained even in a valid ready result: report its share and
optionally compare a clearly labelled high-confidence subset.

## 3. Generate current reports

For a migrated local database or a consistent SQLite backup:

```bash
mkdir -p artifacts
.venv/bin/python scripts/report_enrichment.py \
  --db /path/to/rtjobs.db --output artifacts/report.json
```

For the standalone HTML charts, install the optional analysis dependencies in
the local environment, then add `--html`:

```bash
.venv/bin/pip install -r requirements-analysis.txt
.venv/bin/python scripts/report_enrichment.py \
  --db /path/to/rtjobs.db --output artifacts/report.json \
  --html artifacts/dashboard.html
```

The report opens SQLite read-only, streams compact rows and never sends LLM or
Telegram requests. Plotly is not installed in the production scraper image.
`artifacts/` and `exports/` are private, ignored output directories.

For JSON from the production worker's mounted database:

```bash
docker compose exec enrichment python scripts/report_enrichment.py \
  --db /data/rtjobs.db --output /tmp/analytics.json
docker compose cp enrichment:/tmp/analytics.json artifacts/report.json
```

Use SQLite's backup API for a consistent live backup; copying only the `.db` file
can omit uncheckpointed WAL data. Production timestamps are local Cairo time.
Run local reports with the same timezone, for example `TZ=Africa/Cairo`.

## 4. Current metrics and denominators

The report uses a rolling 30-day posting window. Within it, `windows` contains
15-minute, 1-hour, 24-hour, 7-day and 30-day raw/enriched counts.

- `coverage_percent`: ready current-schema jobs divided by all stored postings
  in the 30-day window. Historical unenriched rows lower coverage; they are not
  extraction failures.
- `distributions`: family, family-specific specialization, sector, family ×
  sector, seniority, daily seniority, experience bounds, work setup, governorate,
  tools, certifications, languages, posting entity and posting weekday/hour.
- `disclosure_rates`: explicit salary, work setup, education and experience
  counts, each with the number of ready enriched jobs as its denominator. A zero
  denominator produces null, not a claimed 0% disclosure rate.
- `unique_employers` and `employer_hiring_velocity`: raw-posting counts with
  employer names normalized for case/whitespace. They do not resolve subsidiaries,
  spelling variants or recruitment clients into legal entities.
- `salary_benchmarks`: percentiles of explicit advertised lower and upper bounds
  separately, grouped by currency, pay period and net/gross basis.

Time windows use `posted_at`, not scrape time or classification completion time.
Future/unsupported dates are excluded. Scrapers sometimes use observation time
when a source provides no usable posting date, so posting-hour analysis reflects
source precision and parser behavior as well as employer activity.

The same vacancy can appear on multiple boards or be reposted with a new source
ID. Counts represent stored postings, not guaranteed unique vacancies or hires.
Source coverage is also limited by access, pagination, detail caps and outages.

## 5. Read-only query examples

These queries cover all eligible stored results. Add a local-time `posted_at`
filter by joining `jobs` if a specific date window is needed.

Coverage by state and contract:

```sql
SELECT schema_version, state, COUNT(*) AS jobs
FROM job_enrichments
GROUP BY schema_version, state;
```

Profession × employer business:

```sql
SELECT job_family, employer_sector, COUNT(*) AS jobs
FROM job_enrichments
WHERE schema_version = 'job_family_v2' AND state = 'ready'
GROUP BY job_family, employer_sector
ORDER BY jobs DESC;
```

Tool demand, counted once per job and normalized for case:

```sql
SELECT lower(tool.value) AS technology, COUNT(DISTINCT e.job_id) AS jobs
FROM job_enrichments AS e,
     json_each(e.result_json, '$.requirements.tools_and_technologies') AS tool
WHERE e.schema_version = 'job_family_v2' AND e.state = 'ready'
GROUP BY lower(tool.value)
ORDER BY jobs DESC;
```

Jobs needing review:

```sql
SELECT j.id, j.source, j.title, e.job_family, e.routing_confidence
FROM jobs AS j JOIN job_enrichments AS e ON e.job_id = j.id
WHERE e.schema_version = 'job_family_v2'
  AND e.state = 'ready' AND e.needs_review = 1;
```

## 6. Interpretation rules

Preserve explicit title seniority even when the experience range is low. Keep
fractional experience years. Never assume a salary currency from location, a
monthly salary period, on-site work, a degree, a language or a certification.
Null means unsupported; it is not the same as false, zero or a negative condition.

Compare salary only within compatible currency/period/basis groups and show `n`.
The current report does not convert currencies, annualize salaries or estimate
midpoints. Review sample size and coverage before interpreting a percentile.

Use tools/technologies and formal certifications for demand charts. Do not
reintroduce generic hard-skills, soft-skills or Top Competencies rankings.
Candidate gender/age/military restrictions are explicit-posting metadata for
aggregate bias analysis; they do not affect job routing.

Always label date range, coverage, review rate and sample count on shared
reports. The HTML dashboard currently visualizes the family × sector matrix,
tools and certifications; the JSON report provides the additional metrics.

## 7. Changing the contract later

Update prompt, schema, taxonomy, validation and evaluation fixtures together.
Cache keys incorporate the contract, so incompatible old output is not reused.
Make any historical reclassification an explicit, budgeted operation; preserve
raw job evidence and never reset notification state just to refresh analytics.

Run `.venv/bin/python scripts/verify_offline.py` after analytics changes. It checks
window boundaries, independent axes, coverage/denominators, fallback exclusion,
distinct tool counts and compatible salary groups in temporary databases.
