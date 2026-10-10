# Production deployment — 2026-10-04

Classification and job-family delivery were enabled at 16:42 Cairo time after
the user authorized cleanup, push and production activation. Application code
commit: `7c912f0`. Image tag: `rtjobs-scraper:production-family-v2`.

## Active configuration

- `ENRICHMENT_ENABLED=true`
- `CLASSIFIED_DELIVERY_ENABLED=true`
- `CLASSIFIER_MODEL=openai/gpt-6-luna`
- `CLASSIFIER_DAILY_BUDGET_USD=1`
- All 26 unique family destinations configured in private `TELEGRAM_CHANNELS_JSON`.
- API credentials, channel IDs and configurable API base URLs live in `.env`.
- `ofelia` starts the one-shot scraper every six minutes. The separate
  `enrichment` service classifies whole completed scrape cycles in one request.
  `delivery` posts saved results independently; `monitor` checks local pipeline
  health and alerts. All three services run without Chrome.

Public `.env.example` keeps activation disabled and credentials/IDs blank.
Historical jobs are not automatically queued or reclassified.

## Verified rollout

The scheduler was paused between scrape runs. A consistent SQLite backup and
environment backups were saved outside the repository under
`~/.local/state/rtjobs/backups/`. The migration preserved all **38,794** existing
raw jobs, 92,077,418 description characters and 38,794 delivery acknowledgements.
The historical enrichment queue remained empty; SQLite quick_check passed.

The first production run completed all three boards with `ok` status:

| Source | Newly saved jobs | Outcome |
|---|---:|---|
| LinkedIn | 8 | Parsed and saved |
| Wuzzuf | 5 | Parsed and saved |
| Indeed | 0 | Cloudflare challenge solved; 15 cards already seen |

The worker classified and delivered all **13/13** new postings across **11 job
families**. Each SQLite delivery acknowledgement was checked against the expected
family destination. All results were ready, with no model-failure fallbacks.
Provider-reported cost was **$0.00602995** and cached input tokens totalled **81,480**.
These figures describe the first batch, not all subsequent scheduled runs.

The one-shot scraper exited successfully; the worker and scheduler remained
running without restarts. Live permissions checks passed for every channel.
Local Python 3.14 and container Python 3.13 regression checks passed, as did
dependency validation and the repository credential/ID check.

## Operations

```bash
docker compose logs --tail 50 enrichment delivery monitor
docker logs --tail 50 ofelia
```

Run `scripts/check_channels.py` to verify current channel identity/permissions
without sending test messages. Its output includes private IDs: keep exported
reports outside Git. Run `scripts/check_repository.py` before pushing changes.

See [ANALYTICS.md](ANALYTICS.md) for read-only statistics and coverage interpretation.

## Rollback

The previous working image is retained as
`rtjobs-scraper:before-family-production-20261004`.
Pause `ofelia`, let an active scrape finish, and stop the enrichment service.
Set both activation flags false in `.env`, tag the retained image as
`rtjobs-scraper:latest`, and recreate only the scraper with `--no-build --no-deps`.
Then restart `ofelia`.

Keep the current database during a code rollback; the migration is additive.
The backup is available for deliberate database recovery, which must account
for jobs saved since that backup. Do not reset notified flags or remove volumes.


## Whole-cycle pipeline update — 2026-10-04

The batching update sends all uncached jobs from one completed scrape cycle in
one completion request, with no 25-job cap. Provider/budget failures stay pending
and local monitoring alerts independently. See [PIPELINE.md](PIPELINE.md).

Pre-deployment validation passed on Python 3.14 and container Python 3.13. The
offline 37-job test verified a single completion request across multiple boards,
exact/reordered IDs, incomplete-response rejection, shared billing, persistent
retry and simultaneous delivery. All 19 live evaluation fixtures passed together
in one completion request for $0.002573325. All 26 channel identities and bot
posting permissions passed the read-only audit again.

With production writers stopped, a read-only snapshot was saved under the
gitignored `artifacts/whole-batch-20261004/` directory. Migration on a copy
preserved all **39,084 raw jobs and delivery acknowledgements**, including an
exact fingerprint of every original job field and notification flag. All 290
existing enrichments remained ready; no historical rows were queued. SQLite
quick_check passed.

For this update, the previous production image remains
`rtjobs-scraper:production-family-v2`. A rollback to that image requires stopping
`delivery` and `monitor` as well as `enrichment` first: the previous classifier
also sends messages itself. Keep the current SQLite database and delivery flags.


The update went live at **23:06 Cairo**, application commit `771d456`, image
`rtjobs-scraper:production-whole-batch-20261004`. The scheduler resumed normally.
The first cycle saved 7 LinkedIn and 1 Indeed job; Wuzzuf had no new jobs. All
three source runs were `ok`. Exactly **one completion request classified all
8 jobs**, then all 8 were delivered with zero destination mismatches.

Measured timings for that cycle: 93 seconds scraping; 28 seconds before the
classifier started its request (30-second idle polling interval); 26 seconds
classification; mean 3 and maximum 4 seconds from classification to delivery.
Total scrape-start to last acknowledgement was 151 seconds, excluding time
waiting for the six-minute schedule. These are observed timings, not an SLA.
Request cost was $0.00274745, with 6,790 cached input tokens. No pipeline checks
were failing and no queue backlog remained at verification.


## Immediate dispatch update — 2026-10-04

Deployed at **23:21 Cairo**, application commit `d1d8fb2`, image
`rtjobs-scraper:production-immediate-dispatch-20261004`. The previous
`production-whole-batch-20261004` image remains available for rollback.
This update changes no database schema and does not reset saved work.

After SQLite commits a completed cycle, a local notification wakes enrichment;
after validated results commit, another wakes delivery. Startup scans and timeout
scans remain recovery fallbacks, respecting retry deadlines and acknowledgements.

The first live cycle after this update finished at **23:22:16**. The worker
received its notification, selected all **4 new jobs**, and started their single
completion request in that same recorded second. Classification finished at
23:22:30; delivery received its notification then and acknowledged all 4 jobs by
23:22:31. All three source runs were healthy, destinations matched their families,
and local monitoring reported no failing checks. Timestamps have one-second
resolution; timings describe this observed cycle, not a latency guarantee.

The complete offline suite passed on Python 3.14 and container Python 3.13.
Additional real IPC checks verified cross-process and cross-container wakeups,
commit visibility, immediate delivery, missed notifications, restart, backoff,
full/duplicate notifications, shutdown and rollback without a premature wakeup.

## UTC clock, login safety, cycle limits and speed — deployed 2026-10-08

Deployed at **18:33 Cairo** (application `d6fae84`), image
`rtjobs-scraper:production-utc-speed-20261008` (`4d092309d4b6`); the previous
`production-latency-budget-20261008` image remains for rollback, and a
pre-migration SQLite backup is at `/data/backups/rtjobs-pre-utc-20261008-1832.db`
(integrity ok, 42,374 jobs). Verified live: migration to `user_version` 1 (18:30
Cairo rows read 15:30 UTC); ofelia registered `0 */3 * * * *`; the first cycles
took 50s and 37s with every board `ok`; LinkedIn login_check fell from ~8s to
3.5s and Indeed's from 8.2s to 6.1s; both classifier requests succeeded first
time; scraped -> delivered was 25-50s (median before: ~3 min); the monitor
reported 0 of 16 checks failing. `up --build` hit a parallel-build tag race
(four services building one image); only the scraper builds it now.


Changes: stored timestamps move to UTC (`core/clock.py`; only `posted_at`
stays local), LinkedIn rejected-credential lock / late-redirect fix, free
hold-back of cycles that can never fit one classifier request, classifier
`Connection: close` (no stale keep-alive sockets), first search page only on
every board with a 3-minute ofelia schedule (`SCRAPE_INTERVAL_MINUTES`,
no-overlap, immediate catch-up cycle after an overrun), and no idle 5s wait
after the LinkedIn/Indeed login checks. The scraper `command` and ofelia
labels change, so the containers must be recreated (the commands below do).
`jobs.scraped_at` changes shape from `HH:MM` local to `HH:MM:SS` UTC; anything
reading the DB directly should parse it with `datetime.fromisoformat`.
The scraper now starts through `docker/entrypoint.sh` (Xvfb + exec, so
`docker stop` reaches Python), which only exists in a rebuilt image: always
deploy with `--build`. Manual CDP attach is unchanged (chrome://inspect or
`scripts/inspect_chrome.py`); other web origins are now refused.
To change the cadence later, set `SCRAPE_INTERVAL_MINUTES` in `.env` and run
`docker compose up -d` (it recreates both the scraper and the scheduler).

The first process on the new image converts existing local timestamps once
(`PRAGMA user_version` 0 -> 1). An old-image process still running afterwards
would keep writing local times, so restart everything together:

```bash
docker compose --profile enrichment stop          # scraper, workers, monitor
docker compose --profile enrichment up -d --build # all services on the new image
```

Rollback to an older image: stop every service, then convert back before
starting it (see the script's docstring):

```bash
docker compose --profile enrichment stop
docker compose run --rm -e TZ=Africa/Cairo scraper python scripts/revert_utc_timestamps.py --yes
```

## Parallel boards, reposts, LinkedIn cache — deployed 2026-10-09

Deployed in steps on 2026-10-09 (Cairo), each with
`docker compose --profile enrichment up -d --build`; no migration (the
`job_reposts` table and `jobs.reposted_at` column are created on startup).

| Time  | Commit    | Image tag                                   | Change |
|-------|-----------|---------------------------------------------|--------|
| 15:30 | `7c036e9` | `production-parallel-20261009`              | Repost detection, Telegram idle reconnect, parallel boards (CDP 9222/9223/9224), per-board time budget, one alert per failure episode |
| 15:55 | `de3bf20` | `production-watchdog-20261009`              | Parent kills a board past budget + 60s (Playwright swallowed the in-board timeout) |
| 16:00 | `2f5c198` | `production-indeed-back-20261009`           | Indeed: go Back from the `;jsessionid` auth 400 |
| 16:25 | `43af5ba` | `production-linkedin-cache-20261009`        | LinkedIn without request interception (HTTP cache on) |
| 16:34 | `32b2d26` | `production-linkedin-slow-asset-20261009`   | One same-page wait while the app bundle downloads |
| 16:50 | `962dbf0` | `production-linkedin-scroll-20261009`       | Scroll the real list scroller; settle while data loads |

Verified live after 16:50: three consecutive cycles `ok` on all boards
(83s with 10 new LinkedIn jobs, then 25s and 25s), 25 of 25 LinkedIn cards
per page, one classifier request per cycle, every new job delivered, no
failing scrape-health checks. The watchdog stopped stuck Indeed (Cloudflare
Turnstile loop) and LinkedIn (slow detail panels) runs at ~360s.
Rollback: retag `production-card-list-20261008` as `rtjobs-scraper:latest`
and `docker compose --profile enrichment up -d` (no schema change to undo;
the extra table/column are ignored by older code).

## Hotfix: clean Chrome quit — deployed 2026-10-09 22:46

Production code `962dbf0` plus the cookie fix only (tag
`deploy/hotfix-clean-quit-20261009`, commit `81406a4`; the image was built
from `2c6df88`, identical code, only the AGENTS.md wording was corrected
afterwards). Image `rtjobs-scraper:hotfix-clean-quit-20261009`
(`fe666088d997`), deployed with `docker compose --profile enrichment up -d`
(no build) between cycles. First cycle (22:46:24-22:46:48): all three boards
`ok`, and all three profiles' Cookies files were written at the end of a
24s run (before, only runs longer than 30s saved cookies).
Rollback: retag `production-linkedin-scroll-20261009` as latest and
`docker compose --profile enrichment up -d`.

Not deployed yet: main also contains the cycle-latency work (immediate
saves, LinkedIn direct search and job/cards API data, Indeed single load,
Cloudflare fast path) and the NaukriGulf board (`NAUKRIGULF_ENABLED=false`).
Deploy with `--build` (the Dockerfile gains the naukrigulf profile mount
point and compose a `naukrigulf_profile` volume).

## main 726a07e (cycle latency + NaukriGulf) — deployed 2026-10-09 22:54

`docker compose --profile enrichment up -d --build` from the main checkout;
image `rtjobs-scraper:production-main-726a07e-20261009` (`3987ea6ab93c`). The
build took ~40s after the idle check, so the recreate interrupted the 22:54:00
cycle (cycle row `interrupted`; its LinkedIn run row stayed `running`, nothing
lost). Next time: build first, then wait for idle right before `up -d`.

NaukriGulf: the `naukrigulf_profile` volume was seeded from the supervised
staging profile (Akamai cookies + cached app, 23 MB, owner `scraper`), then
`NAUKRIGULF_ENABLED=true` was added to the private `.env` and the services were
recreated (22:56:13). Its very first run failed with `ERR_NETWORK_CHANGED`
during that recreate (one navigation per run, no retry); the next run read
30/30 jobs and all were classified and delivered.

First 2.5 hours (22:56-01:21, `scripts/report_window.py`): 49 cycles, 44 ok and
5 degraded; cycle p50 12s (baseline 31s), p90 127s. Quiet runs p50: LinkedIn 7s
(was 17s), Indeed 5.5s (was 21s), Wuzzuf 5.5s (was 8s), NaukriGulf 5s. LinkedIn
detail p50 1.6s (was 3.1s), scraped->delivered p50 17s (was 30s). 71 new jobs,
71 delivered, no coverage gaps, no saturated first page (except NaukriGulf's
first load). The degraded runs were simultaneous navigation failures across
boards at 23:57, 00:12 and 01:18 (Telegram itself unreachable at 23:57): host
network drops; every board recovered on the next run; one Indeed run was
stopped by the 360s watchdog at 00:12.
Rollback: retag `hotfix-clean-quit-20261009` (or `production-linkedin-scroll-20261009`)
as latest, set `NAUKRIGULF_ENABLED=false`, `docker compose --profile enrichment up -d`.

## 2026-10-10 18:30 Cairo: deploy gate + pinning + alert rules (`cf7db61`)

First deploy through `scripts/deploy.sh` (run by the user). Build + gate in the
candidate: 42 offline checks, scrapling contract ok, Chrome 151.0.7922.108
(18:28:31-18:30:30). No cycle was running and the next tick was 150s away, so
it switched at once (18:30:30); the 2 LinkedIn jobs the 18:30:00 cycle found
were delivered at 18:30:32 while the workers restarted. Watched cycles: 18:30:32
(NaukriGulf `degraded`, `ERR_NETWORK_CHANGED` while the containers were being
recreated, same as at the 22:56 switch on Oct 9; Wuzzuf retried past it) and
18:33:00 (all four boards ok). Verdict 18:33:06. No alert for the NaukriGulf
blip (new confirm-twice rule; `scrape_health_episodes` holds its one failure).
Image `rtjobs-scraper:production-cf7db61-20261010-1828`; rollback target
`rtjobs-scraper:rollback-20261010-1828` (= `production-main-726a07e-20261009`,
safe: scrape_health keeps its 8 columns). Rollback: retag it as latest,
`docker compose --profile enrichment up -d`.
