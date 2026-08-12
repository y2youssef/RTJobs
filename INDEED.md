# INDEED.md — Indeed board scraping notes

Strategy: poll a single search URL (sorted by date), no login, no pagination.
Dedupe via `jobkey` against `seen_ids`; fetch `/viewjob?jk=` only for NEW jobs.

## Search URL (single source of truth)
https://eg.indeed.com/jobs?q=&l=مصر&radius=100&sort=date&vjk=992741b34fe51afd

- No login needed for page 1; pagination IS login-gated → don't paginate.
- First page holds ~15 jobs (newest first). `sort=date` keeps new jobs at top.

## Gating
- Plain curl with CF cookies → 403 "Security Check" (Cloudflare captcha).
- Must use scrapling stealth session with `solve_cloudflare=True`, persistent
  profile (cf_clearance), CDP pattern like the other boards.

## Data sources in HTML

### 1. Search page — job cards blob
Marker: `window.mosaic.providerData["mosaic-provider-jobcards"] = {...};`
Path: `metaData.mosaicProviderJobCardsModel.results` (array)
Fields per job:
- `jobkey` (Indeed unique id → external_id)
- `displayTitle` / `normTitle` (title)
- `company`
- `formattedLocation` / `jobLocationCity` (location)
- `extractedSalary` → {max, min, type} e.g. {"max":50000,"min":18000,"type":"MONTHLY"}
  - CAVEAT: some jobs have `{"currency":"","salaryTextFormatted":false}` → None
- `jobTypes` (array, e.g. ["Full-time"])
- `pubDate` / `createDate` (unix ms → posted_at)
- `snippet` (short HTML summary)
- `viewJobLink` (relative URL)
- NOTE: this blob only covers the visible page (~15 jobs); requires page 2+ for more

### 2. View job page `/viewjob?jk={JOB_KEY}` — full description
Fetch only for new keys (dedupe first). Two embedded sources:
- `window._initialData = {...};`
  Path (VERIFIED on real viewjob capture, Aug 2026):
  `hostQueryExecutionResult.data.jobData.results[0].job`
  - `description.text` — clean plain-text description (PRIMARY)
  - `description.html` — HTML variant
  - `location.latitude` / `location.longitude` / `city` / `formatted`
  - `url` (canonical), `jobTypes` [{label}], `sourceEmployerName`,
    `datePublished` (unix ms, matches search `createDate`)
  - NOTE: the older `autoOpenTwoPaneViewjobResponse.body.` prefix exists on
    the SEARCH page's two-pane blob; the viewjob page uses the bare
    `hostQueryExecutionResult...` shape. Parser tries both.
- `<script type="application/ld+json">` (Schema.org JobPosting)
  - `title`, `hiringOrganization.name`, `jobLocation`, `description` (HTML),
    `baseSalary.value.{minValue,maxValue,currency}`, `datePosted` (ISO) — good
    fallback for salary/date, stable structure.

## Extraction approach
- Marker regex + brace balancing (like Wuzzuf `_extract_state`) — NOT
  non-greedy `({.*?});` regexes: nested braces break them.
- `re.search(r'window\.mosaic\.providerData\["mosaic-provider-jobcards"\]\s*=\s*', html)`
  then balance braces to the closing `};`.
- Use `page.content()` (stealth isolated contexts kill `evaluate`, learned on Wuzzuf).

## Poll frequency / "never miss" math
- Miss condition: >15 new jobs posted within one poll interval.
- Threshold = 15 jobs/interval = 3,600 jobs/day at 6-min polling. Egypt's real
  rate (~tens-hundreds/day) gives >25x margin → 6-min ofelia cadence is safe.
- Caveats: sponsored jobs occupy top-15 slots (smaller capacity); bursts
  (batch postings) could exceed 15/interval. Mitigation candidates: tighter
  polling (CF burn/rate-limit risk), or accept edge case.
- Health signal: log newest `pubDate` per run; deltas between runs should stay
  small — if not, margin erodes.

## Job dict mapping
source="indeed", external_id=jobkey, title=displayTitle, company=company,
posted_at=createDate (ms→local `YYYY-MM-DD HH:MM`, TZ=Africa/Cairo; NOTE:
`pubDate` is normalized to midnight — always prefer `createDate`),
description=viewjob description.text (fallback ld+json / search snippet),
link=https://eg.indeed.com/viewjob?jk={key} (canonical — drop the token-laden
`viewJobLink` query string), extra={location, salary min/max/type, jobTypes,
snippet, latitude/longitude}, scraped_at.

## To verify (implementation phase)
- Offline fixtures in `markup/indeed/`: search page capture + 1 viewjob capture
- `_extract_jobs(search_html)` → 15 jobs, non-empty fields
- `_extract_description(viewjob_html)` → non-empty text
- Confirm sponsored count in top-15 on real capture.
