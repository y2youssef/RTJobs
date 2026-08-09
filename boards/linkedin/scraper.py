"""LinkedIn job spider built on scrapling's Spider framework.

Structure verified against the scrapling docs (spiders/sessions.html and
fetching/stealthy.html): configure_sessions + manager.add, requests routed
with sid="stealth", and per-request page_action callbacks.
"""

import asyncio
import random
import re
from datetime import datetime, timedelta

from scrapling import Selector
from scrapling.fetchers import AsyncStealthySession
from scrapling.spiders import Request, Response, Spider

from config import CHROME_ARGS, HEADLESS, LINKEDIN_PROFILE_DIR, LINKEDIN_SEARCH_URL
from core import db, markup
from core.browser import patch_no_load_wait

_MAX_START = 100
_PAGE_SIZE = 25
_DETAIL_TIMEOUT = 8_000

_TIME_DELTAS = {
    "minute": lambda v: timedelta(minutes=v),
    "hour": lambda v: timedelta(hours=v),
    "day": lambda v: timedelta(days=v),
    "week": lambda v: timedelta(weeks=v),
    "month": lambda v: timedelta(days=v * 30),
}


def _parse_linkedin_time(time_str: str) -> str:
    if not time_str:
        return datetime.now().strftime("%Y-%m-%d %H:%M")
    match = re.search(r"(\d+)\s+(minute|hour|day|week|month)", time_str)
    if not match:
        return datetime.now().strftime("%Y-%m-%d %H:%M")
    value, unit = int(match.group(1)), match.group(2)
    return (datetime.now() - _TIME_DELTAS[unit](value)).strftime("%Y-%m-%d %H:%M")


def _text(sel: Selector, css: str, separator: str = "") -> str:
    el = sel.css(css).first
    if not el:
        return ""
    text = el.get_all_text(separator=separator) if separator else el.get_all_text()
    return (text or "").strip()


class LinkedInJobSpider(Spider):
    name = "linkedin_job_spider"

    def __init__(self, selectors: dict, *args, **kwargs):
        self.sel = selectors
        self.seen_ids = db.load_seen_ids("linkedin")

        self._page_jobs: list[dict] = []
        self._repeat_found: bool = False
        self._login_redirect: bool = False

        super().__init__(*args, **kwargs)

    def configure_sessions(self, manager):
        manager.add(
            "stealth",
            AsyncStealthySession(
                headless=HEADLESS,
                user_data_dir=LINKEDIN_PROFILE_DIR,
                real_chrome=True,
                extra_flags=CHROME_ARGS,
                disable_resources=True,
                timeout=60_000,
                page_setup=patch_no_load_wait,
            ),
        )

    async def start_requests(self):
        yield Request(
            LINKEDIN_SEARCH_URL,
            callback=self.parse,
            sid="stealth",
            page_action=self.deep_scan_page,
        )

    async def deep_scan_page(self, page):
        self._page_jobs = []
        found_at_least_one_duplicate = False

        try:
            await page.wait_for_selector(
                self.sel["search"]["job_card"], timeout=30_000
            )

            pane = page.locator(self.sel["search"]["results_list"]).first
            for _ in range(4):
                await pane.evaluate("el => el.scrollTop += 1000")
                await asyncio.sleep(0.8)
        except Exception as e:
            print(f"[warn] Card list never appeared: {e}")
            return

        cards = await page.locator(self.sel["search"]["job_card"]).all()

        jobs_to_scrape_now = []
        for card in cards:
            job_id = await card.get_attribute(self.sel["search"]["job_id_attr"])
            if not job_id:
                continue

            if str(job_id) in self.seen_ids:
                found_at_least_one_duplicate = True
            else:
                jobs_to_scrape_now.append((card, job_id))

        print(
            f"[info] Found {len(jobs_to_scrape_now)} new jobs and"
            f" {len(cards) - len(jobs_to_scrape_now)} old jobs on this page."
        )

        for card, job_id in jobs_to_scrape_now:
            await asyncio.sleep(random.uniform(2.0, 4.0))  # human-like pause
            job = await self._scrape_card(page, card, job_id)
            if job:
                self._page_jobs.append(job)
                self.seen_ids.add(str(job_id))

        if found_at_least_one_duplicate:
            print("[stop] Duplicate detected on this page — no next page.")
            self._repeat_found = True

        if not jobs_to_scrape_now and len(cards) > 0:
            self._repeat_found = True

        # Suspicious empty page or login redirect -> keep markup for debugging
        if (not jobs_to_scrape_now and len(cards) == 0) or self._login_redirect:
            html = await page.content()
            kind = "search_redirect" if self._login_redirect else "search_empty"
            markup.save_snapshot("linkedin", kind, html)

    async def _scrape_card(self, page, card, job_id: str) -> dict | None:
        try:
            await card.scroll_into_view_if_needed()
            await asyncio.sleep(random.uniform(0.8, 2.2))
            await card.click()

            try:
                await page.wait_for_selector(
                    self.sel["search"]["detail_panel"], timeout=_DETAIL_TIMEOUT
                )
            except Exception:
                print(f"[warn] Detail panel timed out for {job_id} — skipping")
                return None

            if (
                await page.locator(self.sel["search"]["login_redirect"]).count()
                > 0
            ):
                print("[warn] Redirected to login — session may have expired")
                self._login_redirect = True
                return None

            await asyncio.sleep(random.uniform(1.5, 4.0))

            html = await page.content()
            sel = Selector(html)
            d = self.sel["job_detail"]

            title = _text(sel, d["title"])
            company = _text(sel, d["company"])
            rel_time = _text(sel, d["posted_at"])
            desc = _text(sel, d["description"], separator="\n")
            mgr_name = _text(sel, d["hiring_manager_name"])
            mgr_role = _text(sel, d["hiring_manager_role"])

            print(f"  [ok] {title[:45]}")
            return {
                "source": "linkedin",
                "external_id": str(job_id),
                "title": title,
                "company": company,
                "posted_at": _parse_linkedin_time(rel_time),
                "description": desc,
                "link": f"https://www.linkedin.com/jobs/view/{job_id}/",
                "extra": {
                    "hiring_manager_name": mgr_name,
                    "hiring_manager_role": mgr_role,
                },
                "scraped_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            }
        except Exception as e:
            print(f"[error] Job {job_id}: {e}")
            return None

    async def parse(self, response: Response):
        for job in self._page_jobs:
            yield job

        if self._repeat_found or self._login_redirect:
            return

        match = re.search(r"start=(\d+)", response.url)
        start_val = int(match.group(1)) if match else 0
        next_start = start_val + _PAGE_SIZE

        if next_start >= _MAX_START:
            return

        next_url = (
            re.sub(r"start=\d+", f"start={next_start}", response.url)
            if "start=" in response.url
            else response.url + f"&start={next_start}"
        )
        print(f"[page] → page {next_start // _PAGE_SIZE + 1}")
        yield Request(
            next_url,
            callback=self.parse,
            sid="stealth",
            page_action=self.deep_scan_page,
        )


def scrape(selectors: dict) -> dict:
    """Run the spider. Returns {'items': [...], 'login_redirect': bool}."""
    spider = LinkedInJobSpider(selectors=selectors)
    result = spider.start()
    items = list(result.items)
    print(
        f"[spider] {len(items)} item(s) scraped in {result.stats.elapsed_seconds:.1f}s"
    )
    return {"items": items, "login_redirect": spider._login_redirect}
