"""Wuzzuf job spider.

Wuzzuf is Cloudflare-protected (cf_clearance challenge) and server-side
rendered. We fetch the SSR search pages through a stealth browser session
with solve_cloudflare=True.

Two data sources per page, combined:
1. The rendered DOM (job card order, job ids from the slug links).
2. The embedded SSR state blob `window.Wuzzuf.initialStoreState.job.collection`
   — full entities per job with HTML description, requirements, exact
   postedAt/expireAt timestamps, salary, career level, workplace, etc.

Structure verified against the scrapling docs: spiders/sessions.html
(configure_sessions + manager.add + sid routing) and fetching/stealthy.html
(solve_cloudflare + user_data_dir on sessions).
"""

import json
import re
from datetime import datetime, timedelta

from scrapling import Selector
from scrapling.fetchers import AsyncStealthySession
from scrapling.spiders import Request, Response, Spider

from config import (
    CHROME_ARGS,
    HEADLESS,
    WUZZUF_PROFILE_DIR,
    WUZZUF_SEARCH_URL,
)
from core import db, markup
from core.browser import patch_no_load_wait

_MAX_PAGES = 20

_TIME_DELTAS = {
    "minute": timedelta(minutes=1),
    "hour": timedelta(hours=1),
    "day": timedelta(days=1),
    "week": timedelta(weeks=1),
    "month": timedelta(days=30),
}

_STATE_MARKER = re.compile(r'"job"\s*:\s*\{\s*"collection"\s*:')


def _extract_state(html: str) -> dict:
    """Parse the SSR state blob `window.Wuzzuf.initialStoreState.job.collection`
    straight from the page HTML (it's a JSON-able inline script).

    The `job` object is located by marker + brace balancing, then parsed.
    Returns {} when absent/unparseable — callers then fall back to the
    DOM-only fields.
    """
    m = _STATE_MARKER.search(html or "")
    if not m:
        return {}

    start = html.find("{", m.start())
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

    try:
        obj = json.loads("{" + html[m.start():k + 1] + "}")
        collection = (obj.get("job") or {}).get("collection") or {}
        return collection
    except (ValueError, TypeError) as e:
        print(f"[wuzzuf] Could not parse SSR state: {e}")
        return {}


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().rstrip("-").strip()


def _parse_ago(time_str: str) -> str:
    """'2 hours ago' -> ISO-ish local timestamp (fallback when no state blob)."""
    match = re.search(r"(\d+)\s+(minute|hour|day|week|month)", time_str or "")
    if not match:
        return datetime.now().strftime("%Y-%m-%d %H:%M")
    value, unit = int(match.group(1)), match.group(2)
    posted = datetime.now() - _TIME_DELTAS[unit] * value
    return posted.strftime("%Y-%m-%d %H:%M")


def _parse_state_timestamp(value: str) -> str:
    """'08/09/2026 16:48:58' -> '2026-08-09 16:48'.

    Wuzzuf emits MM/DD/YYYY; fall back to DD/MM/YYYY when a value can't
    parse the other way (day > 12).
    """
    for fmt in ("%m/%d/%Y %H:%M:%S", "%d/%m/%Y %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).strftime("%Y-%m-%d %H:%M")
        except (ValueError, TypeError):
            continue
    return ""


def _strip_html(html_text: str) -> str:
    """Convert the HTML description/requirements to plain text."""
    if not html_text:
        return ""
    try:
        text = Selector(html_text).get_all_text()
    except Exception:
        text = re.sub(r"<[^>]+>", " ", html_text)
    return re.sub(r"\s+", " ", text or "").strip()


def _job_id_from_slug(slug: str) -> str:
    return (slug or "").split("/")[-1].split("-")[0]


def _enrich(job: dict, entity: dict) -> dict:
    """Merge the rich SSR entity data into a DOM-extracted job dict."""
    attrs = (entity or {}).get("attributes") or {}
    posted = _parse_state_timestamp(attrs.get("postedAt"))
    extra = job["extra"]

    if posted:
        job["posted_at"] = posted

    description = _strip_html(attrs.get("description"))
    requirements = _strip_html(attrs.get("requirements"))
    if description:
        job["description"] = description

    salary = attrs.get("salary") or {}
    if salary.get("additionalDetails"):
        extra["salary"] = salary["additionalDetails"]
    elif salary.get("min") is not None or salary.get("max") is not None:
        extra["salary"] = _clean(
            f"{salary.get('min') or ''} - {salary.get('max') or ''}"
            f" {salary.get('currency') or ''} {salary.get('period') or ''}"
        )

    career = attrs.get("careerLevel") or {}
    if career.get("name"):
        extra["career_level"] = career["name"]
    workplace = (attrs.get("workplaceArrangement") or {}).get("displayedName")
    if workplace:
        extra["workplace"] = workplace
    work_types = [
        wt.get("displayedName") for wt in (attrs.get("workTypes") or [])
        if wt.get("displayedName")
    ]
    if work_types:
        extra["work_types"] = work_types
    years = attrs.get("workExperienceYears") or {}
    if years.get("min") is not None or years.get("max") is not None:
        extra["experience_years"] = {
            "min": years.get("min"),
            "max": years.get("max"),
        }
    if attrs.get("vacancies"):
        extra["vacancies"] = attrs["vacancies"]
    expire = _parse_state_timestamp(attrs.get("expireAt"))
    if expire:
        extra["expire_at"] = expire

    return job


def _extract_jobs(html, selectors, seen_ids, entities: dict | None = None) -> tuple[list, bool]:
    """Parse one SSR search page. Returns (jobs, found_duplicate).

    DOM gives the card order + job ids; the optional `entities` dict (from
    the window.Wuzzuf SSR state) enriches each job with the full payload.
    """
    sel = Selector(html)
    cards = [
        c
        for c in sel.css(selectors["search"]["job_card"])
        if c.css(selectors["search"]["title_link"])
    ]

    if entities:
        by_id = {
            _job_id_from_slug((e.get("attributes") or {}).get("slug")): e
            for e in entities.values()
        }
    else:
        by_id = {}

    jobs: list[dict] = []
    found_duplicate = False

    for card in cards:
        title_el = card.css(selectors["search"]["title_link"])[0]
        link = title_el.attrib.get("href", "")
        job_id = link.split("/")[-1].split("-")[0]
        if not job_id:
            continue

        if job_id in seen_ids:
            found_duplicate = True
            continue

        company = card.css(selectors["search"]["company"])
        location = card.css(selectors["search"]["location"])
        posted = card.css(selectors["search"]["posted_at"])
        tags = [
            t.get_all_text() for t in card.css(selectors["search"]["tags"])
        ]

        job = {
            "source": "wuzzuf",
            "external_id": job_id,
            "title": _clean(title_el.get_all_text()),
            "company": _clean(company[0].get_all_text()) if company else "",
            "posted_at": _parse_ago(
                _clean(posted[0].get_all_text()) if posted else ""
            ),
            "description": "",
            "link": f"https://wuzzuf.net{link}",
            "extra": {
                "location": _clean(location[0].get_all_text()) if location else "",
                "tags": [t for t in tags if t],
            },
            "scraped_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }

        entity = by_id.get(job_id)
        if entity:
            job = _enrich(job, entity)

        jobs.append(job)

    return jobs, found_duplicate


class WuzzufJobSpider(Spider):
    name = "wuzzuf_job_spider"

    def __init__(self, selectors: dict, *args, **kwargs):
        self.sel = selectors
        self.seen_ids = db.load_seen_ids("wuzzuf")
        self._page_jobs: list[dict] = []
        self._repeat_found: bool = False
        super().__init__(*args, **kwargs)

    def configure_sessions(self, manager):
        manager.add(
            "stealth",
            AsyncStealthySession(
                headless=HEADLESS,
                user_data_dir=WUZZUF_PROFILE_DIR,
                real_chrome=True,
                extra_flags=CHROME_ARGS,
                solve_cloudflare=True,
                timeout=120_000,
                page_setup=patch_no_load_wait,
            ),
        )

    async def start_requests(self):
        yield Request(
            WUZZUF_SEARCH_URL,
            callback=self.parse,
            sid="stealth",
            page_action=self.scan_page,
        )

    async def scan_page(self, page):
        self._page_jobs = []

        try:
            await page.wait_for_selector(
                self.sel["search"]["title_link"], timeout=45_000
            )
        except Exception as e:
            print(f"[wuzzuf] Job list never appeared: {e}")
            markup.save_snapshot("wuzzuf", "search_failed", await page.content())
            return

        html = await page.content()
        entities = _extract_state(html)
        jobs, found_duplicate = _extract_jobs(
            html, self.sel, self.seen_ids, entities
        )

        self._page_jobs = jobs
        for job in jobs:
            self.seen_ids.add(job["external_id"])

        print(
            f"[wuzzuf] {len(jobs)} new + {found_duplicate and 'duplicate(s)' or 'no duplicates'}"
        )
        if found_duplicate:
            print("[wuzzuf] Duplicate found — stopping pagination.")
            self._repeat_found = True

        if not jobs and not found_duplicate:
            markup.save_snapshot("wuzzuf", "search_empty", html)

    async def parse(self, response: Response):
        for job in self._page_jobs:
            yield job

        if self._repeat_found:
            return

        match = re.search(r"start=(\d+)", response.url)
        start = int(match.group(1)) if match else 0
        next_start = start + 1

        if next_start >= _MAX_PAGES:
            return

        next_url = (
            re.sub(r"start=\d+", f"start={next_start}", response.url)
            if "start=" in response.url
            else response.url + f"&start={next_start}"
        )
        print(f"[wuzzuf] → page start={next_start}")
        yield Request(
            next_url,
            callback=self.parse,
            sid="stealth",
            page_action=self.scan_page,
        )


def scrape(selectors: dict) -> list[dict]:
    """Run the spider and return the scraped job dicts."""
    spider = WuzzufJobSpider(selectors=selectors)
    result = spider.start()
    items = list(result.items)
    print(
        f"[wuzzuf] {len(items)} item(s) scraped in {result.stats.elapsed_seconds:.1f}s"
    )
    return items
