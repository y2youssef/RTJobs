# EXPAND.md — RTJobs scale-up: classify at scrape, per-dept channels, Gulf

## 0. Locked decisions
- Channels: per country × per department (`EG|Sales & Business Development`
  → chat id). 20 departments × 5 countries (EG/SA/AE/QA/KW) = up to 100
  slots, but ONLY pairs present in the map get traffic; everything else →
  fallback (today's channel). Start: top ~6 depts × 5 ≈ 30 + fallback.
  Channels are hand-created; bot added as admin; IDs pasted into
  `TELEGRAM_CHANNELS_JSON`. Verify with `scripts/ping_channels.py`.
- Countries: EG (live) → SA + AE + QA + KW (full Gulf).
- Unclassifiable → fallback channel. Nothing is ever silently dropped.
- Phase 0 first: keyword-only routing, no API key, no new infra.
- LLM: OpenRouter as Tier-2 fallback on DeepSeek 4.1 (see §8 for model ID
  + cost math). Heavy da_guide extraction stays async, out of the loop.
- LinkedIn: one account PER COUNTRY, parallel per-country services
  (not round-robin). Accounts do NOT exist yet — creation + warm-up is
  step zero. Staggered crons, never simultaneous (single egress IP).
- New boards: NaukriGulf + Bayt, each shipped disabled until its own
  verified live run.

## 1. Target architecture
```
Scrape (board × country service)
  → Tier-1 keyword classify (core/dept_rules.py — free, ~97% hit)
  → Tier-2 OpenRouter/DeepSeek-4.1 on misses only (pennies, cached)
  → persist (extra.department + extra.country)
  → route to country|dept channel (fallback otherwise)
```
Analytics pipeline (extraction → analysis → charts) UNCHANGED.

## 2. Step 0 — accounts & channels (human, before code matters)
1. Create + phone-verify 4 LinkedIn accounts (SA/AE/QA/KW). Light human
   use ~1–2 weeks (new accounts have tight search-view limits).
2. Hand-create channels: `RTJobs EG | Sales & Business Development`, … —
   country code + dept VERBATIM (the router matches on it). Bot → admin.
3. Collect chat IDs, fill `TELEGRAM_CHANNELS_JSON`, run ping script.

## 3. Build order (code)
1. **DB groundwork** — D2 (`idx_jobs_notified_source`, 90-day `seen_ids`
   retention) + D8 (batched writes) + WAL mode. Needed before concurrency.
2. **Classifier** — `core/dept_rules.py` (title→dept, Arabic-aware) +
   `core/classify.py` (rules → OpenRouter → fallback), SQLite cache on
   normalized (title, company), spend cap + per-run cost log. Tests.
3. **Pipeline** — `persist_and_notify` classifies pre-save; `country` attr
   per board; `notify_jobs` routes by `country|dept` with flag prefix
   (🇪🇬🇸🇦🇦🇪🇶🇦🇰🇼); D3 per-channel backlog caps; D5 telegram hardening;
   D4 truncated-description marker (classifier input quality).
4. **LinkedIn per-country services** — `LINKEDIN_ACCOUNTS_JSON`
   (geo → creds/profile/port/cron-offset); compose
   `scraper-linkedin-{eg,sa,ae,qa,kw}` sharing one image; CDP 9222–9226;
   staggered ofelia schedules; per-service healthchecks. Missing creds →
   graceful no-op (exit 0 + warning), never crash-loop.
5. **Other countries** — Indeed `sa/ae/qa/kw` domains (single service,
   internal loop); Wuzzuf EG-only untouched; `external_id`
   country-prefixed to avoid cross-country dedupe collisions.
6. **NaukriGulf + Bayt** — anti-bot recon each → Spider + selectors +
   profile volume + `*_ENABLED=false` until verified live. Gulf region
   tables (Riyadh→Riyadh Province, …) + per-market blocklist review.
7. **Verify** — offline routing/classifier tests, channel ping, one live
   run per new piece. Rollback = empty channel map + toggles off.

## 4. Config reference (new env)
```
OPENROUTER_API_KEY=            # Tier-2 LLM
CLASSIFIER_MODEL=deepseek/deepseek-4.1   # confirm exact ID on OpenRouter; overridable
TELEGRAM_CHANNELS_JSON={"EG|Sales & Business Development":"-100xxx", ...}
LINKEDIN_ACCOUNTS_JSON={"EG":{"email":..,"password":..,"geoId":"106155005","profile":"chromeprofile","port":9222}, ...}
NAUKRIGULF_ENABLED=false
BAYT_ENABLED=false
```

## 5. Cost model — DeepSeek 4.1 classification (Sep-2026 pricing)
Classify-only prompt ≈ 300 tokens in / ~30 out. DeepSeek V4-Flash-class
≈ $0.14/M input + $0.28/M output (OpenRouter similar; confirm exact 4.1
rate on the model page — Nano/Flash-class ranges $0.02–0.14/M in).

| Daily volume | All-LLM (no rules) | Rules-first (~5% hit API) |
|---|---|---|
| 770 jobs (today) | ~$0.04/day ≈ **$1.20/mo** | **≈ $0.06/mo** |
| 5,000 jobs/day | ~$0.25/day ≈ **$7.50/mo** | **≈ $0.40/mo** |
| 20,000 jobs/day | ~$1.00/day ≈ **$30/mo** | **≈ $1.50/mo** |

Rules-first is the whole game: the keyword tier resolves ~95%+ for free,
the API only sees genuine ambiguities (each cached, never re-paid).
OpenRouter spend limit = hard ceiling regardless. Extraction/analytics
costs unchanged (async, already sunk). Telegram: free within rate limits;
0.3s/msg pacing × channels is the throttle, not money.

## 6. Risks
- **Single egress IP × 5 accounts** is the top risk (weeks 1–2 especially).
  Staggering mitigates; residential proxies are the escape hatch.
- Checkpoint/2FA babysitting ×5 accounts — the real ongoing human cost.
- RAM: 5 headful Chromes ≈ 1.5–2.5 GB + other boards; confirm headroom.
- New-account search limits: new geos start slow, ramp after warm-up.
- Blocklist is substring-based: review per market (`=` exact-match exists).
- Location tables are EG-centric: Gulf regions needed before Gulf
  analytics make sense (§6 does this).
- NaukriGulf/Bayt anti-bot unknown until recon — could need the full
  stealth playbook (AGENTS.md #1–2) or manual profile seeding.
