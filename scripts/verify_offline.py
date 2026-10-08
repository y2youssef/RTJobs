"""Offline regression checks using repository fixtures and a temporary SQLite DB.

Run with .venv/bin/python scripts/verify_offline.py. No browser or network.
"""

import asyncio
import copy
import json
import logging
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack, nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def isolated_markup(directory) -> str:
    """Copy markup/ (minus runtime snapshots) into the test directory.

    The real markup/ is bind-mounted into production as its snapshot store;
    tests that save evidence there would mix fake snapshots with real ones and
    let the 20-per-kind pruning evict genuine production evidence.
    """
    import shutil
    target = Path(directory) / "markup"
    shutil.copytree(ROOT / "markup", target, ignore=shutil.ignore_patterns("snapshots"), dirs_exist_ok=True)
    return str(target)


def main():
    # Broken fixtures deliberately emit errors; show assertions/PASS lines,
    # not simulated production warnings that could be mistaken for a live outage.
    logging.disable(logging.CRITICAL)
    # Production TZ (compose pins it); the UTC migration checks rely on its DST rules.
    os.environ["TZ"] = "Africa/Cairo"
    time.tzset()
    with tempfile.TemporaryDirectory(prefix="rtjobs-tests-") as directory:
        os.environ.update(PYTHON_DOTENV_DISABLED="1", TELEGRAM_TOKEN="offline",
            TELEGRAM_CHAT_ID="-1001", TELEGRAM_FAILURE_CHAT_ID="-1002",
            OPENROUTER_API_KEY="offline", DATA_DIR=directory, MARKUP_DIR=isolated_markup(directory),
            ENRICHMENT_ENABLED="true", CLASSIFIED_DELIVERY_ENABLED="true", TELEGRAM_CHANNELS_JSON="", LOG_FILE="")
        with patch("requests.sessions.Session.request", side_effect=AssertionError("Network forbidden")):
            verify(directory)
    subprocess.run([sys.executable, str(ROOT / "scripts/verify_pipeline.py")], check=True)
    subprocess.run([sys.executable, str(ROOT / "scripts/verify_wakeup.py")], check=True)
    print("All offline checks passed.")


def verify(directory):
    from core import db, telegram
    from core.browser import patch_no_load_wait
    from core.classify import Enricher, load_channels
    from core.enrichment_worker import process_job
    from core.scrape_health import ScrapeHealth
    from boards.base import load_board_selectors
    from boards.wuzzuf import scraper as wu
    from boards.indeed import scraper as indeed
    from boards.linkedin.scraper import _company_metadata
    from scrapling import Selector

    # Simulate the deployed pre-enrichment schema and ensure repeat migrations
    # preserve historical jobs and delivery flags without queuing a backfill.
    conn = sqlite3.connect(Path(directory) / "rtjobs.db")
    conn.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL,"
                 " external_id TEXT NOT NULL, title TEXT, company TEXT, posted_at TEXT, description TEXT,"
                 " link TEXT, extra TEXT, scraped_at TEXT, notified INTEGER DEFAULT 0, UNIQUE(source,external_id))")
    conn.execute("INSERT INTO jobs(source,external_id,description,notified) VALUES ('legacy','old','keep me',1)")
    # Pre-UTC rows hold Cairo wall time: summer (+3) and winter (+2) values.
    conn.execute("INSERT INTO jobs(source,external_id,posted_at,scraped_at) VALUES"
                 " ('legacy','summer','2026-08-09 16:48','2026-08-09 16:48'),"
                 " ('legacy','winter','2026-12-01 12:00','2026-12-01 12:00:00')")
    conn.execute("CREATE TABLE pipeline_state (name TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)")
    conn.execute("INSERT INTO pipeline_state VALUES ('classifier',"
                 " '{\"reason\":\"daily_budget_exhausted\",\"pause_until\":\"2026-08-10 00:00:00\"}', '2026-08-09 17:00:00')")
    conn.commit()
    conn.close()
    db.init_db()
    db.init_db()
    with db.get_db() as conn:
        assert conn.execute("SELECT description,notified FROM jobs WHERE external_id='old'").fetchone()[:] == ("keep me", 1)
        assert conn.execute("SELECT count(*) FROM job_enrichments").fetchone()[0] == 0
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        # Converted exactly once (two init_db calls); posted_at stays local.
        rows = {r[0]: r[1:] for r in conn.execute("SELECT external_id,posted_at,scraped_at,next_notify_at FROM jobs")}
        assert rows["summer"] == ("2026-08-09 16:48", "2026-08-09 13:48:00", "")
        assert rows["winter"] == ("2026-12-01 12:00", "2026-12-01 10:00:00", "")
        state = conn.execute("SELECT value,updated_at FROM pipeline_state WHERE name='classifier'").fetchone()
        assert json.loads(state[0])["pause_until"] == "2026-08-09 21:00:00" and state[1] == "2026-08-09 14:00:00"
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        conn.execute("DELETE FROM jobs WHERE external_id IN ('summer','winter')")
        conn.execute("DELETE FROM pipeline_state")
    # The October fallback repeats local 23:00-24:00; stored UTC stays ordered.
    from core import clock
    from datetime import date
    assert clock.to_local("2026-10-29 20:30:00") == clock.to_local("2026-10-29 21:30:00") == "2026-10-29 23:30:00"
    assert clock.next_local_midnight(date(2026, 10, 28)) == "2026-10-28 21:00:00"  # +3 night
    assert clock.next_local_midnight(date(2026, 10, 29)) == "2026-10-29 22:00:00"  # fallback night, +2
    from datetime import datetime
    assert clock.age_seconds("2026-10-29 20:30:00", datetime.fromisoformat("2026-10-29 21:30:00")) == 3600
    print("PASS UTC storage: one-time local->UTC migration, posted_at kept local, DST-safe deadlines")

    raw = (ROOT / "markup/wuzzuf/wazzuf_guide.txt").read_text()
    html = raw[raw.find("<!DOCTYPE"):]
    entities = wu._extract_state(html)
    jobs, dup = wu._extract_jobs(html, load_board_selectors("wuzzuf"), set(), entities)
    assert len(entities) == len(jobs) == 15 and not dup
    by_id = {wu._job_id_from_slug(e["attributes"]["slug"]): e["attributes"] for e in entities.values()}
    for job in jobs:
        attrs = by_id[job["external_id"]]
        assert wu._strip_html(attrs["description"]) in job["description"]
        assert wu._strip_html(attrs["requirements"]) in job["description"]
    assert wu._extract_state('<script>"job":{"collection": invalid}</script>') == {}
    assert db.save_jobs(jobs, [("wuzzuf", "blocked-id")], enqueue=False) == 15
    assert db.save_jobs(jobs, enqueue=True) == 0
    assert db.seen_ids_for("wuzzuf", [jobs[0]["external_id"], "blocked-id", "new-id"]) == {jobs[0]["external_id"], "blocked-id"}
    remaining, dup = wu._extract_jobs(html, load_board_selectors("wuzzuf"), set(), entities, lookup_seen=True)
    assert not remaining and dup
    print("PASS Wuzzuf full requirements, indexed dedupe, atomic inserts, migrations")

    search = (ROOT / "markup/indeed/first_page.html").read_text()
    cards, seen, missing = indeed._extract_jobs(search, set())
    assert len(cards) == 15 and not seen and not missing
    assert all(j["extra"]["description_truncated"] for j in cards)
    data = indeed._extract_balanced_json(search, indeed._CARDS_MARKER)
    source_cards = data["metaData"]["mosaicProviderJobCardsModel"]["results"]
    for job, source in zip(cards, source_cards):
        assert job["posted_at"] == indeed._parse_pubdate(source.get("createDate") or source.get("pubDate"))
    desc, extra = indeed._extract_detail((ROOT / "markup/indeed/newjob_sample.html").read_text())
    assert desc and extra["latitude"] and extra["longitude"]
    assert extra["salary"]["currency"] == "EGP" and extra["salary"]["min"] == 20134.4
    assert extra["job_types"] == ["Contract"]
    live = (ROOT / "markup/indeed/standalone_job_sample.html").read_text()
    desc, extra = indeed._extract_detail(live, "52628dfe51b7fda6")
    assert len(desc) > 1000 and extra["salary"]["max"] == 40000 and extra["country"] == "EG"
    assert extra["job_types"] == ["FULL_TIME", "CONTRACTOR"]
    assert indeed._extract_detail(live, "wrong-job") == ("", {})
    assert indeed._extract_detail('<script type="application/ld+json">{"@type":"Organization","description":"not a job"}</script>') == ("", {})
    _, fixed_pay = indeed._extract_detail('<script type="application/ld+json">'
        '{"@type":"JobPosting","baseSalary":{"currency":"EGP","value":{"value":9000,"unitText":"MONTH"}},'
        '"validThrough":"2026-10-15T12:30:00+03:00"}</script>')
    assert fixed_pay["salary"]["min"] == fixed_pay["salary"]["max"] == 9000
    assert len(fixed_pay["expire_at"]) == 19 and "T" not in fixed_pay["expire_at"]
    # A failed request with no detail callback must retain the search card.
    pending = copy.deepcopy(cards[0])
    fake_spider = SimpleNamespace(_pending={pending["external_id"]: pending}, _logged_out=False,
        _blocked=False, _queued={pending["external_id"]}, health=ScrapeHealth("indeed"),
        start=lambda: SimpleNamespace(items=[], stats=SimpleNamespace(elapsed_seconds=0)))
    fake_spider.health.check("search_fetch", True)
    with patch.object(indeed, "IndeedJobSpider", return_value=fake_spider), patch.object(telegram, "notify_failure", return_value=True) as alert:
        recovered = indeed.scrape({}, "unused")["items"]
        assert alert.call_count == 1 and "detail_fetch" in alert.call_args.args[1]
    assert len(recovered) == 1 and recovered[0]["extra"]["detail_status"] == "fetch_failed"
    print("PASS Indeed old/current layouts, salary/types, snippet flags, failed-detail recovery")

    extra = _company_metadata(Selector((ROOT / "markup/linkedin/company_context_sample.html").read_text()),
                              load_board_selectors("linkedin")["job_detail"])
    assert extra["company_industry"] == "Medical Practices" and len(extra["company_description"]) > 200
    assert not extra["company_description"].endswith("show more")
    print("PASS LinkedIn explicit employer context")

    client = Enricher()
    channels = load_channels()
    assert set(channels) <= set(client.taxonomy["job_families"])
    # Use synthetic complete destinations for mocked delivery checks; the live
    # checker independently reports missing IDs instead of inventing channels.
    channels = {family: "-100" + str(9000000 + i) for i, family in enumerate(client.taxonomy["job_families"])}
    os.environ["TELEGRAM_CHANNELS_JSON"] = json.dumps(channels)
    import core.classify as classify_module
    classify_module.TELEGRAM_CHANNELS_JSON = json.dumps(channels)
    for family in client.taxonomy["job_families"]:
        assert telegram.destination_for({"job_family": family, "employer_sector": "technology_telecom"}, channels) == channels[family]
    base = {"source": "test", "external_id": "one", "title": "Engineer", "company": "Software Ltd",
            "description": "Develop backend software.", "extra": {}, "link": "https://example.com/a(b)", "posted_at": "2026-10-03 12:00"}
    assert db.save_jobs([base], enqueue=True) == 1
    row = dict(db.pending_enrichments(1)[0])
    assert all(r["id"] != row["id"] for r in db.get_unnotified())
    def fake_classify(batch):
        data = batch[0]
        result = client.fallback(data["id"])
        result["classification"].update(job_family="software_engineering", specialization="backend", routing_confidence="High", needs_review=False)
        return [result], {"total_tokens": 100, "cost": 0.001}
    with patch.object(client, "request_bound", return_value=0.01), patch.object(client, "classify_batch", side_effect=fake_classify) as call:
        first = process_job(client, row)
        assert first["state"] == "ready" and not first["cached"]
        assert call.call_count == 1
    base["external_id"] = "two"
    db.save_jobs([base], enqueue=True)
    second_row = dict(db.pending_enrichments(1)[0])
    # A fresh client simulates a worker restart; cache must survive it.
    fresh = Enricher()
    second = process_job(fresh, second_row)
    assert second["cached"] and second["result"]["job_id"] == second_row["id"]
    altered = dict(second_row, description="Different salary and responsibilities")
    assert client.prepare(altered)[1] != client.prepare(second_row)[1]
    invalid = copy.deepcopy(first["result"])
    invalid["classification"]["specialization"] = "pharmacy"
    try:
        client.validate(invalid, row["id"])
    except ValueError:
        pass
    else:
        raise AssertionError("Cross-family specialization accepted")
    assert not db.reserve_enrichment_spend("1900-01-01", 0.02, 0.01)
    assert db.reserve_enrichment_spend("1900-01-01", 0.009, 0.01)
    assert not db.reserve_enrichment_spend("1900-01-01", 0.002, 0.01)
    # Operational errors remain pending even after repeated failures.
    base.update(external_id="three", description="Unique retry job")
    db.save_jobs([base], enqueue=True)
    retry_row = dict(db.pending_enrichments(1)[0])
    with patch.object(client, "request_bound", side_effect=ValueError("provider unavailable")):
        assert process_job(client, retry_row)["state"] == "pending"
        retry_row["attempts"] = 2
        db.set_pipeline_state("classifier", {})
        assert process_job(client, retry_row)["state"] == "pending"
    print("PASS all 26 job-family routes, queue restart/cache, schema validation, budgets, persistent retries")

    payloads = []
    responses = iter([SimpleNamespace(ok=False, status_code=400, text="parse error"), SimpleNamespace(ok=True)])
    def post(_url, **kwargs):
        payloads.append(kwargs["json"])
        return next(responses)
    with patch.object(telegram._SESSION, "post", side_effect=post):
        assert telegram._send("-1001", "bad markdown")
    assert payloads[0]["parse_mode"] == "MarkdownV2" and "parse_mode" not in payloads[1]
    # The plain retry is readable text, not leftover MarkdownV2 escapes.
    payloads.clear()
    responses = iter([SimpleNamespace(ok=False, status_code=400, text="parse error"), SimpleNamespace(ok=True)])
    with patch.object(telegram._SESSION, "post", side_effect=post):
        assert telegram._send("-1001", "*A\\.B* \\(x\\)", plain="A.B (x)")
    assert payloads[1]["text"] == "A.B (x)"
    assert telegram._unescape_md("Senior \\(C\\+\\+\\) Dev\\.") == "Senior (C++) Dev."
    # A huge exception text still fits Telegram's 4096-character limit.
    sent = []
    with patch.object(telegram, "TELEGRAM_FAILURE_CHAT_ID", "-1002"), \
            patch.object(telegram, "_send", side_effect=lambda chat, text, plain=None: sent.append((text, plain)) or True):
        telegram.notify_failure("Board crashed", "Call log:\n" + "x.(y)" * 5000, "snap/x.html", "hint")
    # Telegram counts the limit after entity parsing (escapes removed).
    assert len(telegram._unescape_md(sent[0][0])) <= 4096 and len(sent[0][1]) <= 4096
    assert sent[0][1].startswith("🚨 Board crashed") and sent[0][1].endswith("🔧 hint")
    delivered = [r for r in db.get_unnotified(limit=100) if r["id"] == row["id"]]
    with patch.object(telegram, "_send", return_value=True) as send, patch.object(telegram.time, "sleep"):
        assert telegram.notify_jobs(delivered) == 1
        assert send.call_args.args[0] == channels["software_engineering"]
        assert "a%28b%29" in send.call_args.args[1]
    with db.get_db() as conn:
        assert conn.execute("SELECT notified FROM jobs WHERE id=?", (row["id"],)).fetchone()[0] == 1
        assert conn.execute("SELECT description FROM jobs WHERE id=?", (row["id"],)).fetchone()[0] == row["description"]
    # A rate-limit wait is not a failed attempt (no backoff, no retries alert).
    db.defer_notification(second_row["id"], minimum_delay=30, count_attempt=False)
    with db.get_db() as conn:
        assert conn.execute("SELECT notify_attempts FROM jobs WHERE id=?", (second_row["id"],)).fetchone()[0] == 0
    db.defer_notification(second_row["id"], minimum_delay=300)
    with db.get_db() as conn:
        assert conn.execute("SELECT notify_attempts FROM jobs WHERE id=?", (second_row["id"],)).fetchone()[0] == 1
    assert all(r["id"] != second_row["id"] for r in db.get_unnotified(limit=100))
    print("PASS delivery acknowledgement, safe links, plaintext retry and persistent backoff")

    class SyncPage:
        def goto(self, url, **kw): return kw
        def wait_for_load_state(self, state=None, **kw): return state
    sync = SyncPage()
    assert patch_no_load_wait(sync) is None
    wrapper = sync.goto
    assert patch_no_load_wait(sync) is None and sync.goto is wrapper
    assert sync.goto("x")["wait_until"] == "domcontentloaded"
    class AsyncPage:
        async def goto(self, url, **kw): return kw
        async def wait_for_load_state(self, state=None, **kw): return state
    async def check():
        page = AsyncPage()
        await patch_no_load_wait(page)
        wrapper = page.goto
        await patch_no_load_wait(page)
        assert page.goto is wrapper
        assert (await page.goto("x"))["wait_until"] == "domcontentloaded"
    asyncio.run(check())
    from verify_classifier import verify_classifier
    verify_classifier(client, channels)
    from verify_analytics import verify_analytics
    verify_analytics(client)
    client.close()
    fresh.close()
    print("PASS sync/async browser patch contract and repeated setup")

    # Parser alerts survive a process restart. Successful checks reset only
    # the checks actually observed; an all-seen page is not a detail recovery.
    from core import markup
    with patch.object(telegram, "notify_failure", return_value=True) as alert, \
            patch.object(markup, "save_snapshot", return_value="test/snapshot.html"):
        def observation(good):
            health = ScrapeHealth("health-test")
            health.check("search_fetch", True)
            health.check("structure", good, "SSR job data missing", "<main>jobs</main>")
            return health
        broken = observation(False)
        broken.check("structure", True)  # a later good page cannot erase a failure
        broken.report()
        assert alert.call_count == 1 and alert.call_args.args[2] == "test/snapshot.html"
        observation(False).report()
        assert alert.call_count == 1
        empty = ScrapeHealth("health-test")
        empty.check("search_fetch", True)
        empty.job_fields([])
        empty.report()
        observation(False).report()
        assert alert.call_count == 1
        observation(True).report()
        observation(False).report()
        assert alert.call_count == 2
        observation(True).report()
        alert.return_value = False
        observation(False).report()
        alert.return_value = True
        observation(False).report()
        assert alert.call_count == 4  # failed delivery was retried
        no_callback = ScrapeHealth("no-callback")
        no_callback.report()
        assert "search_fetch" in alert.call_args.args[1]
    print("PASS parser-health detection, persistent dedupe, recovery and failed-alert retry")

    # Exercise actual browser callbacks against changed markup, not just the
    # alert helper. No browser is launched and no files/messages are produced.
    class FixturePage:
        url = "https://eg.indeed.com/jobs"
        def __init__(self, html): self.html = html
        async def wait_for_selector(self, *args, **kwargs): pass
        async def wait_for_timeout(self, *args): pass
        async def title(self): return "Jobs"
        async def content(self): return self.html
    async def changed_markup():
        raw = (ROOT / "markup/wuzzuf/wazzuf_guide.txt").read_text()
        raw = raw[raw.find("<!DOCTYPE"):]
        w_spider = wu.WuzzufJobSpider(load_board_selectors("wuzzuf"), "http://127.0.0.1:1")
        damaged = raw.replace('"job"', '"renamedJob"')
        assert damaged != raw
        await w_spider.scan_page(FixturePage(damaged))
        assert not w_spider.health.checks["ssr_collection"]["good"]
        w_spider.health.report()
        i_spider = indeed.IndeedJobSpider({}, "http://127.0.0.1:1")
        await i_spider.scan_search_page(FixturePage(search.replace('"jobkey"', '"changedJobKey"')))
        assert not i_spider.health.checks["card_identity"]["good"]
        i_spider.health.report()
        # A valid, all-seen Indeed page produces no new jobs and no alarm.
        good = indeed.IndeedJobSpider({}, "http://127.0.0.1:1")
        with patch.object(db, "seen_ids_for", return_value={j["external_id"] for j in cards}):
            await good.scan_search_page(FixturePage(search))
        assert not good._page_jobs and all(c["good"] for c in good.health.checks.values())
        # "Access Denied" inside job text is not a block page while job cards exist.
        snippet = indeed.IndeedJobSpider({}, "http://127.0.0.1:1")
        with patch.object(db, "seen_ids_for", return_value=set()):
            await snippet.scan_search_page(FixturePage(search + "<p>Access Denied handling for IAM roles</p>"))
        assert not snippet._blocked and snippet._page_jobs
        blocked_page = indeed.IndeedJobSpider({}, "http://127.0.0.1:1")
        await blocked_page.scan_search_page(FixturePage("<html><body>Access Denied</body></html>"))
        assert blocked_page._blocked
        from boards.linkedin.scraper import LinkedInJobSpider
        class MissingCardsPage(FixturePage):
            async def wait_for_selector(self, *args, **kwargs): raise TimeoutError("changed selector")
        linked = LinkedInJobSpider(load_board_selectors("linkedin"), "http://127.0.0.1:1")
        await linked.deep_scan_page(MissingCardsPage("<main>new layout</main>"))
        assert not linked.health.checks["search_structure"]["good"]
        linked.health.report()
        stalled = LinkedInJobSpider(load_board_selectors("linkedin"), "http://127.0.0.1:1")
        await stalled.deep_scan_page(MissingCardsPage((ROOT / "markup/linkedin/loading_shell_sample.html").read_text()))
        assert "startup/loading screen" in stalled.health.error
        assert "waiting for job cards" in stalled.health.error
        assert stalled.health.status == "degraded"
    with patch.object(telegram, "notify_failure", return_value=True) as alert, \
            patch.object(markup, "save_snapshot", return_value="test/changed.html"):
        asyncio.run(changed_markup())
        assert alert.call_count == 3
    print("PASS changed LinkedIn/Wuzzuf/Indeed markup triggers error-channel alerts; all-seen is normal")

    # Startup failures must close the run record even before a session exists.
    import boards.linkedin as li_board
    import boards.wuzzuf as wu_board
    import boards.indeed as in_board
    with ExitStack() as stack:
        stack.enter_context(patch.object(li_board.login_state, "is_blocked", return_value=False))
        stack.enter_context(patch.object(li_board.login_state, "should_wipe_profile", return_value=False))
        stack.enter_context(patch.object(li_board.login, "kill_zombie_chrome"))
        for module, cls in ((li_board, li_board.LinkedInBoard), (wu_board, wu_board.WuzzufBoard), (in_board, in_board.IndeedBoard)):
            with patch.object(module, "chrome_session", side_effect=RuntimeError("startup failed")):
                try:
                    cls().run()
                except RuntimeError:
                    pass
                else:
                    raise AssertionError("Chrome launch failure swallowed")
            with db.get_db() as conn:
                row = conn.execute("SELECT status,finished_at FROM runs WHERE source=? ORDER BY id DESC LIMIT 1", (cls.name,)).fetchone()
                assert row[0] == "error" and row[1]
    print("PASS Chrome startup failure audit for all boards")

    # A completed browser/spider with failed data checks must record degraded,
    # while retaining valid partial results. Healthy empty/new runs remain ok.
    class LoggedInSession:
        def __init__(self, **kwargs): self.action = kwargs["page_action"]
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def fetch(self, *args, **kwargs): self.action(object())
    with ExitStack() as stack:
        stack.enter_context(patch.object(li_board.login_state, "is_blocked", return_value=False))
        stack.enter_context(patch.object(li_board.login_state, "should_wipe_profile", return_value=False))
        stack.enter_context(patch.object(li_board.login, "kill_zombie_chrome"))
        for module, cls in ((li_board, li_board.LinkedInBoard), (wu_board, wu_board.WuzzufBoard), (in_board, in_board.IndeedBoard)):
            for healthy in (False, True):
                def fake_scrape(selectors, cdp_url, health):
                    health.check("search_fetch", True)
                    health.check("search_structure", healthy, "Simulated incomplete page")
                    items = [dict(base, source=cls.name, external_id="status-test-" + str(healthy))]
                    if cls.name == "wuzzuf":
                        return items
                    return {"items": items, "login_redirect": False, "logged_out": False, "blocked": False}
                with ExitStack() as case:
                    case.enter_context(patch.object(module, "chrome_session", side_effect=lambda *a, **kw: nullcontext("unused")))
                    case.enter_context(patch.object(module.scraper, "scrape", side_effect=fake_scrape))
                    if cls.name != "wuzzuf":
                        case.enter_context(patch.object(module, "StealthySession", LoggedInSession))
                        case.enter_context(patch.object(module.login, "ensure_logged_in", return_value=True))
                    assert cls().run() == 1
                with db.get_db() as conn:
                    row = conn.execute("SELECT status,jobs_found,error FROM runs WHERE source=? ORDER BY id DESC LIMIT 1", (cls.name,)).fetchone()
                    assert row[0] == ("ok" if healthy else "degraded") and row[1] == 1
                    assert bool(row[2]) == (not healthy)
    print("PASS loading-screen diagnosis and degraded run status with partial data preserved")
    verify_browser_recovery()
    verify_linkedin_login()
    verify_first_page_and_schedule()
    verify_browser_launch()


def verify_browser_launch():
    """CDP origin allow-list, sandbox switch, busy port, group stop, crash stderr."""
    import signal as signals
    import socket
    import tempfile as tmp
    from core import browser

    args = browser.chrome_args("chrome", "/profile", 9222, no_sandbox=False)
    origins = next(a for a in args if a.startswith("--remote-allow-origins="))
    assert "*" not in origins and "devtools://devtools" in origins and "http://localhost:9222" in origins
    assert "--no-sandbox" not in args and "--no-sandbox" in browser.chrome_args("chrome", "/p", 9222, no_sandbox=True)

    # A port someone else already serves is refused before anything launches.
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0)); busy.listen()
        with patch.object(browser.subprocess, "Popen", side_effect=AssertionError("must not launch")):
            try:
                browser.launch_cdp_chrome(tmp.mkdtemp(), busy.getsockname()[1])
            except RuntimeError as exc:
                assert "already in use" in str(exc)
            else:
                raise AssertionError("busy CDP port accepted")

    # An early exit reports Chrome's own stderr instead of a bare exit code.
    fake = Path(tmp.mkdtemp()) / "chrome"
    fake.write_text("#!/bin/sh\necho 'Failed to move to new namespace' >&2\nexit 5\n"); fake.chmod(0o755)
    with patch.object(browser, "_find_chrome", return_value=str(fake)):
        try:
            browser.launch_cdp_chrome(tmp.mkdtemp(), 1)
        except RuntimeError as exc:
            assert "code 5" in str(exc) and "new namespace" in str(exc), exc
        else:
            raise AssertionError("dead Chrome reported as started")

    # stop_chrome ends the whole process group (renderer/GPU children too).
    group = subprocess.Popen(["sh", "-c", "sleep 60 & sleep 60"], start_new_session=True)
    time.sleep(0.2)
    browser.stop_chrome(group)
    time.sleep(0.2)
    try:
        os.killpg(group.pid, 0)
        raise AssertionError("Chrome child processes survived stop_chrome")
    except ProcessLookupError:
        pass
    print("PASS Chrome launch: origin allow-list, sandbox switch, busy port, crash stderr, process-group stop")


def verify_first_page_and_schedule():
    """One search page per run with every new card; overruns chain at once."""
    import fcntl
    import main as entry
    from boards.base import load_board_selectors
    from boards.indeed.scraper import IndeedJobSpider
    from boards.linkedin.scraper import LinkedInJobSpider
    from boards.wuzzuf.scraper import WuzzufJobSpider
    from config import DB_PATH
    from scrapling.spiders import Request

    async def collect(generator):
        return [item async for item in generator]

    # LinkedIn/Wuzzuf: the parsed first page is everything; no next-page request.
    for spider, url in ((LinkedInJobSpider(load_board_selectors("linkedin"), "http://127.0.0.1:1"),
                         "https://www.linkedin.com/jobs/search/?sortBy=DD"),
                        (WuzzufJobSpider(load_board_selectors("wuzzuf"), "http://127.0.0.1:1"),
                         "https://wuzzuf.net/search/jobs?q=&start=0")):
        spider._page_jobs = [{"external_id": str(i)} for i in range(25)]
        out = asyncio.run(collect(spider.parse(SimpleNamespace(url=url))))
        assert out == spider._page_jobs and not any(isinstance(item, Request) for item in out)
    # Indeed: every new card gets a detail request (the old cap was 10).
    indeed = IndeedJobSpider(load_board_selectors("indeed"), "http://127.0.0.1:1")
    indeed._page_jobs = [{"external_id": f"k{i}", "extra": {}} for i in range(14)]
    out = asyncio.run(collect(indeed.parse(SimpleNamespace(url="https://eg.indeed.com/jobs"))))
    assert len(out) == 14 and all(isinstance(item, Request) for item in out)
    assert not any(job["extra"].get("detail_status") for job in indeed._page_jobs)

    # Ticks sit on epoch multiples of the interval.
    assert not entry._missed_tick(180 * 1000 + 2, 180 * 1000 + 179, minutes=3)
    assert entry._missed_tick(180 * 1000 + 2, 180 * 1001 + 1, minutes=3)

    def lock_is_free():
        with open(DB_PATH + ".scraper.lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)  # raises if still held

    with ExitStack() as stack:
        stack.enter_context(patch.object(entry, "_install_signal_handlers"))
        stack.enter_context(patch.object(entry, "_run_boards", return_value=0))
        chain = stack.enter_context(patch.object(entry, "_start_next_cycle_now", side_effect=lock_is_free))
        missed = stack.enter_context(patch.object(entry, "_missed_tick", return_value=True))
        with patch.object(entry.sys, "argv", ["main.py", "--scheduled"]):
            assert entry.main() == 0 and chain.call_count == 1, "an overrun must chain the next cycle"
        with patch.object(entry.sys, "argv", ["main.py"]):
            entry.main()
        assert chain.call_count == 1, "manual runs never chain"
        missed.return_value = False
        with patch.object(entry.sys, "argv", ["main.py", "--scheduled"]):
            entry.main()
        assert chain.call_count == 1, "a cycle that ended before the next tick waits for ofelia"
    with patch.object(entry.os, "execv") as execv, patch.object(entry.logging, "shutdown"), \
            patch.object(entry.sys, "argv", ["main.py", "--scheduled"]):
        entry._start_next_cycle_now()
    assert execv.call_args.args[1][1:] == ["main.py", "--scheduled"]
    print("PASS first page only (all cards, no Indeed detail cap), overrun chains the next cycle after releasing the lock")

    # A broken selectors.json (live-editable bind mount) skips only that board,
    # alerts once per episode, and the exit code reports the failure.
    ran = []
    class Broken:
        name, enabled = "wuzzuf", True
        def __init__(self): raise ValueError("Expecting ',' delimiter: line 3 column 5")
    class Healthy:
        name, enabled = "indeed", True
        def run(self): ran.append(self.name); return 1
    from core import telegram as tg
    with patch.object(entry, "BOARDS", [Broken, Healthy]), patch.object(entry, "ENRICHMENT_ENABLED", True), \
            patch.object(tg, "notify_failure", return_value=True) as alert:
        assert entry._run_boards() == 1 and ran == ["indeed"]
        assert entry._run_boards() == 1 and ran == ["indeed", "indeed"]
        assert alert.call_count == 1 and "selectors.json" in alert.call_args.args[1]
    with entry.db.get_db() as conn:
        assert conn.execute("SELECT status FROM runs WHERE source='wuzzuf' ORDER BY id DESC LIMIT 1").fetchone()[0] == "config_error"
    with patch.object(entry, "BOARDS", [Healthy]), patch.object(entry, "ENRICHMENT_ENABLED", True):
        assert entry._run_boards() == 0
    print("PASS broken selectors skip only their board, alert once, non-zero exit")


def verify_linkedin_login():
    """Rejected credentials stop logins; checkpoints never wipe; one wipe per streak."""
    import boards.linkedin as li_board
    from boards.linkedin import login
    from boards.base import load_board_selectors
    from core import login_state, markup
    sel = load_board_selectors("linkedin")

    class Element:
        def __init__(self, text, visible=True): self.text, self.visible = text, visible
        def is_visible(self): return self.visible
        def text_content(self): return self.text
    class Locator:
        def __init__(self, elements): self.elements = elements
        def all(self): return self.elements
    class Page:
        def __init__(self, url, errors=(), redirect_to=None, alerts=(), cookies=()):
            self.url, self.errors, self.redirect_to = url, list(errors), redirect_to
            self.alerts = list(alerts)
            self.context = SimpleNamespace(cookies=lambda _url=None: list(cookies))
        def locator(self, css):
            # Field-level credential errors vs page-level sign-in alerts.
            return Locator(self.errors if css == sel["login"]["credential_error"] else self.alerts)
        def wait_for_url(self, pattern, timeout=None, wait_until=None):
            assert wait_until == "commit", "LinkedIn never fires load"
            if self.redirect_to:
                self.url = self.redirect_to
            if not pattern.search(self.url):
                raise TimeoutError("still on the sign-in form")
        def wait_for_timeout(self, _ms): pass
        def content(self): return "<html></html>"

    with ExitStack() as stack:
        alerts = stack.enter_context(patch.object(login, "notify_failure", return_value=True))
        stack.enter_context(patch.object(markup, "save_snapshot", return_value="test/login.html"))
        stack.enter_context(patch.object(login, "LINKEDIN_EMAIL", "user@example.com"))
        stack.enter_context(patch.object(login, "LINKEDIN_PASSWORD", "old-password"))
        stack.enter_context(patch.object(li_board, "LINKEDIN_EMAIL", "user@example.com"))
        stack.enter_context(patch.object(li_board, "LINKEDIN_PASSWORD", "old-password"))
        login_state.reset_retries()

        # An active session whose /login -> /feed redirect lands after
        # DOMContentLoaded is logged in, never a failure (Sep-Oct snapshots).
        late = Page("https://www.linkedin.com/login", redirect_to="https://www.linkedin.com/feed/")
        assert login.ensure_logged_in(late, sel) and login_state.get_retry_count() == 0
        # A failure path that finds the feed after settling reports success.
        assert login._fail(Page("https://www.linkedin.com/feed/"), "other", "automation error")
        assert login_state.get_retry_count() == 0 and not alerts.called

        # Hidden or empty error placeholders are not a rejection.
        quiet = Page("https://www.linkedin.com/login", errors=[Element("Wrong password", visible=False), Element("  ")])
        assert login._classify(quiet, sel) is None and not login_state.credentials_locked("user@example.com", "old-password")

        # A visible rejection locks logins with one alert; the board skips Chrome.
        rejected = Page("https://www.linkedin.com/login", errors=[Element("Wrong email or password. Try again.")])
        assert login._classify(rejected, sel) is False
        assert alerts.call_count == 1 and "rejected" in alerts.call_args.args[0]
        assert "Wrong email or password" in alerts.call_args.args[1]
        with patch.object(li_board, "chrome_session", side_effect=AssertionError("Chrome must not start")):
            for _ in range(3):
                assert li_board.LinkedInBoard().run() == 0
        with login_state.db.get_db() as conn:
            assert conn.execute("SELECT status FROM runs WHERE source='linkedin' ORDER BY id DESC LIMIT 1").fetchone()[0] == "credentials_rejected"
        assert alerts.call_count == 1, "a rejected login must alert once, not every run"
        assert "old-password" not in login_state.db.get_state(login_state.REJECTED_KEY)
        # Changed credentials release the lock and the streak by themselves.
        assert not login_state.credentials_locked("user@example.com", "new-password")
        assert login_state.db.get_state(login_state.REJECTED_KEY) == ""
        login_state.lock_credentials("user@example.com", "new-password")
        login_state.reset_retries()  # --reset-login also clears it
        assert not login_state.credentials_locked("user@example.com", "new-password")

        def expire_cooldown():
            login_state.db.set_state(login_state.BLOCKED_KEY, 0)

        # A page-level alert ("try again later") is transient: cooldown only,
        # never a credential lock, and it does not earn a profile wipe.
        alerts.reset_mock()
        busy = Page("https://www.linkedin.com/login", alerts=[Element("Unusual activity. Please try again later.")])
        assert login._classify(busy, sel) is False
        assert not login_state.credentials_locked("user@example.com", "new-password")
        assert login_state.get_retry_count() == 1 and login_state.is_blocked() and not alerts.called
        for _ in range(2):
            expire_cooldown()
            login._register_failure("alert", "sign-in alert", error_text="Unusual activity. Please try again later.")
        expire_cooldown()
        assert login_state.max_retries_reached() and not login_state.should_wipe_profile()
        assert "sign-in alert" in alerts.call_args.args[1] and "Unusual activity" in alerts.call_args.args[1]
        login_state.reset_retries()

        # /jobs counts as logged in only with LinkedIn's session cookie.
        assert login._is_logged_in(Page("https://www.linkedin.com/jobs/", cookies=[{"name": "li_at", "value": "x"}]))
        assert not login._is_logged_in(Page("https://www.linkedin.com/jobs/"))
        assert not login._is_logged_in(Page("https://www.linkedin.com/login", cookies=[{"name": "li_at", "value": "x"}]))

        # Unsolved checkpoints keep cooling down but never wipe the profile.
        for _ in range(4):
            login._register_failure("checkpoint", "checkpoint unresolved")
            expire_cooldown()
        assert login_state.max_retries_reached() and not login_state.should_wipe_profile()
        assert "checkpoint or sign-in alert is involved" in alerts.call_args.args[1]
        login_state.reset_retries()

        # Other failures earn exactly one wipe per streak, then cooldowns only.
        alerts.reset_mock()
        for _ in range(3):
            login._register_failure("other", "automation error")
        assert alerts.call_count == 1 and "wiped once" in alerts.call_args.args[1]
        expire_cooldown()
        assert login_state.should_wipe_profile()
        login_state.mark_profile_wiped()
        for _ in range(5):
            login._register_failure("other", "automation error")
            expire_cooldown()
        assert not login_state.should_wipe_profile()
        assert alerts.call_count == 2 and "already failed" in alerts.call_args.args[1]
        login_state.reset_retries()
    print("PASS LinkedIn login: late feed redirect, credential lock vs transient alerts, cookie-checked /jobs, checkpoint never wipes, one wipe per streak")



def verify_browser_recovery():
    from boards.linkedin import scraper as li
    from boards.base import load_board_selectors
    from core.browser_diagnostics import BrowserDiagnostics
    from core import db

    class Request:
        def __init__(self, kind="script", failure="net::ERR_NETWORK_CHANGED", host="static.licdn.com"):
            self.url = "https://" + host + "/private/path?token=never-log-this"
            self.resource_type = kind
            self.failure = failure
    class EventPage:
        def __init__(self): self.listeners = {}
        def on(self, event, callback): self.listeners.setdefault(event, []).append(callback)
        def emit(self, event, value):
            for callback in self.listeners.get(event, []): callback(value)
    diagnostics = BrowserDiagnostics({"www.linkedin.com", "static.licdn.com"})
    emitter = EventPage()
    diagnostics.attach(emitter)
    diagnostics.attach(emitter)
    assert all(len(callbacks) == 1 for callbacks in emitter.listeners.values())
    req = Request()
    emitter.emit("request", req)
    assert len(diagnostics.pending) == 1
    emitter.emit("requestfailed", req)
    assert not diagnostics.pending and "ERR_NETWORK_CHANGED" in diagnostics.summary()
    assert "private" not in diagnostics.summary() and "never-log-this" not in diagnostics.summary()
    diagnostics.reset()
    for req in (Request("stylesheet"), Request(failure="net::ERR_ABORTED"), Request(host="ads.example.com")):
        emitter.emit("requestfailed", req)
    assert not diagnostics.events
    emitter.emit("response", SimpleNamespace(request=Request("document", host="www.linkedin.com"), status=503))
    assert "HTTP:" in diagnostics.summary() and "503" in diagnostics.summary()
    emitter.emit("response", SimpleNamespace(request=Request("xhr", host="www.linkedin.com"), status=429))
    assert diagnostics.access_status == 429

    sel = load_board_selectors("linkedin")
    loading = (ROOT / "markup/linkedin/loading_shell_sample.html").read_text()
    ready_html = '<ul class="scaffold-layout__list"><li class="scaffold-layout__list-item" data-occludable-job-id="123"></li></ul>'
    class Card:
        def __init__(self, page): self.page = page; self.clicks = 0
        async def get_attribute(self, name): return "123"
        async def scroll_into_view_if_needed(self, **kwargs): pass
        async def click(self, **kwargs):
            self.clicks += 1
            if self.clicks >= 2 and self.page.detail_recovers: self.page.detail_id = "123"
    class Locator:
        def __init__(self, page): self.page = page; self.first = self
        async def evaluate(self, *args, **kwargs): pass
        async def all(self): return [self.page.card]
    class Page(EventPage):
        url = "https://www.linkedin.com/jobs/search/"
        def __init__(self, recovers=True):
            super().__init__()
            self.recovers = recovers; self.ready = False; self.reloads = 0
            self.detail_mode = False; self.detail_id = "old"; self.detail_recovers = True
            self.card = Card(self)
        async def content(self): return ready_html if self.ready else loading
        def locator(self, css): return Locator(self)
        async def wait_for_load_state(self, *args, **kwargs): pass
        async def goto(self, url, **kwargs):
            assert 0 < kwargs["timeout"] <= 30000
            self.reloads += 1
            self.ready = self.recovers
            if not self.ready: self.emit("requestfailed", Request())
        async def wait_for_selector(self, css, **kwargs):
            assert kwargs["timeout"] > 0
            if self.detail_mode:
                if "/jobs/view/123/" in css and self.detail_id != "123":
                    raise TimeoutError("previous job still visible")
                return
            if not self.ready: raise TimeoutError("loading")

    async def scenarios():
        recovered = li.LinkedInJobSpider(sel, "http://127.0.0.1:1")
        page = Page()
        await recovered.setup_search_page(page)
        with patch.object(db, "seen_ids_for", return_value={"123"}):
            await recovered.deep_scan_page(page)
        assert page.reloads == 1 and recovered.health.status == "ok"
        assert not recovered._page_jobs  # all-seen remains normal after recovery

        stalled = li.LinkedInJobSpider(sel, "http://127.0.0.1:1")
        page = Page(recovers=False)
        await stalled.setup_search_page(page)
        await stalled.deep_scan_page(page)
        assert page.reloads == 1 and stalled.health.status == "degraded"
        assert "ERR_NETWORK_CHANGED" in stalled.health.error

        for restricted in ("checkpoint", "rate_limit"):
            blocked = li.LinkedInJobSpider(sel, "http://127.0.0.1:1")
            page = Page()
            if restricted == "checkpoint": page.url = "https://www.linkedin.com/checkpoint/challenge"
            else: blocked.diagnostics.access_status = 429
            await blocked.deep_scan_page(page)
            assert page.reloads == 0 and blocked.health.status == "degraded"

        detail = li.LinkedInJobSpider(sel, "http://127.0.0.1:1")
        page = Page(); page.detail_mode = True
        assert await detail._open_card(page, page.card, "123")
        assert page.card.clicks == 2 and not detail._detail_failures
        page = Page(); page.detail_mode = True; page.detail_recovers = False
        assert not await detail._open_card(page, page.card, "123")
        assert page.card.clicks == 2 and "123" in detail._detail_failures
        page = Page(); page.detail_mode = True; page.url = "https://www.linkedin.com/login"
        assert not await detail._open_card(page, page.card, "123")
        assert page.card.clicks == 1

        failed = li.LinkedInJobSpider(sel, "http://127.0.0.1:1")
        failed.health.check("search_fetch", True)  # earlier page worked
        await failed.on_error(None, RuntimeError("net::ERR_CONNECTION_CLOSED https://private/?token=secret"))
        assert failed.health.status == "degraded" and "ERR_CONNECTION_CLOSED" in failed.health.error
        assert "token" not in failed.health.error

    with patch.object(li, "LINKEDIN_SEARCH_RECOVERY_ATTEMPTS", 1), patch.object(asyncio, "sleep", new=AsyncMock()):
        asyncio.run(scenarios())
    print("PASS network/HTTP diagnostics, bounded search/detail recovery, job identity and access-stop cases")


if __name__ == "__main__":
    main()
