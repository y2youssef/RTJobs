# INDEED.md — Indeed board scraping notes

Strategy: poll a single search URL (sorted by date), no pagination.
Login-gated: keep the persistent-profile session alive, re-login by email
code when it expires (see "Login" below). Dedupe via `jobkey` against
`seen_ids`; fetch `/viewjob?jk=` only for NEW jobs.

## Search URL (single source of truth)
https://eg.indeed.com/jobs?q=&l=مصر&radius=100&sort=date&vjk=992741b34fe51afd

- The current anonymous flow is login-gated even on page 1; use the saved session.
  This board still polls only one page.
- First page holds ~15 jobs (newest first). `sort=date` keeps new jobs at top.

## Gating
- Plain curl with CF cookies → 403 "Security Check" (Cloudflare captcha).
- Must use scrapling stealth session with `solve_cloudflare=True`, persistent
  profile (cf_clearance), CDP pattern like the other boards.
- Since ~Oct 2026 Indeed is LOGIN-GATED for anonymous sessions: even after a
  solved turnstile it 403-redirects to
  `/account/login?branding=login-required&from=bot-detection-anonymous`.
  Scraping requires the logged-in profile session (see "Login").

## Login (Telegram-assisted email codes)
- Flow (boards/indeed/login.py, verified live Oct 2026): search page ->
  302 to `secure.indeed.com/auth` -> fill `INDEED_EMAIL` -> Continue ->
  click "Sign in with a code instead" (`a#auth-page-google-otp-fallback`)
  -> type the emailed 6-digit code -> verify via homepage
  (`"isLoggedIn":true` / logout link). One-time per session; cookies persist
  in the `indeed_profile` volume like LinkedIn.
- Code round-trip: the run prompts the failure channel (ForceReply, falls
  back to plain text in channels) and long-polls Bot `getUpdates`
  (`timeout=30`, `allowed_updates=[message, channel_post]`) up to
  `INDEED_CODE_WAIT_SECONDS`. Reply in the channel or DM the bot — a reply
  to the prompt is matched first, any 6-digit message is fallback. Offset is
  persisted (`telegram_update_offset`); the window opens at alert time so
  stale codes are never consumed. A missed code cools down 30 min, then the
  next run starts a fresh episode (fresh code + fresh prompt).
- Gotchas:
  - Navigate with ONE in-browser `page.goto` (page_action), NOT the
    engine's redirect chain: the engine follows the 307/302 to
    `secure.indeed.com` as a separate fetch and the dispatcher 400s on the
    cookieless `;jsessionid` URL. If a landing still carries `;jsessionid`,
    re-request the stripped URL (session cookie now set) — this flips
    400 -> 200 (verified live).
  - After code submit, wait until the URL LEAVES the auth page (OAuth dance
    via postauthfunnel, up to ~90s) before judging — an early check misreads
    a mid-flight redirect as a rejection.
  - `secure.indeed.com/auth` challenge pages are titled "Security Check"
    (not "Just a moment") — the settle helper watches for both.
  - Spider safety net: auth-redirect/block-page mid-scrape sets flags
    (`logged_out`/`blocked`) instead of grinding turnstiles; the board
    alerts once per episode (see `mark_logged_out` / `mark_blocked_page`).

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
Fetch only for new keys (dedupe first). Three supported embedded sources:
- `window._rootProps.preloadedVJData` (verified live October 2026):
  `jobInfoWrapperModel.jobInfoModel.sanitizedJobDescription`, header location,
  `salaryInfoModel`, and `jobKey`. In this layout `_initialData` is JavaScript
  referring to `_rootProps`, not strict JSON. Parse `_rootProps` directly.
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
    fallback for description and a supplement for salary, employment types,
    location/country, remote work and expiry even when another source supplied
    the description. Require `@type=JobPosting` and reject conflicting job keys.

## Extraction approach
- Marker regex + `json.JSONDecoder().raw_decode(html, start)` — NOT
  non-greedy `({.*?});` regexes: nested braces break them.
- `re.search(r'window\.mosaic\.providerData\["mosaic-provider-jobcards"\]\s*=\s*', html)`
  then let the JSON decoder identify the object boundary, including nested braces.
- Use `page.content()` (stealth isolated contexts kill `evaluate`, learned on Wuzzuf).

## Poll frequency / "never miss" math
- Miss condition: >15 new jobs posted within one poll interval.
- Nominal capacity = 15 jobs/interval = 7,200 jobs/day at 3-min polling.
  This assumes every run completes on schedule and all slots are new listings;
  it is not a guarantee of coverage.
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
snippet, latitude/longitude}, scraped_at. `description_truncated` remains true
until a full detail description is parsed; `description_source` is either
`search_snippet` or `detail`. `detail_status` identifies `unavailable` or
`fetch_failed`. Failed detail requests retain the raw search card. Every new
card on the page gets a detail fetch (the former ten-detail cap was removed);
failed details are not retried later because those job keys are saved.

## To verify (implementation phase)
- Offline fixtures in `markup/indeed/`: search page capture + 1 viewjob capture
- `_extract_jobs(search_html)` → 15 jobs, non-empty fields
- `_extract_detail(viewjob_html, expected_key)` → full text + metadata
- Confirm sponsored count in top-15 on real capture.

`scripts/verify_offline.py` covers both the older `newjob_sample.html` and
current `standalone_job_sample.html` fixtures, including salary, employment
types, mismatched job identity and failed-request card retention. Live validation
on 2026-10-03 parsed 15 search cards and three current detail pages per audit;
this sample verifies the parser paths, not site-wide completeness.
