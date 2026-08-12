"""Indeed job spider.

Indeed is Cloudflare-gated (403 Security Check for plain curl — see
INDEED.md) and embeds all its data as JSON inside <script> tags, so we fetch
the search page through a stealth browser session with solve_cloudflare=True
and parse the blobs — no CSS selectors needed.

Data sources:
1. Search page: `window.mosaic.providerData["mosaic-provider-jobcards"]`
   -> metaData.mosaicProviderJobCardsModel.results — one dict per job card
   (jobkey, displayTitle, company, formattedLocation, extractedSalary,
   jobTypes, pubDate in unix ms, snippet, viewJobLink).
2. View job page (followed per NEW jobkey only):
   - `window._initialData` -> ...jobData.results[0].job.description.text
     (clean plain-text description, latitude/longitude) — PRIMARY
   - <script type="application/ld+json"> (Schema.org JobPosting) — fallback
     with HTML description + baseSalary.

Single search URL, sort=date, NO pagination (pagination is login-gated).
"""

import asyncio
import json
import random
import re
from datetime import datetime

from scrapling import Selector
from scrapling.fetchers import AsyncStealthySession
from scrapling.spiders import Request, Response, Spider

from config import INDEED_SEARCH_URL
from core import db, markup
from core.browser import patch_no_load_wait

_BASE_URL = "https://eg.indeed.com"

# Cap per run: each new job means one extra detail-page navigation.
_MAX_DETAIL_FETCHES = 10

_CARDS_MARKER = re.compile(
    r'window\.mosaic\.providerData\[\'?"?mosaic-provider-jobcards\'?"?\]\s*=\s*'
)
_VIEWJOB_MARKER = re.compile(r"window\._initialData\s*=\s*")
_LDJSON = re.compile(
    r'<script[^>]*type=[\'"]application/ld\+json[\'"][^>]*>(.*?)</script>',
    re.DOTALL,
)


def _dig(obj, *keys):
    """Safely walk nested dicts; returns None when any level is missing."""
    for key in keys:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def _extract_balanced_json(html: str, marker_re: re.Pattern) -> dict:
    """Locate `marker` in the HTML and parse the following `{...}` object.

    Marker regex must end right before the opening brace (it usually matches
    `... = `). Brace balancing is string-aware (handles braces inside quoted
    strings and escaped quotes). Returns {} when absent/unparseable.
    """
    m = marker_re.search(html or "")
    if not m:
        return {}
    start = html.find("{", m.end())
    if start == -1:
        return {}

    depth = 0
    in_str = False
    esc = False
    k = start
    while k < len(html):
        ch = html[k]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    break
        k += 1

    if depth != 0:
        print("[indeed] Unbalanced JSON blob — marker found, brace never closed.")
        return {}

    try:
        return json.loads(html[start:k + 1])
    except (ValueError, TypeError) as e:
        print(f"[indeed] Could not parse embedded JSON: {e}")
        return {}


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _strip_html(html_text: str) -> str:
    """Convert an HTML fragment (snippet/description) to plain text."""
    if not html_text:
        return ""
    try:
        text = Selector(html_text).get_all_text()
    except Exception:
        text = re.sub(r"<[^>]+>", " ", html_text)
    return _clean(text)


def _parse_pubdate(value) -> str:
    """Unix ms -> local 'YYYY-MM-DD HH:MM' (container TZ = Africa/Cairo)."""
    try:
        return datetime.fromtimestamp(int(value) / 1000).strftime(
            "%Y-%m-%d %H:%M"
        )
    except (ValueError, TypeError):
        return datetime.now().strftime("%Y-%m-%d %H:%M")


def _jobkey_from_url(url: str) -> str:
    m = re.search(r"[?&]jk=([^&]+)", url or "")
    return m.group(1) if m else ""


def _extract_jobs(html: str, seen_ids: set) -> tuple[list, int, bool]:
    """Parse the search-page job cards blob.

    Returns (new_jobs, seen_count, blob_missing). `blob_missing` is True
    when the cards JSON was absent (CF challenge page, empty page, redesign).
    """
    data = _extract_balanced_json(html, _CARDS_MARKER)
    if not data:
        return [], 0, True

    results = _dig(data, "metaData", "mosaicProviderJobCardsModel", "results")
    if not isinstance(results, list):
        return [], 0, True

    jobs: list[dict] = []
    seen = 0
    for item in results:
        if not isinstance(item, dict):
            continue
        key = item.get("jobkey")
        if not key:
            continue
        if str(key) in seen_ids:
            seen += 1
            continue

        company = item.get("company") or ""
        if not isinstance(company, str):
            company = (company or {}).get("name", "") if isinstance(company, dict) else ""

        snippet = _strip_html(item.get("snippet"))
        extra: dict = {
            "location": _clean(
                item.get("formattedLocation") or item.get("jobLocationCity") or ""
            ),
        }
        if snippet:
            extra["snippet"] = snippet

        salary = item.get("extractedSalary")
        if isinstance(salary, dict) and (
            salary.get("min") is not None or salary.get("max") is not None
        ):
            extra["salary"] = {
                "min": salary.get("min"),
                "max": salary.get("max"),
                "type": salary.get("type"),
                "currency": salary.get("currency"),
            }

        job_types = [t for t in (item.get("jobTypes") or []) if isinstance(t, str)]
        if job_types:
            extra["job_types"] = job_types
        if item.get("sponsored"):
            extra["sponsored"] = True

        jobs.append(
            {
                "source": "indeed",
                "external_id": str(key),
                "title": _clean(item.get("displayTitle") or item.get("normTitle") or ""),
                "company": _clean(company),
                # createDate is the precise ms epoch; pubDate is normalized
                # to midnight — prefer createDate.
                "posted_at": _parse_pubdate(
                    item.get("createDate") or item.get("pubDate")
                ),
                # Placeholder: upgraded to the full description when the
                # viewjob page is fetched (see scan_detail_page).
                "description": snippet,
                "link": f"{_BASE_URL}/viewjob?jk={key}",
                "extra": extra,
                "scraped_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            }
        )

    return jobs, seen, False


def _ldjson_objects(html: str):
    """Yield parsed JSON from every application/ld+json script block."""
    for m in _LDJSON.finditer(html or ""):
        try:
            obj = json.loads(m.group(1))
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict):
            yield obj
        elif isinstance(obj, list):
            yield from (o for o in obj if isinstance(o, dict))


def _extract_detail(html: str) -> tuple[str, dict]:
    """Parse a viewjob page. Returns (description, extra dict).

    PRIMARY: window._initialData -> jobData.results[0].job.description
    (.text, else .html stripped) + latitude/longitude. The results list
    sits under hostQueryExecutionResult.data.jobData.results on the
    viewjob page itself, and under
    autoOpenTwoPaneViewjobResponse.body.hostQueryExecutionResult... on the
    search page's two-pane blob — try both.
    FALLBACK: Schema.org JobPosting ld+json (HTML description + baseSalary).
    """
    extra: dict = {}
    data = _extract_balanced_json(html, _VIEWJOB_MARKER)

    if data:
        job = None
        for base in (
            _dig(data, "hostQueryExecutionResult", "data", "jobData", "results"),
            _dig(
                data,
                "autoOpenTwoPaneViewjobResponse",
                "body",
                "hostQueryExecutionResult",
                "data",
                "jobData",
                "results",
            ),
        ):
            if isinstance(base, list) and base and isinstance(base[0], dict):
                job = base[0].get("job")
                break

        if isinstance(job, dict):
            desc = job.get("description") or {}
            text = _clean(desc.get("text"))
            if not text and desc.get("html"):
                text = _strip_html(desc["html"])

            geo = job.get("location") or {}
            if isinstance(geo, dict):
                if geo.get("latitude") is not None:
                    extra["latitude"] = geo["latitude"]
                if geo.get("longitude") is not None:
                    extra["longitude"] = geo["longitude"]

            if text:
                return text, extra

    # Fallback: Schema.org JobPosting block (prefer the typed one).
    ld_obj = None
    for obj in _ldjson_objects(html):
        types = obj.get("@type")
        if isinstance(types, list) and "JobPosting" in types or types == "JobPosting":
            ld_obj = obj
            break
        if ld_obj is None:
            ld_obj = obj
    if ld_obj:
        desc = _strip_html(ld_obj.get("description"))
        if desc:
            return desc, extra
        base = _dig(ld_obj, "baseSalary", "value")
        if isinstance(base, dict) and (
            base.get("minValue") is not None or base.get("maxValue") is not None
        ):
            extra["salary"] = {
                "min": base.get("minValue"),
                "max": base.get("maxValue"),
                "currency": _dig(ld_obj, "baseSalary", "currency"),
            }

    return "", extra


class IndeedJobSpider(Spider):
    name = "indeed_job_spider"

    def __init__(self, selectors: dict, cdp_url: str, *args, **kwargs):
        self.sel = selectors  # unused — data comes from JSON blobs, kept for parity
        self.cdp_url = cdp_url
        self.seen_ids = db.load_seen_ids("indeed")

        self._page_jobs: list[dict] = []
        self._pending: dict[str, dict] = {}  # jobkey -> placeholder job
        self._detail_jobs: dict[str, dict] = {}  # jobkey -> enriched job
        self._queued: set[str] = set()
        self._detail_snapshots = 0

        super().__init__(*args, **kwargs)

    def configure_sessions(self, manager):
        manager.add(
            "stealth",
            AsyncStealthySession(
                cdp_url=self.cdp_url,
                solve_cloudflare=True,
                timeout=120_000,
                page_setup=patch_no_load_wait,
            ),
        )

    async def start_requests(self):
        yield Request(
            INDEED_SEARCH_URL,
            callback=self.parse,
            sid="stealth",
            page_action=self.scan_search_page,
        )

    async def scan_search_page(self, page):
        self._page_jobs = []

        try:
            await page.wait_for_timeout(2500)  # let the SSR blobs land
            html = await page.content()
        except Exception as e:
            print(f"[indeed] Could not read search page: {e}")
            return

        if "INDEED_CLOUDFLARE_STATIC_PAGE" in html:
            print("[indeed] Cloudflare challenge page detected.")
            markup.save_snapshot("indeed", "cloudflare_challenge", html)
            return

        jobs, seen, blob_missing = _extract_jobs(html, self.seen_ids)
        self._page_jobs = jobs
        for job in jobs:
            self.seen_ids.add(job["external_id"])

        print(
            f"[indeed] {len(jobs)} new, {seen} already seen"
            f" ({blob_missing and 'blob MISSING' or 'blob ok'})"
        )
        if blob_missing and not jobs:
            markup.save_snapshot("indeed", "search_empty", html)

    async def scan_detail_page(self, page):
        key = _jobkey_from_url(page.url)
        job = self._pending.get(key)
        if job is None:
            return

        await asyncio.sleep(random.uniform(1.5, 3.0))  # human-like pacing

        try:
            html = await page.content()
        except Exception as e:
            print(f"[indeed] Detail read failed for {key}: {e}")
            html = ""

        desc, extra = _extract_detail(html)
        if desc:
            job["description"] = desc
        else:
            print(f"[indeed] No description parsed for {key}")
            if self._detail_snapshots < 2:
                markup.save_snapshot("indeed", "detail_no_desc", html)
                self._detail_snapshots += 1
        for field, value in extra.items():
            job["extra"].setdefault(field, value)

        self._detail_jobs[key] = job

    async def parse(self, response: Response):
        for job in self._page_jobs:
            key = job["external_id"]
            if key in self._queued or len(self._queued) >= _MAX_DETAIL_FETCHES:
                # Detail cap hit — keep the snippet as the description.
                print(f"[indeed] Detail fetch skipped for {key} (cap reached)")
                yield job
                continue

            self._pending[key] = job
            self._queued.add(key)
            yield Request(
                f"{_BASE_URL}/viewjob?jk={key}",
                callback=self.parse_job_detail,
                sid="stealth",
                page_action=self.scan_detail_page,
            )

    async def parse_job_detail(self, response: Response):
        key = _jobkey_from_url(response.url)
        job = self._detail_jobs.pop(key, None)
        if job is not None:
            yield job


def scrape(selectors: dict, cdp_url: str) -> list[dict]:
    """Run the spider and return the scraped job dicts."""
    spider = IndeedJobSpider(selectors=selectors, cdp_url=cdp_url)
    result = spider.start()
    items = list(result.items)
    print(
        f"[indeed] {len(items)} item(s) scraped in {result.stats.elapsed_seconds:.1f}s"
    )
    return items
