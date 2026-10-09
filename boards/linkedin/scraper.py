"""LinkedIn job spider built on scrapling's Spider framework.

ONE search page per run (the newest 25, sortBy=DD), every card on it checked:
the schedule runs often enough that anything on page 2 is already minutes old,
and no "stop at the first known card" rule can skip promoted/reposted cards.

Structure verified against the scrapling docs (spiders/sessions.html and
fetching/stealthy.html): configure_sessions + manager.add, requests routed
with sid="stealth", and per-request page_action callbacks.
"""

import logging
import asyncio
import random
import re
import time
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from scrapling import Selector
from scrapling.fetchers import AsyncStealthySession
from scrapling.spiders import Request, Response, Spider

from config import (LINKEDIN_SEARCH_URL, LINKEDIN_SEARCH_RECOVERY_ATTEMPTS,
                    LINKEDIN_SEARCH_RECOVERY_TIMEOUT_SECONDS, LINKEDIN_SLOW_ASSET_WAIT_SECONDS,
                    LINKEDIN_DETAIL_RECOVERY_TIMEOUT_SECONDS,
                    LINKEDIN_NAVIGATION_TIMEOUT_SECONDS,
                    LINKEDIN_NAVIGATION_RETRY_DELAY_SECONDS)
from core import clock, db, markup
from core.browser import patch_no_load_wait
from core.browser_diagnostics import BrowserDiagnostics
from core.scrape_health import ScrapeHealth
from core.log import configure_spider_logging

logger = logging.getLogger(__name__)

_DETAIL_TIMEOUT = 8_000
# LinkedIn renders 25 cards per search page. The list loads top-first and
# appends the rest while scrolled: snapshots caught 7 of 25 (positions 0-6).
_FULL_PAGE = 25
# On a slow link the other 18 cards arrive in ONE jump 11-21s after the
# scroll (Oct 9), so a still count only counts as settled while no LinkedIn
# data request is in flight; the cap bounds short pages.
_SETTLE_SECONDS = 25
_STABLE_SECONDS = 1.5
# Scrolls the card's nearest scrollable ancestor (step px, or to the bottom).
# Structural on purpose: LinkedIn's list scroller has only obfuscated class
# names, and the named `.scaffold-layout__list` does not scroll once CSS
# loads (overflow visible), so scrolling it was a no-op (7 of 25 cards, Oct 9).
_SCROLL_RESULTS_JS = """(card, step) => {
  let el = card;
  while ((el = el.parentElement)) {
    const overflow = getComputedStyle(el).overflowY;
    if ((overflow === 'auto' || overflow === 'scroll') && el.scrollHeight > el.clientHeight) break;
  }
  el = el || document.scrollingElement;
  el.scrollTop = step ? el.scrollTop + step : el.scrollHeight;
  return el.scrollTop;
}"""

_TIME_DELTAS = {
    "minute": lambda v: timedelta(minutes=v),
    "hour": lambda v: timedelta(hours=v),
    "day": lambda v: timedelta(days=v),
    "week": lambda v: timedelta(weeks=v),
    "month": lambda v: timedelta(days=v * 30),
}


_FRESH_AGE = re.compile(r"(\d+)\s+(minute|hour)s?\s+ago")


def _listed_at(text: str) -> str | None:
    """A card's "N minutes/hours ago" as local wall time, else None.

    Repost detection only: a repost is fresh, and day/week ages are too coarse
    to compare with the stored posting time. No fallback to "now"."""
    match = _FRESH_AGE.search(text or "")
    if not match:
        return None
    value, unit = int(match.group(1)), match.group(2)
    return (datetime.now() - _TIME_DELTAS[unit](value)).strftime("%Y-%m-%d %H:%M")


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


# Pill values LinkedIn renders in the fit-preferences row. Stored raw in
# extra; normalization to the da_guide enums happens at extraction time.
_WORKPLACE_PILLS = {"on-site", "on site", "remote", "hybrid"}
_JOB_TYPE_PILLS = {"full-time", "part-time", "contract", "temporary",
                   "internship", "freelance", "volunteer", "other"}
# Segments of the detail header that are NOT the location.
_HEADER_NOISE = re.compile(
    r"^(·|applicants?|no response insights.*|school alum.*)$"
    r"|ago|minute|hour|day|week|month|year|applicant|hiring|promoted",
    re.I,
)


def _parse_detail_header(header_text: str, pill_texts: list) -> dict:
    """Split the detail top-card header into location/workplace/job_type.

    header_text is the tertiary-description container rendered with "|"
    separators, e.g. "Cairo, Egypt|·|6 minutes ago|·|0 applicants".
    pill_texts are the fit-preferences buttons, e.g. ["On-site",
    "Full-time"]. Missing values come back as "".
    """
    location, workplace, job_type = "", "", ""
    for part in (p.strip(" \t\r\n\xa0") for p in header_text.split("|")):
        if not part or part == "·":
            continue
        if _HEADER_NOISE.search(part):
            continue
        if not location:
            location = part
    for pill in pill_texts:
        key = pill.strip().lower()
        if not workplace and key in _WORKPLACE_PILLS:
            workplace = pill.strip()
        elif not job_type and key in _JOB_TYPE_PILLS:
            job_type = pill.strip()
    return {"detail_location": location, "workplace": workplace,
            "job_type": job_type}


def _company_metadata(sel: Selector, selectors: dict) -> dict:
    """Preserve explicit employer context independently of the role's text."""
    extra = {}
    if selectors.get("company_industry"):
        text = _text(sel, selectors["company_industry"], separator="\n")
        industry = next((line.strip() for line in text.splitlines() if line.strip()), "")
        if industry:
            extra["company_industry"] = industry
    if selectors.get("company_description"):
        for element in sel.css(selectors["company_description"]):
            text = re.sub(r"\s+", " ", element.get_all_text(separator=" ")).strip()
            text = re.sub(r"\s*(?:…\s*)?show more\s*$", "", text, flags=re.I)
            if text:
                extra["company_description"] = text
                break
    return extra


def _search_failure_detail(html: str, selectors: dict, stage: str, error_type: str) -> str:
    """Describe the captured DOM without guessing why rendering failed."""
    if not html:
        return f"Search failed while {stage} ({error_type}); page content was unavailable."
    page = Selector(html)
    cards = len(page.css(selectors["job_card"]))
    lists = len(page.css(selectors["results_list"]))
    if page.css(selectors["login_redirect"]):
        reason = "Sign-in form is present on the search page; check session expiry."
    elif (not cards and not lists and selectors.get("loading_shell")
          and page.css(selectors["loading_shell"])):
        reason = ("LinkedIn is stuck on its startup/loading screen; no job cards or results list rendered. "
                  "The snapshot does not establish the cause of the loading failure.")
    else:
        reason = f"Search DOM contains {cards} cards and {lists} results lists; check loading or selector changes."
    return f"{reason} Failed while {stage} ({error_type})."


class LinkedInJobSpider(Spider):
    name = "linkedin_job_spider"

    def __init__(self, selectors: dict, cdp_url: str, *args, **kwargs):
        self.sel = selectors
        self.cdp_url = cdp_url
        self.seen_ids: set[str] = set()  # IDs discovered during this run only

        self._page_jobs: list[dict] = []
        self.listings: dict[str, str] = {}  # known job id -> current listing time
        self.on_job = None  # board.save_now: persist each job as soon as it is scraped
        self._login_redirect: bool = False
        self._detail_failures: dict[str, str] = {}
        self.health = ScrapeHealth("linkedin")
        self.diagnostics = BrowserDiagnostics({"www.linkedin.com", "linkedin.com", "static.licdn.com"})

        super().__init__(*args, **kwargs)
        configure_spider_logging(self)

    def configure_sessions(self, manager):
        manager.add(
            "stealth",
            AsyncStealthySession(
                cdp_url=self.cdp_url,
                # disable_resources=False: scrapling's resource blocking installs
                # page.route(), and Playwright disables Chrome's HTTP cache on any
                # intercepted page, so LinkedIn's ~3 MB app bundle downloaded on
                # EVERY run; on a slow link the app never booted (Oct 9: 24-59s,
                # cards never shown). Cached, the page needs ~0.5 MB and ~3s.
                disable_resources=False,
                timeout=LINKEDIN_NAVIGATION_TIMEOUT_SECONDS * 1000,
                retries=2,
                retry_delay=LINKEDIN_NAVIGATION_RETRY_DELAY_SECONDS,
                page_setup=self.setup_search_page,
            ),
        )

    async def setup_search_page(self, page):
        await patch_no_load_wait(page, selector_readiness=True)
        self.diagnostics.reset()
        self.diagnostics.attach(page)

    async def on_error(self, request: Request, error: Exception):
        # Called after scrapling exhausts its three navigation attempts. This
        # also covers a later pagination request failing after earlier success.
        self.diagnostics.navigation_error(error)
        self.health.check("search_fetch", False,
                          "Search navigation failed after retries. Evidence: " + self.diagnostics.summary())

    def _access_blocked(self, page, html: str) -> bool:
        """Login/checkpoints and explicit access/rate limits need a later run."""
        path = urlsplit(page.url or "").path.lower()
        if any(part in path for part in ("/login", "/checkpoint", "/authwall", "/security-verification")):
            return True
        if self.diagnostics.access_status in (401, 403, 429):
            return True
        return bool(html and Selector(html).css(self.sel["search"]["login_redirect"]))

    async def _wait_for_search(self, page) -> bool:
        """Retry a stalled load once; keep terminal evidence only if it persists.

        A timeout while LinkedIn's app bundle is still downloading gets ONE
        longer wait on the same page instead of a reload (which would restart
        the download); it does not use up a recovery attempt.
        """
        target = page.url
        stage = "waiting for job cards"
        html = ""
        last_error = "TimeoutError"
        if self._access_blocked(page, ""):
            try:
                initial_html = await page.content()
            except Exception:
                initial_html = ""
            detail = "LinkedIn search requires sign-in/checkpoint handling or returned an access/rate-limit error. "
            self.health.check("search_structure", False, detail + self.diagnostics.summary(), initial_html)
            return False
        attempt, extend, extended = 0, False, False
        while attempt <= LINKEDIN_SEARCH_RECOVERY_ATTEMPTS:
            if extend:
                deadline = time.monotonic() + LINKEDIN_SLOW_ASSET_WAIT_SECONDS
            else:
                deadline = time.monotonic() + (LINKEDIN_SEARCH_RECOVERY_TIMEOUT_SECONDS if attempt else 30)
            def remaining_ms():
                # Playwright treats timeout=0 as unlimited, so always use >=1.
                return max(1, int((deadline - time.monotonic()) * 1000))
            if attempt and not extend:
                logger.info("[linkedin] Retrying stalled search (%s/%s); previous evidence: %s",
                            attempt, LINKEDIN_SEARCH_RECOVERY_ATTEMPTS, self.diagnostics.summary())
                self.diagnostics.reset()
                stage = "reloading search"
                try:
                    await page.goto(target, wait_until="commit", timeout=remaining_ms())
                except Exception as exc:
                    # A navigation timeout can leave a usable DOM. Spend only
                    # the remainder of this attempt's deadline checking it.
                    last_error = type(exc).__name__
                    self.diagnostics.navigation_error(exc)
                    logger.warning("[linkedin] Recovery navigation: %s", last_error)
            extend = False
            try:
                if self._access_blocked(page, ""):
                    stage = "checking LinkedIn access (login/checkpoint or HTTP 401/403/429); no reload attempted"
                    break
                stage = "waiting for job cards"
                await page.wait_for_selector(self.sel["search"]["job_card"], timeout=remaining_ms())
                # A redirect or access response may arrive while cards hydrate.
                if self._access_blocked(page, ""):
                    stage = "checking LinkedIn access (login/checkpoint or HTTP 401/403/429); no reload attempted"
                    break
                stage = "scrolling the results list"
                for _ in range(4):
                    await self._scroll_results(page, 1000, timeout=remaining_ms())
                    await asyncio.sleep(0.8)
                if attempt or extended:
                    logger.info("[linkedin] Search recovered after %s retry%s; continuing normal extraction.",
                                attempt, " and a slow-asset wait" if extended else "")
                return True
            except Exception as exc:
                last_error = type(exc).__name__
                logger.warning("[linkedin] Search attempt %s failed while %s: %s", attempt + 1, stage, last_error)
                try:
                    html = await page.content()
                except Exception:
                    html = ""
                if self._access_blocked(page, html):
                    stage = "checking LinkedIn access (login/checkpoint or HTTP 401/403/429); no reload attempted"
                    break
                if (not extended and LINKEDIN_SLOW_ASSET_WAIT_SECONDS > 0
                        and self.diagnostics.still_loading("static.licdn.com script")):
                    extended = extend = True
                    logger.info("[linkedin] LinkedIn scripts still downloading (%s) — waiting up to %ss"
                                " on the same page instead of reloading.",
                                self.diagnostics.summary(), LINKEDIN_SLOW_ASSET_WAIT_SECONDS)
                    continue
            attempt += 1
        detail = _search_failure_detail(html, self.sel["search"], stage, last_error)
        detail += " Evidence: " + self.diagnostics.summary()
        self.health.check("search_structure", False, detail, html)
        return False

    async def start_requests(self):
        yield Request(
            LINKEDIN_SEARCH_URL,
            callback=self.parse,
            sid="stealth",
            page_action=self.deep_scan_page,
        )

    async def deep_scan_page(self, page):
        from core import timing

        self._page_jobs = []
        self._detail_failures = {}
        self.health.check("search_fetch", True)
        # Whole search page: hydration wait, scroll, card clicks, detail panels.
        with timing.stage("linkedin", "search_page", lambda: {"new_jobs": len(self._page_jobs)}):
            if not await self._wait_for_search(page):
                return

            cards = await self._settled_cards(page)
            card_ids = [(card, await card.get_attribute(self.sel["search"]["job_id_attr"]))
                        for card in cards]
            usable = sum(bool(key) for _, key in card_ids)
            if not usable:
                await self.health.page_failure("search_structure", f"Found {len(cards)} cards but no usable job IDs.", page)
                return
            self.health.check("search_structure", True)
            # Only page 1 is read, so an incomplete list silently leaves the
            # lower cards unchecked until a later run; never observed so far.
            self.health.check("full_page", len(cards) >= _FULL_PAGE,
                              f"Only {len(cards)} of {_FULL_PAGE} search cards rendered after waiting; "
                              "lower cards were not checked this run.")
            self.health.check("card_ids", usable == len(cards),
                              f"{len(cards) - usable} of {len(cards)} search cards had no job ID and were "
                              "skipped (layout change or non-job list items).")
            stored_ids = db.seen_ids_for("linkedin", (key for _, key in card_ids))
            known_ids = self.seen_ids | stored_ids
            self.listings.update(await self._known_listings(page, stored_ids))

            # Every unknown card on the page, wherever it sits: promoted or
            # reposted old cards never hide newer ones (no pagination).
            jobs_to_scrape_now = [(card, job_id) for card, job_id in card_ids
                                  if job_id and str(job_id) not in known_ids]

            logger.info(
                f"[info] Found {len(jobs_to_scrape_now)} new jobs and"
                f" {usable - len(jobs_to_scrape_now)} old jobs on this page"
                f" ({len(cards)} cards, {len(cards) - usable} without ID)."
            )

            for card, job_id in jobs_to_scrape_now:
                if self._login_redirect:
                    break  # session gone: every further card would wait out its timeouts
                pause = random.uniform(2.0, 4.0)  # human-like pause
                timing.record("linkedin", "human_delay", pause, {"card": str(job_id)})
                await asyncio.sleep(pause)
                with timing.stage("linkedin", "detail_panel", {"job_id": str(job_id)}):
                    job = await self._scrape_card(page, card, job_id)
                if job:
                    self._page_jobs.append(job)
                    self.seen_ids.add(str(job_id))
                    if self.on_job:
                        self.on_job(job)

        if jobs_to_scrape_now:
            failures = len(jobs_to_scrape_now) - len(self._page_jobs)
            if failures:
                reasons = "; ".join(f"{key}: {value}" for key, value in list(self._detail_failures.items())[:3])
                await self.health.page_failure("detail_fetch",
                    f"{failures}/{len(jobs_to_scrape_now)} new cards produced no job after recovery. "
                    f"{reasons}. Evidence: {self.diagnostics.summary()}", page)
            else:
                self.health.check("detail_fetch", True)
            self.health.job_fields(self._page_jobs)

        # Suspicious empty page or login redirect -> keep markup for debugging
        if (not jobs_to_scrape_now and len(cards) == 0) or self._login_redirect:
            html = await page.content()
            kind = "search_redirect" if self._login_redirect else "search_empty"
            markup.save_snapshot("linkedin", kind, html)

    async def _known_listings(self, page, known: set[str]) -> dict[str, str]:
        """Current listing time of already-known cards, for repost detection.

        One evaluate call reads every card's "N minutes ago" (a repost shows
        its new time there). Never fails the scan; occluded cards without a
        rendered time are simply skipped.
        """
        try:
            pairs = await page.locator(self.sel["search"]["job_card"]).evaluate_all(
                "(cards, [attr, timeCss]) => cards.map(card => [card.getAttribute(attr),"
                " (card.querySelector(timeCss) || {}).textContent || ''])",
                [self.sel["search"]["job_id_attr"], self.sel["search"]["card_listed"]])
        except Exception as exc:
            logger.debug("[linkedin] Card listing times unavailable: %s", type(exc).__name__)
            return {}
        listings = {}
        for job_id, text in pairs or []:
            listed = _listed_at(text) if job_id and str(job_id) in known else None
            if listed:
                listings[str(job_id)] = listed
        return listings

    async def _scroll_results(self, page, step: int | None, timeout: int = 1000):
        """Scroll the element that really scrolls the results list."""
        await page.locator(self.sel["search"]["job_card"]).first.evaluate(
            _SCROLL_RESULTS_JS, step, timeout=timeout)

    async def _settled_cards(self, page):
        """Card locators once the list is complete: 25 cards, or a count that
        stopped growing for 1.5s with no LinkedIn data request in flight (25s
        cap). Instant when all 25 are present."""
        cards = page.locator(self.sel["search"]["job_card"])
        deadline = time.monotonic() + _SETTLE_SECONDS
        last, changed_at = -1, time.monotonic()
        while True:
            count = await cards.count()
            now = time.monotonic()
            if count >= _FULL_PAGE or now >= deadline:
                break
            if count != last:
                last, changed_at = count, now
            elif now - changed_at >= _STABLE_SECONDS and not self.diagnostics.fetching("www.linkedin.com"):
                break
            try:  # appending is driven by scrolling the results list
                await self._scroll_results(page, None)
            except Exception:
                pass
            await asyncio.sleep(0.3)
        if count < _FULL_PAGE:
            logger.warning("[linkedin] Search list settled at %s of %s cards.", count, _FULL_PAGE)
        return await cards.all()

    async def _scrape_card(self, page, card, job_id: str) -> dict | None:
        try:
            if not await self._open_card(page, card, job_id):
                return None

            if (
                await page.locator(self.sel["search"]["login_redirect"]).count()
                > 0
            ):
                logger.warning("[warn] Redirected to login — session may have expired")
                self._login_redirect = True
                return None

            d = self.sel["job_detail"]
            # Detail panel HTML exists instantly (skeleton) — poll for
            # company hydration instead of a fixed sleep. Detail company
            # often takes 1-3s after click to populate.
            detail_company_loc = page.locator(d["company"]).first
            # Short initial settle then poll for non-empty company.
            await asyncio.sleep(random.uniform(0.8, 1.5))
            for _ in range(12):  # up to ~6s
                try:
                    if ((await detail_company_loc.text_content()) or "").strip():
                        break
                except Exception:
                    pass
                await asyncio.sleep(0.5)
            # Small extra settle for title/description to finish hydrating.
            await asyncio.sleep(random.uniform(0.5, 1.0))

            # Serialize ONLY the detail panel container instead of the whole
            # page (~15 KB vs ~550 KB per card) — the old full-page
            # page.content() per job dominated the run's CPU/memory. All
            # job_detail selectors are descendants of the panel container
            # (verified against markup/linkedin snapshots). Falls back to
            # the full page if the panel locator is gone (layout change /
            # slow hydration).
            try:
                html = await page.locator(d["panel"]).first.inner_html()
            except Exception:
                html = ""
            if not html.strip():
                logger.warning(f"[warn] Panel empty for {job_id} — full page fallback")
                html = await page.content()
            sel = Selector(html)

            title = _text(sel, d["title"])
            company = _text(sel, d["company"])
            rel_time = _text(sel, d["posted_at"])
            desc = _text(sel, d["description"], separator="\n")
            mgr_name = _text(sel, d["hiring_manager_name"])
            mgr_role = _text(sel, d["hiring_manager_role"])

            # Structured header: location ("Cairo, Egypt · 6 minutes ago")
            # plus fit-preference pills ("On-site", "Full-time"). Stored
            # raw in extra; the extractor normalizes to the guide enums.
            header_text = _text(sel, d.get("detail_header", ""),
                                separator="|") if d.get("detail_header") else ""
            pill_texts: list = []
            if d.get("fit_pills"):
                try:
                    for el in sel.css(d["fit_pills"]):
                        t = (el.get_all_text() or "").strip()
                        if t:
                            pill_texts.append(t)
                except Exception:
                    pass
            header = _parse_detail_header(header_text, pill_texts)

            # Fallback chain: Selector(html) can be stale vs live DOM,
            # and detail hydration can still lag. Card subtitle is always
            # populated (left pane) — use it when detail is empty.
            if not company:
                try:
                    live = ((await detail_company_loc.text_content()) or "").strip()
                    if live:
                        company = live
                except Exception:
                    pass
            if not company:
                try:
                    card_locator = self.sel["search"].get(
                        "card_company", ".artdeco-entity-lockup__subtitle"
                    )
                    # Primary: the card Handle we clicked
                    card_text = ""
                    try:
                        card_text = (
                            (await card.locator(card_locator).first.text_content())
                            or ""
                        ).strip()
                    except Exception:
                        pass
                    # Fallback: re-query by job_id in case card handle is stale
                    if not card_text:
                        try:
                            alt = page.locator(
                                f'li[data-occludable-job-id="{job_id}"] {card_locator}'
                            ).first
                            card_text = ((await alt.text_content()) or "").strip()
                        except Exception:
                            pass
                    if card_text:
                        company = card_text
                except Exception as e:
                    logger.warning(f"  [warn] card fallback failed for {job_id}: {e}")

            if not company:
                # Still empty after fallbacks — keep markup for debugging.
                snapshot = markup.save_snapshot("linkedin", "company_missing", html)
                self.health.snapshot = self.health.snapshot or snapshot
                logger.warning(f"  [warn] Company name not parsed for {job_id}")

            self.health.check("detail_content", bool(title and desc),
                              f"Job {job_id}: title present={bool(title)}, description present={bool(desc)}.", html)

            logger.info(f"  [ok] {title[:45]}")
            return {
                "source": "linkedin",
                "external_id": str(job_id),
                "title": title,
                "company": company,
                "posted_at": _parse_linkedin_time(rel_time),
                "description": desc,
                "link": f"https://www.linkedin.com/jobs/view/{job_id}/",
                "extra": {
                    **_company_metadata(sel, d),
                    "hiring_manager_name": mgr_name,
                    "hiring_manager_role": mgr_role,
                    "detail_location": header["detail_location"],
                    "workplace": header["workplace"],
                    "job_type": header["job_type"],
                },
                "scraped_at": clock.now_str(),  # UTC; posted_at stays local
            }
        except Exception as e:
            self._detail_failures[str(job_id)] = type(e).__name__
            logger.error(f"[error] Job {job_id}: {e}")
            return None

    async def _open_card(self, page, card, job_id: str) -> bool:
        """Retry a slow detail once and require the requested job's title link.

        Waiting for any existing detail panel can capture the previous card's
        description under the new ID. A matching title link prevents that race.
        """
        for attempt, timeout in enumerate((_DETAIL_TIMEOUT, LINKEDIN_DETAIL_RECOVERY_TIMEOUT_SECONDS * 1000)):
            try:
                deadline = time.monotonic() + timeout / 1000
                def remaining_ms():
                    return max(1, int((deadline - time.monotonic()) * 1000))
                await card.scroll_into_view_if_needed(timeout=remaining_ms())
                await asyncio.sleep(random.uniform(0.8, 1.2))
                await card.click(timeout=remaining_ms())
                await page.wait_for_selector(
                    self.sel["job_detail"]["title_link_for_job"].format(job_id=job_id), timeout=remaining_ms())
                await page.wait_for_selector(self.sel["search"]["detail_panel"], timeout=remaining_ms())
                self._detail_failures.pop(str(job_id), None)
                if attempt:
                    logger.info("[linkedin] Detail %s recovered on retry.", job_id)
                return True
            except Exception as exc:
                self._detail_failures[str(job_id)] = type(exc).__name__ + " waiting for matching job detail"
                try:
                    html = await page.content()
                except Exception:
                    html = ""
                if self._access_blocked(page, html):
                    self._detail_failures[str(job_id)] = "LinkedIn login/checkpoint/access restriction"
                    return False
                if not attempt:
                    logger.info("[linkedin] Detail %s did not load; retrying once with up to %ss.",
                                job_id, LINKEDIN_DETAIL_RECOVERY_TIMEOUT_SECONDS)
        logger.warning("[linkedin] Detail %s failed after two attempts.", job_id)
        return False

    async def parse(self, response: Response):
        # First page only: no follow-up page requests.
        for job in self._page_jobs:
            yield job


def scrape(selectors: dict, cdp_url: str, health: ScrapeHealth | None = None, on_job=None) -> dict:
    """Run the spider. Returns {'items': [...], 'login_redirect': bool}.

    `on_job(job)` is called for each job as soon as it is scraped.
    """
    spider = LinkedInJobSpider(selectors=selectors, cdp_url=cdp_url)
    spider.on_job = on_job
    if health is not None:
        spider.health = health
    result = spider.start()
    items = list(result.items)
    if "search_fetch" not in spider.health.checks:
        spider.health.check("search_fetch", False,
                            "Search navigation never reached the parser. Evidence: " + spider.diagnostics.summary())
    elif "search_structure" not in spider.health.checks and spider.health.checks["search_fetch"]["good"]:
        spider.health.check("search_structure", False, "The search callback did not finish checking the page. Check scraper logs.")
    spider.health.report()
    logger.info(
        f"[spider] {len(items)} item(s) scraped in {result.stats.elapsed_seconds:.1f}s"
    )
    return {"items": items, "login_redirect": spider._login_redirect, "listings": spider.listings}
