# RTJobs classification reset — job_family_v2

The 2026-10-04 professional-family specification replaces the previous industry/department classifier. There are **25 named job families plus `other`: 26 canonical categories**. Employer sector is a separate analytics axis and never selects a Telegram channel. The old plan is preserved only in [the archive](docs/archive/EXPAND-industry-v1.md).

## Current authorization

The user authorized production classification and job-family channel delivery on 2026-10-04, following the disabled staging tests. Both flags remain false by default in the public template; the production `.env` explicitly enables them. Do not reclassify the historical corpus implicitly.

## Source of truth

- [Prompt](markup/enrichment/prompt.txt): extraction, professional boundaries, evidence precedence and no-fabrication rules.
- [Taxonomy](markup/enrichment/taxonomy.json): families, valid specializations and analytics-only employer sectors.
- [Strict output schema](markup/enrichment/schema.json): `{ "jobs": [...] }`, exactly one matching result per input job.
- `TELEGRAM_CHANNELS_JSON` in private `.env`: canonical job-family key to Telegram ID, with no industry or country composite keys. The [environment template](.env.example) lists all 26 keys with empty values.
- [Channel sanity check](docs/channel-audit.md): actual titles, IDs and posting permissions.

## Egypt channels

Expected Telegram titles start with RTJobs and end with 🇪🇬. The configured bot is @suggestmeabotnamebot. IDs were checked with getChat and getChatMember; no test messages were sent.

Sales / Business Development: [configured in .env]
Accounting / Finance / Banking: [configured in .env]
Customer Service / Call Center: [configured in .env]
Human Resources / Recruitment: [configured in .env]
Marketing / E-commerce: [configured in .env]
Software Engineering: [configured in .env]
Data / AI / Analytics: [configured in .env]
IT / Cloud / Cybersecurity: [configured in .env]
Engineering / Construction: [configured in .env]
Industrial / Manufacturing / Maintenance: [configured in .env]
Supply Chain / Procurement / Logistics: [configured in .env]
Operations / Projects / Quality: [configured in .env]
Administration / Office Support: [configured in .env]
Design / Creative: [configured in .env]
Content / Media / Communications: [configured in .env]
Healthcare / Medical: [configured in .env]
Education / Training: [configured in .env]
Legal / Compliance / Risk: [configured in .env]
Product / Business Analysis: [configured in .env]
Hospitality / Tourism / Food Service: [configured in .env]
Retail / Store Operations: [configured in .env]
Safety / Security / Facilities: [configured in .env]
Science / Research: [configured in .env]
Consulting / Strategy: [configured in .env]
General Management: [configured in .env]
Other / Unclassified: [configured in .env]

## Classification contract

Route by `classification.job_family` only. Specialization must belong to that family. Low confidence and `other` require `needs_review=true`; `other` also requires Low confidence. Explicit contradictory evidence is flagged. Review flags are retained for inspection, and no posting is silently discarded.

Raw fields stay in `jobs`; normalized enrichment stays in `job_enrichments` in the same SQLite database. The API processes one job per call inside the requested jobs-array envelope, avoiding cross-job contamination. Batch validation checks identity/cardinality and accepts reordered valid IDs without mixing results.

Explicit title seniority wins over experience. Salary, currency, work setup, languages, technologies, certifications and candidate restrictions are extracted only when supported. No hard_skills, soft_skills, domain_skills or global competencies ranking is produced. `posting_entity_type=unknown` is a valid enum; missing factual scalars use null.

The schema and prompt change the result-cache namespace. Legacy industry results remain auditable as obsolete and are not eligible for family delivery. A migration does not enqueue old jobs. Failed model calls retry with bounded backoff and eventually create explicit Other/Low/review fallbacks, which are not cached as successful enrichment.

## Analytics outside the LLM

`core/analytics.py` computes rolling 15-minute/1-hour/24-hour/7-day/30-day counts, family/specialization/sector demand and the family × sector matrix, seniority trends, experience distributions, work setup, governorates, tools, certifications, languages, posting patterns, employer velocity, unique employers, posting-entity shares and disclosure rates.

Salary percentiles compare explicit lower and upper bounds separately, grouped by currency, period and net/gross basis; no inferred salary, currency conversion or period conversion. Reports expose enrichment coverage and exclude provider-fallback results from extraction/disclosure denominators. Employer counts normalize case/whitespace, not legal entity identities.

`scripts/report_enrichment.py --db PATH --output REPORT.json --html DASHBOARD.html` reads a migrated database without writes. The standalone dashboard shows family × sector, named tools and certifications. It needs the optional analysis dependencies for HTML charts. The legacy `dataanalysis/` directory was removed at the user's request; current reports start from the new enrichment contract.

## Verification

Run `.venv/bin/python scripts/verify_offline.py` for parser, schema, migration, cache, family-routing, delivery-off and analytics checks. Run `.venv/bin/python scripts/check_channels.py` for a fresh read-only channel audit. Opt-in paid fixtures: `.venv/bin/python scripts/evaluate_classifier.py --live --output /tmp/classifier-eval.json`.

All 26 channels passed the read-only check on 2026-10-04, including General Management. Rerun the checker before deployment. Classified delivery requires both enable flags and a complete environment channel map with unique IDs.
