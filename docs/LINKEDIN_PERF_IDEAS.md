# LinkedIn: further speed ideas (branch `perf/linkedin-ideas`)

Constraint (user, Oct 2026): reliability first (never miss a job), latency
second, and no compromise: nothing that makes the account look more automated
or loses data. Measure each idea against main with
`scripts/report_window.py` and staging runs (copy of the profile + DB,
Telegram on a dead port, logins disabled; `FORGET=N` re-scrapes N real jobs).

## Where a LinkedIn run spends its time (main, Oct 9)

- Process + Chrome start: ~1.3s.
- Search page document + app boot (bundle cached): ~2s.
- The one job-cards API response (`voyagerJobsDashJobCards`, all 25 IDs and
  listing times): 3-12s after the first scroll, network-bound. Cards render
  0.1-0.2s after it lands; reading the API instead of the DOM saves ~nothing.
- Per new job: 2-4s human pause + ~2s detail (API record path; 3.1s on the
  old panel path).

## Ideas, in the recommended order

1. **Open new jobs while the rest of the list loads.** The first 7 cards are
   server-rendered at once, and new jobs are almost always at the top (sorted
   by date). Click those while cards 8-25 arrive, then handle new ones lower
   down. Hides most detail time inside the 3-12s list wait (~5-10s off a run
   with 1-2 new jobs). Risk: the list re-renders when the other 18 cards land;
   the retry in `_open_card` covers a stale handle and job data is keyed by ID.
2. **Pause after the click, not before** (`human_delay` overlaps the detail
   load). ~1-2s per new job. Clicks come every ~3-5s instead of ~4.5-7s, still
   human-paced; user decision.
3. **Turn off Chrome's own background traffic** (component updates, Safe
   Browsing list downloads, optimization-guide models, metrics) with launch
   flags. Not visible to sites; on a 60-190 KB/s link it competes with the
   cards API. Measure container traffic with and without first.
4. **Give LinkedIn the link first.** All boards share one connection and the
   cycle ends with LinkedIn (the slowest board). Starting the other boards a
   few seconds later could shorten the cycle on slow links. Measure LinkedIn
   alone vs in parallel first.
5. **Fork board children** instead of spawning a new interpreter (skips ~1s
   of imports per child). Small, low risk.
6. **End quiet runs when the cards API shows nothing new**, without waiting for
   the DOM to finish rendering. ~0.1-0.5s.

## Considered and not recommended

- **Persistent warm tab with in-app refresh**: biggest gain on quiet runs
  (~3-5s: no page boot, warm TLS/DNS), but it loses the clean slate every run
  that recovered the Oct 9 failures (Turnstile loops, stuck detail panels,
  crashed tabs) and needs its own health checks and restarts.
- **Narrower search window (`f_TPR`, e.g. last hour)**: smaller pages, but a
  job LinkedIn indexes late could fall outside the window and be missed.
- **Calling LinkedIn's API directly for descriptions (no clicks)**: fastest,
  but API calls without UI events are the classic scraper signature; an
  account restriction would take LinkedIn down entirely.
