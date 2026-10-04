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
  `enrichment` service continuously processes new SQLite queue rows and delivers
  completed jobs. It runs without Chrome.

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
docker compose logs --tail 50 enrichment
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
