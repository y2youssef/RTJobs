"""NaukriGulf job spider.

NaukriGulf is a client-rendered app behind Akamai Bot Manager (plain HTTP
clients and fresh automated browsers get HTTP/2 stream resets; see
NAUKRIGULF.md). The HTML is only a splash screen: the page's own JS fetches
the job list from `/spapi/jobapi/search` — the newest 30 jobs, date-sorted,
each with its FULL HTML description, exact `latestPostedDate` (epoch
seconds), company, location and experience range. No per-job detail page.

We never call that API ourselves (its headers, cookies and the Akamai
sensor belong to the page). The spider loads the search page in our real
Chrome on a persistent profile, so the site's trust history accumulates,
and reads the response the page receives. First page only; every job on it
is checked.
"""

import asyncio
import logging
import re
import time
from datetime import datetime

from scrapling import Selector
from scrapling.fetchers import AsyncStealthySession
from scrapling.spiders import Request, Response, Spider

from config import NAUKRIGULF_SEARCH_URL
from core import clock, db, timing
from core.browser import patch_no_load_wait
from core.scrape_health import ScrapeHealth

logger = logging.getLogger(__name__)

# The app requests the list right after its bootstrap XHRs; on a slow link
# that can take a while, but past this the run reports what it saw.
_API_WAIT_SECONDS = 30


def _strip_html(html_text: str) -> str:
    """Convert the HTML description to plain text (same as Wuzzuf)."""
    if not html_text:
        return ""
    try:
        text = Selector(html_text).get_all_text()
    except Exception:
        text = re.sub(r"<[^>]+>", " ", html_text)
    return re.sub(r"\s+", " ", text or "").strip()


def _posted_at(epoch) -> str | None:
    """`latestPostedDate` (epoch seconds, string) as local wall time."""
    try:
        return datetime.fromtimestamp(int(epoch)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _job_from_api(entry: dict, link_base: str) -> dict | None:
    """One API job entry as our job dict; None without a jobId."""
    job_id = str(entry.get("jobId") or "").strip()
    if not job_id:
        return None
    slug = str(entry.get("jdURL") or "").strip()
    link = slug if slug.startswith("http") else (link_base + slug.lstrip("/") if slug else "")
    company = entry.get("company") if isinstance(entry.get("company"), dict) else {}
    experience = entry.get("experience") if isinstance(entry.get("experience"), dict) else {}
    posted = _posted_at(entry.get("latestPostedDate"))
    return {
        "source": "naukrigulf",
        "external_id": job_id,
        "title": (entry.get("designation") or "").strip(),
        "company": (company.get("name") or "").strip(),
        "posted_at": posted or datetime.now().strftime("%Y-%m-%d %H:%M"),
        "description": _strip_html(entry.get("description")),
        "link": link,
        "extra": {
            "location": entry.get("location") or "",
            "summary": entry.get("jobInfo") or "",
            "experience_min": experience.get("min"),
            "experience_max": experience.get("max"),
            "vacancies": entry.get("vacancies"),
            "company_id": company.get("id"),
            "is_consultant": bool(entry.get("isConsultant")),
            "is_confidential_company": bool(entry.get("isConfidentialCompany")),
            "is_sponsored": bool(entry.get("isSponsoredJob")),
            "is_easy_apply": bool(entry.get("isEasyApply")),
            "job_source": entry.get("jobSource"),
            "posted_at_exact": posted is not None,
        },
        "scraped_at": clock.now_str(),  # UTC; posted_at stays local
    }


def _extract_jobs(body: dict, seen_ids: set[str], link_base: str, lookup_seen: bool = False,
                  listings: dict | None = None) -> tuple[list[dict], dict]:
    """New jobs from one search response, plus counts for the health checks.

    `listings` collects the current latestPostedDate of already-known jobs
    for repost detection (core/db.record_listings).
    """
    entries = body.get("jobs") if isinstance(body, dict) else None
    stats = {"entries": None, "known": 0, "no_id": 0, "no_date": 0, "total": None}
    if not isinstance(entries, list):
        return [], stats
    stats["entries"] = len(entries)
    stats["total"] = body.get("totalJobsCount")
    parsed = [(entry, _job_from_api(entry, link_base)) for entry in entries if isinstance(entry, dict)]
    stats["no_id"] = len(entries) - sum(1 for _, job in parsed if job)
    ids = [job["external_id"] for _, job in parsed if job]
    stored = db.seen_ids_for("naukrigulf", ids) if lookup_seen else set()
    known_ids = seen_ids | stored
    jobs = []
    for _, job in parsed:
        if job is None:
            continue
        if not job["extra"]["posted_at_exact"]:
            stats["no_date"] += 1
        if job["external_id"] in known_ids:
            stats["known"] += 1
            if listings is not None and job["external_id"] in stored and job["extra"]["posted_at_exact"]:
                listings[job["external_id"]] = job["posted_at"]
            continue
        jobs.append(job)
    return jobs, stats


class NaukriGulfJobSpider(Spider):
    name = "naukrigulf_job_spider"

    def __init__(self, selectors: dict, cdp_url: str, *args, **kwargs):
        self.sel = selectors
        self.cdp_url = cdp_url
        self.seen_ids: set[str] = set()  # IDs discovered during this run only
        self._page_jobs: list[dict] = []
        self.listings: dict[str, str] = {}  # known job id -> current latestPostedDate
        self.health = ScrapeHealth("naukrigulf")
        # The search API response the page receives, and what went wrong if not.
        self._api_body: dict | None = None
        self._api_status: int | None = None
        self._api_failure: str = ""
        self._api_reads: list = []
        super().__init__(*args, **kwargs)
        from core.log import configure_spider_logging
        configure_spider_logging(self)

    def configure_sessions(self, manager):
        manager.add(
            "stealth",
            AsyncStealthySession(
                cdp_url=self.cdp_url,
                # No resource blocking: interception disables Chrome's HTTP
                # cache (Gotchas #11), and an app whose images/styles never
                # load is one more automation signal for Akamai's sensor.
                disable_resources=False,
                timeout=60_000,
                # One navigation per run: repeated failing visits are what
                # escalates Akamai's verdict (NAUKRIGULF.md).
                retries=1,
                page_setup=self.setup_page,
            ),
        )

    async def setup_page(self, page):
        await patch_no_load_wait(page)
        # Tabs are reused across fetches: attach the listeners once per page.
        if getattr(page, "_rtjobs_naukrigulf_spider", None) is not self:
            page._rtjobs_naukrigulf_spider = self
            page.on("response", self._on_response)
            page.on("requestfailed", self._on_failed)

    def _api_url(self, url: str) -> bool:
        return self.sel["search"]["api_path"] in (url or "")

    def _on_response(self, response):
        if self._api_url(response.url):
            self._api_reads.append(asyncio.ensure_future(self._read_api(response)))

    def _on_failed(self, request):
        if self._api_url(request.url):
            # ERR_HTTP2_PROTOCOL_ERROR here is Akamai's soft-block signature.
            self._api_failure = request.failure or "request failed"

    async def _read_api(self, response):
        self._api_status = response.status
        if response.status != 200:
            return
        try:
            body = await response.json()
        except Exception as exc:
            self._api_failure = f"unreadable JSON ({type(exc).__name__})"
            return
        if isinstance(body, dict) and isinstance(body.get("jobs"), list):
            self._api_body = body

    async def _wait_for_api(self, timeout: float = _API_WAIT_SECONDS):
        deadline = time.monotonic() + timeout
        while self._api_body is None and time.monotonic() < deadline:
            if self._api_failure or (self._api_status not in (None, 200)):
                break
            await asyncio.sleep(0.25)
        pending = [task for task in self._api_reads if not task.done()]
        if pending:
            await asyncio.wait(pending, timeout=2)

    async def _diagnose(self, page) -> str:
        """Why no job list arrived, from what the page and network showed."""
        if self._api_failure:
            return (f"The search API request failed ({self._api_failure}); an HTTP/2 protocol "
                    "error is Akamai's soft-block signature for a flagged browser.")
        if self._api_status not in (None, 200):
            return f"The search API answered HTTP {self._api_status}."
        try:
            html = await page.content()
        except Exception:
            html = ""
        if re.search(r"var\s+puppeteer\s*=\s*true", html):
            return "The site flagged this browser as automation (puppeteer=true); the app never requests jobs."
        if self.sel["search"]["error_text"] in html:
            return f"The app shows its error screen ({self.sel['search']['error_text']!r})."
        return f"The search API response never arrived within {_API_WAIT_SECONDS}s."

    async def start_requests(self):
        yield Request(
            NAUKRIGULF_SEARCH_URL,
            callback=self.parse,
            sid="stealth",
            page_action=self.scan_page,
        )

    @timing.timed("naukrigulf", "search_page", lambda spider, page: {"new_jobs": len(spider._page_jobs)})
    async def scan_page(self, page):
        self._page_jobs = []
        self.health.check("search_fetch", True)
        await self._wait_for_api()
        if self._api_body is None:
            detail = await self._diagnose(page)
            logger.warning(f"[naukrigulf] No job list: {detail}")
            await self.health.page_failure("search_api", detail, page)
            return
        self.health.check("search_api", True)

        body = self._api_body
        jobs, stats = _extract_jobs(body, self.seen_ids, self.sel["search"]["job_link_base"],
                                    lookup_seen=True, listings=self.listings)
        entries = stats["entries"] or 0
        self.health.check("search_structure", entries > 0 or not body.get("totalJobsCount"),
                          f"The search response lists no jobs although totalJobsCount is {body.get('totalJobsCount')}.")
        self.health.check("card_identity", not stats["no_id"],
                          f"{stats['no_id']} of {entries} jobs had no jobId and were skipped (API format change?).")
        self.health.check("posted_date", not stats["no_date"],
                          f"{stats['no_date']} of {entries} jobs had no readable latestPostedDate; "
                          "their posted_at is the scrape time.")
        self.health.job_fields(jobs)

        self._page_jobs = jobs
        for job in jobs:
            self.seen_ids.add(job["external_id"])
        logger.info(f"[naukrigulf] {len(jobs)} new, {stats['known']} already seen of {entries} "
                    f"on the first page ({stats['total']} in the search window)")

    async def parse(self, response: Response):
        # First page only: no follow-up page requests.
        for job in self._page_jobs:
            yield job


def scrape(selectors: dict, cdp_url: str, health: ScrapeHealth | None = None) -> dict:
    """Run the spider. Returns {'items': [...], 'listings': {known id: posted_at}}."""
    spider = NaukriGulfJobSpider(selectors=selectors, cdp_url=cdp_url)
    if health is not None:
        spider.health = health
    result = spider.start()
    items = list(result.items)
    spider.health.report()
    logger.info(f"[naukrigulf] {len(items)} item(s) scraped in {result.stats.elapsed_seconds:.1f}s")
    return {"items": items, "listings": spider.listings}
