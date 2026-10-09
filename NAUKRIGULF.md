# NaukriGulf board

Public job board (no login) for the Gulf region; we scrape the Egypt search:
`https://www.naukrigulf.com/jobs-in-egypt?freshness=1&xz=1_3_5`
(`NAUKRIGULF_SEARCH_URL`; `freshness=1` = posted in the last day). Disabled by
default (`NAUKRIGULF_ENABLED=false`) until a supervised live run passes.
CDP port `CHROME_DEBUG_PORT + 3` (9225); profile volume `naukrigulf_profile`.

## Protection: Akamai Bot Manager

- The edge is Akamai; every page loads its sensor script (`/akam/13/...`) and
  sets `ak_bmsc` / `bm_sv` / `bm_mi` cookies. A trusted client also gets `_abck`.
- Plain HTTP clients (curl, any non-browser) get HTTP/2 stream resets
  (`INTERNAL_ERROR`) or a held-open connection. A fresh automation-driven
  browser (`navigator.webdriver=true`, no profile history) got the shell, then
  `ERR_HTTP2_PROTOCOL_ERROR` on the API calls, then on the document itself:
  the verdict is cached per client and escalates (recon, Oct 2026).
- The server renders `var puppeteer = false;` into the page; when it decides a
  visitor is automation the app never bootstraps (no API call at all).
- Consequences: real Chrome over CDP on a persistent profile (trust history
  accumulates), no resource blocking (Gotchas #11 + a page that never loads
  images/styles is another signal), ONE navigation per run (`retries=1`), and
  never call the API ourselves. Repeated failing visits can also spill onto
  the egress IP and break the operator's own browser on this site.

## Data: the search API response the page receives

The HTML is a splash screen; the app's JS calls
`GET /spapi/jobapi/search?...&Limit=30&Location=Egypt&Offset=0&pageNo=1...`
with its own headers (`appid: 205`, `systemid: 2323`, `client-type: desktop`,
`puppeteer: false`, ...). The spider only listens for that response
(`search.api_path` in `markup/naukrigulf/selectors.json`).

Response (fixture: `markup/naukrigulf/search_sample.json`, 5 trimmed jobs):
- `totalJobsCount` (47 for one day of Egypt jobs in Oct 2026), `jobs[]` (30
  per page), `other.searchCriteria.sortPreference = "date"`, `timestamp`.
- Per job: `jobId` (external ID, e.g. `091026501533`), `designation` (title),
  `company.{name,id}`, `location` ("Alexandria - Egypt"), `description`
  (FULL HTML — no detail page needed), `jobInfo` (truncated summary),
  `experience.{min,max}`, `latestPostedDate` (epoch seconds; "latest" = it
  moves on a refresh, so it feeds repost detection like Wuzzuf's postedAt),
  `jdURL` (relative slug → `https://www.naukrigulf.com/<jdURL>`), `vacancies`,
  `isSponsoredJob`, `isEasyApply`, `isConsultant`, `isConfidentialCompany`,
  `jobSource`.
- Newest first; sponsored jobs can appear out of order, so every job on the
  page is checked (Gotchas #4). At ~50 jobs/day the 30-job first page is far
  more than one 3-minute interval produces.

## Health checks (`ScrapeHealth("naukrigulf")`)

`search_api` (the response arrived; the alert says why not: Akamai stream
reset, HTTP status, `puppeteer=true`, the app's "Oops! Something went wrong"
screen, or timeout), `search_structure`, `card_identity` (every job has a
jobId), `posted_date` (latestPostedDate readable), plus the shared job fields.

## Not yet verified live

- First run of the production stack on a fresh profile (does Akamai trust it?).
- Behavior at the 3-minute cadence (rate limiting, verdict escalation).
- Geo parameters derive from the egress IP; `Location=Egypt` should dominate
  from another region, unverified.
