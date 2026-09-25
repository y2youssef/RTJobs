# Job Categories Impact Analysis

Impact assessment only. It does not define a taxonomy, choose a classifier, create a migration, or implement Categories.

## Current reality

No implemented job-category concept exists. The system acquires from three boards, normalizes to an untyped dictionary, blocks companies, deduplicates by source ID, stores SQLite rows, and posts to one Telegram channel.

EXPAND.md proposes future country × department routing with keyword rules and OpenRouter fallback. That is not implemented. “Department” may overlap “Category,” so the concepts must be reconciled before code rather than creating parallel labels.

## Existing category-like concepts

| Location | Current purpose | Conflict/opportunity |
|---|---|---|
| LinkedIn scraper | job_type, workplace, location, hiring-manager role | Employment arrangement/location are metadata, not necessarily categories; possible inputs |
| Wuzzuf scraper | tags, career_level, workplace, work_types, experience_years | Rich inputs; raw tags mix skills/roles/arrangements and are not a canonical taxonomy |
| Indeed scraper | job_types, location, snippet, sponsored | Inputs/metadata; plural naming differs |
| selectors JSON | tags/location selector names | Acquisition config, not taxonomy storage |
| blocklist | company exclusion | Filtering, not categorization; category filtering must not inherit permanent mark-seen behavior accidentally |
| search URLs | Egypt scope and empty/general queries | Implicit geography, not classification |
| login.py _classify | login/feed/checkpoint page state | Name collision only; unrelated |
| EXPAND.md | planned department, rules/LLM, country routing | Direct overlap; proposal requiring confirmation |

No implemented category/categories, job_family, role_type, position_type, tech_stack, labels, topic, subscription, or job-classification module/config was found.

## Integration points

### Shared job contract

Current: all adapters emit the same top-level keys but no typed/validated model.

Effect: Category needs stable representation/cardinality across sources and consumers.

Likely surface: shared post-extraction boundary; contract docs/tests; extra or first-class storage.

Risk: High. Per-source ad-hoc keys would deepen drift.

### Source ingestion/parsing

Current: metadata and description quality differ. Indeed may be snippet-only; Wuzzuf discards parsed requirements; LinkedIn depends on hydrated detail.

Effect: classifier inputs/quality differ.

Likely surface: centralized classifier-input builder and quality flags. Source-native categories, if used, need explicit mapping.

Risk: Medium/high. Putting policy in each scraper duplicates behavior.

### Pipeline ordering

Current: source dedup happens early; company filtering immediately precedes save; save and notification are coupled.

Effect: must decide classification order relative to blocklist, persistence, dedup, notification, and failure.

Likely surface: persist_and_notify or a new service/workflow.

Risk: High. Failure must not lose jobs or cause repeated paid classification.

### SQLite

Current: four tables created inline; extra is JSON text; no schema version/migrations.

Effect: Categories require query/history/backfill/cardinality decisions.

Likely surface: extra, a jobs column, lookup table, join table, or event table plus indexes/migration.

Risk: High. Editing CREATE TABLE does not alter the existing production table.

### Migration/history

Current: no runner/version, backup/rollback procedure, or historical rewrite.

Effect: first-class Categories need existing-row handling and safe production volume migration.

Likely surface: backup, versioned migration, nullable/default/backfill/rollback tests.

Risk: High; this is the main technical prerequisite for schema-backed Categories.

### Filtering

Current: only company blocklist; blocked jobs are marked seen and dropped.

Effect: Categories may become inclusion/exclusion or subscription criteria.

Likely surface: separate filter stage/config/user model.

Risk: High if category mistakes permanently mark jobs seen. Classification metadata and destructive filtering must remain distinct.

### Deduplication

Current: exact source/external_id only.

Effect: Category should describe a job, not identity. Cross-source copies may disagree.

Likely surface: normally none for identity; tests must prove category edits do not duplicate jobs.

Risk: Medium if categorization is conflated with dedup.

### Telegram routing/formatting

Current: one jobs destination; no topics/routes. Message omits source-specific metadata.

Effect: future plan explicitly uses country × department channels; users may need category label/confidence.

Likely surface: validated route map, fallback, per-channel rate/backlog, formatting, tests.

Risk: High for routing: misconfiguration can drop/misroute. Existing unlimited backlog/synchronous retry complicates fan-out.

### Configuration

Current: env plus JSON selector/blocklist, minimal validation, no .env.example.

Effect: taxonomy/rules/thresholds/fallback/model/routing may be configurable.

Likely surface: validated config artifact; secrets separate from taxonomy; version/reload policy.

Risk: Medium/high. Large taxonomies/rules are fragile as environment JSON.

### Classification engine/external AI

Current: none. EXPAND proposes keyword rules and OpenRouter fallback/cache only.

Effect: automated assignment needs rules, manual input, source mapping, AI, or tiers.

Likely surface: new module/service, normalization, confidence/provenance/version, cache/budget/retries/metrics.

Risk: High. External AI adds cost, privacy, availability, nondeterminism, and replay/version issues.

### Run audit/failure handling

Current: runs records board status/count/error only.

Effect: operators need classified/uncategorized/fallback/error counts and scrape-vs-classifier health.

Likely surface: structured metrics and perhaps run schema/audit tables.

Risk: Medium. Classification outage should neither hide degradation nor necessarily discard scraped jobs.

### API/frontend/dashboard

Current: absent.

Effect: future category browsing/editing/admin/analytics needs a stable contract.

Likely surface: future only; avoid speculative UI/API in first slice.

Risk: scope expansion.

### CV matching/application flow/subscriptions

Current: absent.

Effect: Categories may eventually choose CV variants, ranking, queues, or user preferences.

Likely surface: future consumers should use stable IDs, not display strings.

Risk: premature downstream assumptions.

### Tests

Current: no automated suite.

Effect: Categories introduce business rules, migrations, and potentially routing.

Likely surface: offline unit/fixture/DB/migration/routing tests and opt-in live checks.

Risk: High; taxonomy/rule changes could silently reroute/suppress volume.

## Storage options (not decisions)

1. extra JSON
   - Minimal schema change/backward compatible.
   - Poor querying/indexing/constraints; easy inconsistent shapes; ad-hoc backfill.

2. Nullable category/category_id on jobs
   - Good for exactly one category and indexed routing.
   - Needs migration. Names couple data to labels; IDs need definitions.

3. Category lookup + category_id
   - Stable IDs and mutable metadata.
   - Still single-category; SQLite FK enforcement must be deliberate.

4. Category lookup + job_categories join
   - Multiple labels, primary marker, confidence/method/version.
   - Largest complexity; excessive if semantics are single-category.

5. Separate classification events
   - Best history/provenance/reclassification audit.
   - Requires a “current” projection and more operations.

SQLite has no native enum. Enum-like behavior must be application/config/lookup validation or an evolving CHECK constraint.

## Required decisions before implementation

1. Is Category identical to EXPAND Department?
2. One category, primary plus secondary, or many?
3. Fixed, config-managed, or DB-managed taxonomy?
4. Stable IDs/slugs or display names?
5. Flat or hierarchical?
6. Exact taxonomy and ownership/change process?
7. Rule, manual, source-native, AI, or tiered assignment?
8. Inputs: title only, company, full description, tags/requirements/location?
9. Behavior for missing/truncated descriptions?
10. Confidence, explanation, provenance, rule/model/taxonomy version?
11. Unknown/ambiguous: null, uncategorized, fallback/other, review?
12. Is classifier failure nonfatal, retryable, or run failure?
13. Classify before/after company filter and persistence?
14. Historical backfill: none, recent/active/unnotified, or all?
15. Metadata-only first, displayed, filtered, or immediate routing?
16. Routing fallback, missing map, fan-out, duplicate messages, backlog/rate policy?
17. User subscriptions now/later, requiring user/account data?
18. Future CV/ranking influence and long-lived ID stability?
19. Taxonomy/rule/model change version/audit process?
20. Accuracy/coverage threshold and labeled release evaluation set?

## Migration implications

- Operational DB likely lives in Docker scraper_data, not this checkout.
- CREATE TABLE IF NOT EXISTS will not add columns to existing tables.
- Before schema change: consistent DB backup, schema/user-version inspection, migration on a copy, rollback, then row/constraint verification.
- Historical rows should remain valid/null unless tested backfill is approved.
- Backfill must not trigger re-notification or alter source identity.
- Distinguish not-classified, failed, and old-taxonomy classification.
- Avoid copying mutable display names into many locations.

## Testing implications

Minimum layers:

- Taxonomy validation: unique IDs, valid parents, stable fallback.
- Deterministic rule cases, including Arabic/English normalization if in scope.
- Fixture-to-classifier input normalization and truncated/missing inputs.
- Cardinality/confidence/provenance contract.
- Migration from exact current four-table schema, idempotence, rollback, history behavior.
- Persistence update without duplicate jobs or re-notification.
- Route match, missing map, fallback, fan-out, rate/backlog behavior.
- Telegram escaping/formatting.
- Timeout/error/budget behavior with no real external calls.
- Labeled golden set and explicit quality thresholds.
- Opt-in live smoke only with explicit authorization and non-production Telegram destination.

## Risk priorities

P0 before feature coding:

- Resolve Category versus Department and cardinality.
- Decide metadata-only versus filtering/routing.
- Choose stable identity/storage and migration semantics.
- Define uncategorized/fallback so no valid job silently disappears.
- Preserve current code/reports in Git.

P1 during/around Categories:

- Add offline regression suite and labeled cases.
- Normalize classifier inputs.
- Mark incomplete Indeed descriptions; decide whether Wuzzuf requirements are retained.
- Record provenance/version/confidence if inference is used.
- Add routing validation/backlog controls before fan-out.
- Add migration/backfill/rollback tests.

P2:

- Frontend taxonomy admin, user subscriptions, CV selection, analytics, application tracking, multi-country/new boards, and cross-source fuzzy dedup remain outside the first slice unless separately scoped.

## Recommended implementation boundaries

After decisions:

- one documented category contract at the shared boundary;
- one centralized classifier/mapping service, not per-source policy;
- explicit uncategorized/fallback preserving every valid job;
- backward-compatible persistence and tested versioned migration if needed;
- no category-driven dropping in a metadata-only first phase;
- deterministic offline tests using current fixtures and a labeled set;
- no speculative API/dashboard/CV/application/multi-country/new-board/LLM work unless approved;
- multi-channel routing only after metadata accuracy and fallback are separately verified.

The cleanest conceptual insertion is after normalization and source-ID dedup, before notification. That does not force synchronous classify-before-save: durable classify-after-save may be safer if external inference/retries are required.