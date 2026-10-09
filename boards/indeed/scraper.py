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
   - `window._rootProps.preloadedVJData` (current standalone layout)
   - `window._initialData` -> ...jobData.results[0].job.description.text
     (older standalone and two-pane layouts)
   - <script type="application/ld+json"> (Schema.org JobPosting) supplements
     description, salary, employment types, location and expiry.

Single search URL, sort=date, NO pagination (pagination is login-gated).
"""

import logging
import asyncio
import json
import random
import re
from datetime import datetime
from functools import partial

from scrapling import Selector
from scrapling.fetchers import AsyncStealthySession
from scrapling.spiders import Request, Response, Spider

from config import INDEED_SEARCH_URL
from core import clock, db, markup, timing
from core.browser import patch_no_load_wait
from core.scrape_health import ScrapeHealth

logger = logging.getLogger(__name__)

_BASE_URL = "https://eg.indeed.com"

_CARDS_MARKER = re.compile(
    r'window\.mosaic\.providerData\[\'?"?mosaic-provider-jobcards\'?"?\]\s*=\s*'
)
_VIEWJOB_MARKER = re.compile(r"window\._initialData\s*=\s*")
_ROOTPROPS_MARKER = re.compile(r"window\._rootProps\s*=\s*")
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

    JSONDecoder handles nested braces/escaped strings in C without copying
    the whole blob into another string. Returns {} when absent/unparseable.
    """
    m = marker_re.search(html or "")
    if not m:
        return {}
    start = html.find("{", m.end())
    if start == -1:
        return {}

    try:
        data, _end = json.JSONDecoder().raw_decode(html, start)
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError) as e:
        logger.warning(f"[indeed] Could not parse embedded JSON: {e}")
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


def _created_at(value) -> str | None:
    """Exact createDate (unix ms) as local wall time, else None (no fallback)."""
    try:
        return datetime.fromtimestamp(int(value) / 1000).strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError, OverflowError, OSError):
        return None


async def _bounce_back(page) -> str:
    """Async pages: go Back from Indeed's ;jsessionid auth bounce (HTTP 400 on
    a valid session, see login.is_jsessionid_bounce). Returns the URL after."""
    from boards.indeed.login import is_jsessionid_bounce

    url = page.url
    if not is_jsessionid_bounce(url):
        return url
    logger.info("[indeed] Bounced to the ;jsessionid auth page (HTTP 400) — going back.")
    try:
        await page.go_back(wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(2000)
        return page.url
    except Exception as exc:
        logger.info(f"[indeed] Back navigation failed: {type(exc).__name__}")
        return url


def _jobkey_from_url(url: str) -> str:
    m = re.search(r"[?&]jk=([^&]+)", url or "")
    return m.group(1) if m else ""


def _extract_jobs(html: str, seen_ids: set, lookup_seen: bool = False,
                  listings: dict | None = None) -> tuple[list, int, bool]:
    """Parse the search-page job cards blob.

    Returns (new_jobs, seen_count, blob_missing). `blob_missing` is True
    when the cards JSON was absent (CF challenge page, empty page, redesign).
    `listings` collects the createDate of already-known cards for repost
    detection (core/db.record_listings).
    """
    data = _extract_balanced_json(html, _CARDS_MARKER)
    if not data:
        return [], 0, True

    results = _dig(data, "metaData", "mosaicProviderJobCardsModel", "results")
    if not isinstance(results, list):
        return [], 0, True
    if lookup_seen:
        seen_ids = seen_ids | db.seen_ids_for("indeed", (
            item.get("jobkey") for item in results if isinstance(item, dict)))

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
            listed = _created_at(item.get("createDate")) if listings is not None else None
            if listed:
                listings[str(key)] = listed
            continue

        company = item.get("company") or ""
        if not isinstance(company, str):
            company = (company or {}).get("name", "") if isinstance(company, dict) else ""

        snippet = _strip_html(item.get("snippet"))
        extra: dict = {
            "location": _clean(
                item.get("formattedLocation") or item.get("jobLocationCity") or ""
            ),
            "description_truncated": True,
            "description_source": "search_snippet",
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
                "scraped_at": clock.now_str(),  # UTC; posted_at stays local
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
            if isinstance(obj.get("@graph"), list):
                yield from (o for o in obj["@graph"] if isinstance(o, dict))
        elif isinstance(obj, list):
            yield from (o for o in obj if isinstance(o, dict))


def _extract_detail(html: str, expected_key: str = "") -> tuple[str, dict]:
    """Parse a viewjob page. Returns (description, extra dict).

    Current: window._rootProps.preloadedVJData -> jobInfoWrapperModel.
    Older: window._initialData -> jobData.results[0].job.description
    (.text, else .html stripped) + latitude/longitude. The results list
    sits under hostQueryExecutionResult.data.jobData.results on the
    viewjob page itself, and under
    autoOpenTwoPaneViewjobResponse.body.hostQueryExecutionResult... on the
    search page's two-pane blob — try both.
    SUPPLEMENT/FALLBACK: typed JobPosting JSON-LD (description + metadata).
    """
    extra: dict = {}
    text = ""
    # Current standalone pages use strict JSON in _rootProps, while
    # _initialData is a JS object referring to it (not valid JSON).
    root = _extract_balanced_json(html, _ROOTPROPS_MARKER)
    view = root.get("preloadedVJData") or {}
    if not isinstance(view, dict):
        view = {}
    if expected_key and view.get("jobKey") not in (None, expected_key):
        return "", {}
    data = {} if view else _extract_balanced_json(html, _VIEWJOB_MARKER)
    info = _dig(view, "jobInfoWrapperModel", "jobInfoModel") or {}
    if isinstance(info, dict):
        text = _strip_html(info.get("sanitizedJobDescription"))
        location = _dig(info, "jobInfoHeaderModel", "formattedLocation") or view.get("jobLocation")
        if isinstance(location, str) and location:
            extra["location"] = _clean(location)
    salary = view.get("salaryInfoModel") or {}
    if isinstance(salary, dict) and (salary.get("salaryMin") is not None or salary.get("salaryMax") is not None):
        extra["salary"] = {"min": salary.get("salaryMin"), "max": salary.get("salaryMax"),
                           "currency": salary.get("salaryCurrency"), "type": salary.get("salaryType")}

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
            if isinstance(base, list):
                for result in base:
                    candidate = result.get("job") if isinstance(result, dict) else None
                    if isinstance(candidate, dict) and (
                        not expected_key or candidate.get("key") == expected_key
                    ):
                        job = candidate
                        break
                if job:
                    break

        if isinstance(job, dict):
            desc = job.get("description") or {}
            text = _clean(desc.get("text"))
            if not text and desc.get("html"):
                text = _strip_html(desc["html"])

            geo = job.get("location") or {}
            if isinstance(geo, dict):
                formatted = geo.get("formatted") or {}
                location = (formatted.get("long") if isinstance(formatted, dict) else "") or geo.get("fullAddress") or geo.get("city")
                if location:
                    extra["location"] = _clean(location)
                if geo.get("countryCode"):
                    extra["country"] = geo["countryCode"]
                if geo.get("latitude") is not None:
                    extra["latitude"] = geo["latitude"]
                if geo.get("longitude") is not None:
                    extra["longitude"] = geo["longitude"]

            for field, source_field in (("job_types", "jobTypes"),
                                        ("benefits", "benefits"),
                                        ("shift_and_schedule", "shiftAndSchedule")):
                values = [item.get("label") for item in job.get(source_field) or []
                          if isinstance(item, dict) and item.get("label")]
                if values:
                    extra[field] = list(dict.fromkeys(values))

    # Fallback: Schema.org JobPosting block (prefer the typed one).
    ld_obj = None
    for obj in _ldjson_objects(html):
        types = obj.get("@type")
        if isinstance(types, list) and "JobPosting" in types or types == "JobPosting":
            ld_key = _jobkey_from_url(obj.get("url") or "")
            if expected_key and ld_key and ld_key != expected_key:
                continue
            ld_obj = obj
            break
    if ld_obj:
        desc = _strip_html(ld_obj.get("description"))
        if not text:
            text = desc
        base = _dig(ld_obj, "baseSalary", "value")
        if isinstance(base, dict):
            ld_salary = {
                "min": base.get("minValue", base.get("value")),
                "max": base.get("maxValue", base.get("value")),
                "currency": _dig(ld_obj, "baseSalary", "currency"),
                "type": base.get("unitText"),
            }
        elif isinstance(base, (int, float)) and not isinstance(base, bool):
            ld_salary = {"min": base, "max": base,
                         "currency": _dig(ld_obj, "baseSalary", "currency")}
        else:
            ld_salary = {}
        if ld_salary:
            existing = extra.setdefault("salary", {})
            for key, value in ld_salary.items():
                if existing.get(key) in (None, "") and value is not None:
                    existing[key] = value
        employment = ld_obj.get("employmentType")
        if employment and not extra.get("job_types"):
            extra["job_types"] = employment if isinstance(employment, list) else [employment]
        if ld_obj.get("jobLocationType") == "TELECOMMUTE":
            extra["workplace"] = "Remote"
        if ld_obj.get("validThrough"):
            try:
                expiry = datetime.fromisoformat(ld_obj["validThrough"])
                extra["expire_at"] = expiry.astimezone().strftime("%Y-%m-%d %H:%M:%S")
            except (TypeError, ValueError):
                extra["expire_at_raw"] = ld_obj["validThrough"]
        places = ld_obj.get("jobLocation") or []
        if isinstance(places, dict):
            places = [places]
        if isinstance(places, list):
            locations = []
            for place in places:
                address = place.get("address") if isinstance(place, dict) else None
                if isinstance(address, dict):
                    locality = address.get("addressLocality")
                    if locality:
                        locations.append(locality)
                    country = address.get("addressCountry")
                    if isinstance(country, str) and country:
                        extra.setdefault("country", country)
            if locations:
                extra.setdefault("location", "; ".join(dict.fromkeys(locations)))

    return text, extra


class IndeedJobSpider(Spider):
    name = "indeed_job_spider"

    def __init__(self, selectors: dict, cdp_url: str, *args, **kwargs):
        self.sel = selectors  # unused — data comes from JSON blobs, kept for parity
        self.cdp_url = cdp_url
        self.seen_ids: set[str] = set()  # IDs discovered during this run only

        self._page_jobs: list[dict] = []
        self._pending: dict[str, dict] = {}  # jobkey -> placeholder job
        self._detail_jobs: dict[str, dict] = {}  # jobkey -> enriched job
        self._queued: set[str] = set()
        self._detail_snapshots = 0
        self.listings: dict[str, str] = {}  # known jobkey -> current createDate
        self.on_job = None  # board.save_now: persist each job once its detail page is read
        # Safety-net flags for the board: logged-out mid-scrape (auth
        # redirect) or a CF/WAF block page instead of the search page.
        self._logged_out = False
        self._blocked = False
        self.health = ScrapeHealth("indeed")

        super().__init__(*args, **kwargs)
        from core.log import configure_spider_logging
        configure_spider_logging(self)

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

    @timing.timed("indeed", "search_page", lambda spider, page: {"new_jobs": len(spider._page_jobs)})
    async def scan_search_page(self, page):
        self._page_jobs = []
        self.health.check("search_fetch", True)

        # Function-local import: boards.indeed.login lives next to this
        # module (no cycle — login.py never imports the spider).
        from boards.indeed.login import is_logged_out_url

        try:
            landed = await _bounce_back(page)
        except Exception:
            landed = ""
        if is_logged_out_url(landed):
            # Not signed in: stop here (don't grind turnstiles); the board runs
            # its login check and then this spider again.
            logger.info(f"[indeed] Landed logged out ({landed}).")
            self._logged_out = True
            try:
                markup.save_snapshot("indeed", "logged_out", await page.content())
            except Exception:
                pass
            return

        try:
            await page.wait_for_timeout(2500)  # let the SSR blobs land
            html = await page.content()
        except Exception as e:
            logger.info(f"[indeed] Could not read search page: {e}")
            await self.health.page_failure("search_structure", "Could not read search-page HTML.", page)
            return

        if "INDEED_CLOUDFLARE_STATIC_PAGE" in html:
            logger.info("[indeed] Cloudflare challenge page detected.")
            markup.save_snapshot("indeed", "cloudflare_challenge", html)
            self._blocked = True
            return

        try:
            title = await page.title()
        except Exception:
            title = ""
        # Job text can contain "Access Denied" (security roles, snippets), so
        # the body marker only counts when the job-card data is absent.
        if ("Just a moment" in title or "Blocked" in title or "Access Denied" in title
                or ("Access Denied" in html and not _CARDS_MARKER.search(html))):
            logger.info(f"[indeed] Block page instead of search (title {title!r}).")
            markup.save_snapshot("indeed", "blocked_page", html)
            self._blocked = True
            return

        jobs, seen, blob_missing = _extract_jobs(html, self.seen_ids, lookup_seen=True, listings=self.listings)
        self.health.check("search_structure", not blob_missing,
                          "The mosaic jobcards JSON or its results array is missing/unparseable.", html)
        if not blob_missing:
            data = _extract_balanced_json(html, _CARDS_MARKER)
            cards = _dig(data, "metaData", "mosaicProviderJobCardsModel", "results")
            self.health.check("card_identity", len(cards) == len(jobs) + seen,
                              f"Source has {len(cards)} cards; parsed {len(jobs)} new + {seen} known. Check jobkey fields.", html)
        self.health.job_fields(jobs, html, description=False)
        self._page_jobs = jobs
        for job in jobs:
            self.seen_ids.add(job["external_id"])

        logger.info(
            f"[indeed] {len(jobs)} new, {seen} already seen"
            f" ({blob_missing and 'blob MISSING' or 'blob ok'})"
        )
        if blob_missing and not jobs:
            markup.save_snapshot("indeed", "search_empty", html)

    @timing.timed("indeed", "detail_page", lambda spider, page, key="": {"jobkey": (key or "")[:16]})
    async def scan_detail_page(self, page, key: str = ""):
        # Keep the requested identity even if the page redirects to login.
        key = key or _jobkey_from_url(page.url)
        job = self._pending.get(key)
        if job is None:
            return

        pause = random.uniform(1.5, 3.0)  # human-like pacing
        timing.record("indeed", "human_delay", pause, {"jobkey": key[:16]})
        await asyncio.sleep(pause)
        try:
            await _bounce_back(page)
        except Exception:
            pass

        try:
            html = await page.content()
        except Exception as e:
            logger.warning(f"[indeed] Detail read failed for {key}: {e}")
            html = ""

        desc, extra = _extract_detail(html, expected_key=key)
        self.health.check("detail_description", bool(desc),
                          f"Job {key}: detail page yielded no full description; the search snippet was retained.", html)
        if desc:
            job["description"] = desc
            job["extra"]["description_truncated"] = False
            job["extra"]["description_source"] = "detail"
        else:
            job["extra"]["detail_status"] = "unavailable"
            logger.warning(f"[indeed] No description parsed for {key}")
            if self._detail_snapshots < 2:
                markup.save_snapshot("indeed", "detail_no_desc", html)
                self._detail_snapshots += 1
        for field, value in extra.items():
            if field == "salary" and isinstance(value, dict):
                # Search metadata can have a range but omit currency/period.
                salary = job["extra"].setdefault("salary", {})
                for name, amount in value.items():
                    if salary.get(name) in (None, "") and amount is not None:
                        salary[name] = amount
            elif not job["extra"].get(field):
                job["extra"][field] = value

        self._detail_jobs[key] = job
        if self.on_job:
            self.on_job(job)

    async def parse(self, response: Response):
        if self._logged_out or self._blocked:
            return

        # Every new card on the (single) page gets its full description; the
        # page itself bounds the detail navigations (~15).
        for job in self._page_jobs:
            key = job["external_id"]
            if key in self._queued:
                continue

            self._pending[key] = job
            self._queued.add(key)
            yield Request(
                f"{_BASE_URL}/viewjob?jk={key}",
                callback=partial(self.parse_job_detail, key=key),
                sid="stealth",
                page_action=partial(self.scan_detail_page, key=key),
            )

    async def parse_job_detail(self, response: Response, key: str = ""):
        key = key or _jobkey_from_url(response.url)
        job = self._detail_jobs.pop(key, None)
        if job is not None:
            self._pending.pop(key, None)
            yield job


def scrape(selectors: dict, cdp_url: str, health: ScrapeHealth | None = None, on_job=None) -> dict:
    """Run the spider. Returns {'items': [...], 'logged_out': bool,
    'blocked': bool} (mirrors the LinkedIn scrape contract).

    `on_job(job)` is called for each job as soon as its detail page is read.
    """
    spider = IndeedJobSpider(selectors=selectors, cdp_url=cdp_url)
    spider.on_job = on_job
    if health is not None:
        spider.health = health
    result = spider.start()
    items = list(result.items)
    # A failed detail request may never call its callback. Preserve its search
    # card so an unavailable detail page cannot silently discard a whole job.
    yielded = {job["external_id"] for job in items}
    for key, job in spider._pending.items():
        if key not in yielded:
            job["extra"]["description_truncated"] = True
            job["extra"]["detail_status"] = "fetch_failed"
            items.append(job)
    if spider._queued:
        failed = len(spider._pending)
        spider.health.check("detail_fetch", not failed,
                            f"{failed}/{len(spider._queued)} requested detail pages never completed; search cards were retained.")
    spider.health.report()
    logger.info(
        f"[indeed] {len(items)} item(s) scraped in {result.stats.elapsed_seconds:.1f}s"
    )
    return {
        "items": items,
        "listings": getattr(spider, "listings", {}),
        "logged_out": spider._logged_out,
        "blocked": spider._blocked,
    }
