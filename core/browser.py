"""Browser session workarounds.

scrapling's browser fetchers wait for the 'load' event on every navigation
(page.goto defaults to wait_until='load' and _wait_for_page_stability also
waits for it unconditionally). Some sites — LinkedIn especially — keep
tracker/CDN resources loading indefinitely, so 'load' never fires and every
fetch times out (tab spinner spins forever).

Fix: patch the page instance via scrapling's documented page_setup hook
(runs before navigation) so navigations wait only for 'domcontentloaded'.
The page is fully usable at that point; element waits (wait_for_selector /
locator.wait_for) handle the rest.

Both session flavors are supported: async sessions (AsyncStealthySession)
await the page_setup result and their goto/wait_for_load_state, so the
installed wrappers are coroutines there; sync sessions keep sync wrappers.

CDP attach (live debugging / manual 2FA solves):
Playwright always launches Chrome with --remote-debugging-pipe, which
disables the HTTP DevTools endpoint — so port 9222 would never serve
anything. Instead WE launch Chrome with --remote-debugging-port and let
scrapling connect via cdp_url. But scrapling's cdp_url path calls
browser.new_context(), which is isolated from the profile's default
context (cookies/session are lost — verified empirically). The patch
installed here swaps in the browser's default context instead.
"""

import inspect
import logging
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import urllib.request
from contextlib import contextmanager

logger = logging.getLogger(__name__)


def patch_no_load_wait(page, *, selector_readiness=False):
    """Make this page's navigations wait for domcontentloaded, not load.

    Returns a coroutine for async pages (scrapling awaits page_setup in
    async sessions) and None for sync pages.

    Search pages with explicit bounded readiness checks may opt into document
    commit only. Their page_action owns readiness, including access checks;
    login and Cloudflare sessions retain the default DOM-ready behavior.
    """
    page._rtjobs_selector_readiness = selector_readiness
    if inspect.iscoroutinefunction(page.goto):
        return _patch_async(page)
    return _patch_sync(page)


def _patch_sync(page) -> None:
    if getattr(page, "_no_load_patched", False):
        return
    orig_goto = page.goto

    def goto(url, *args, **kwargs):
        kwargs.setdefault("wait_until", "commit" if page._rtjobs_selector_readiness else "domcontentloaded")
        return orig_goto(url, *args, **kwargs)

    page.goto = goto

    orig_wait = page.wait_for_load_state

    def wait_for_load_state(state=None, *args, **kwargs):
        if page._rtjobs_selector_readiness and state in (None, "load", "domcontentloaded"):
            return None
        if state in (None, "load"):
            state = "domcontentloaded"
        return orig_wait(state, *args, **kwargs)

    page.wait_for_load_state = wait_for_load_state
    page._no_load_patched = True


async def _patch_async(page) -> None:
    if getattr(page, "_no_load_patched", False):
        return
    orig_goto = page.goto

    async def goto(url, *args, **kwargs):
        kwargs.setdefault("wait_until", "commit" if page._rtjobs_selector_readiness else "domcontentloaded")
        return await orig_goto(url, *args, **kwargs)

    page.goto = goto

    orig_wait = page.wait_for_load_state

    async def wait_for_load_state(state=None, *args, **kwargs):
        if page._rtjobs_selector_readiness and state in (None, "load", "domcontentloaded"):
            return None
        if state in (None, "load"):
            state = "domcontentloaded"
        return await orig_wait(state, *args, **kwargs)

    page.wait_for_load_state = wait_for_load_state
    page._no_load_patched = True


# ---------------------------------------------------------------------------
# CDP attach: launch Chrome ourselves with an HTTP DevTools port, let
# scrapling connect via cdp_url, and reuse the browser's default context.
# ---------------------------------------------------------------------------


def _find_chrome() -> str:
    for name in ("google-chrome", "google-chrome-stable", "chrome"):
        path = shutil.which(name)
        if path:
            return path
    return "/opt/google/chrome/chrome"


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def chrome_args(chrome: str, profile_dir: str, port: int, headless: bool = False,
                no_sandbox: bool | None = None) -> list[str]:
    """Launch flags. The DevTools WebSocket only accepts DevTools frontends:
    chrome://inspect (devtools://devtools), the frontend served on the port
    itself, and the hosted frontend Chrome advertises in /json/list
    (scripts/inspect_chrome.py prints it). Playwright sends no Origin and is
    always accepted. With "*", any web page open on this host could try to
    drive the logged-in LinkedIn/Indeed sessions."""
    if no_sandbox is None:
        from config import CHROME_NO_SANDBOX
        no_sandbox = CHROME_NO_SANDBOX
    args = [
        chrome,
        f"--user-data-dir={profile_dir}",
        f"--remote-debugging-port={port}",
        "--remote-allow-origins=" + ",".join((
            "devtools://devtools", f"http://localhost:{port}", f"http://127.0.0.1:{port}",
            "https://chrome-devtools-frontend.appspot.com")),
        *(["--no-sandbox"] if no_sandbox else []),
        "--disable-dev-shm-usage",
        "--no-first-run",
        "--no-default-browser-check",
        "about:blank",
    ]
    if headless:
        args.insert(1, "--headless=new")
    return args


def launch_cdp_chrome(profile_dir: str, port: int, headless: bool = False,
                      timeout: float = 30.0, clean_locks: bool = False) -> subprocess.Popen:
    """Launch real Chrome with an HTTP DevTools endpoint on `port`.

    Returns the process; call stop_chrome() when done. Raises RuntimeError
    if the port is already taken (we would attach to someone else's Chrome
    and profile) or if the endpoint doesn't come up; an early exit reports
    the tail of Chrome's stderr.

    clean_locks: remove stale Singleton* profile locks first. Only safe when
    no other Chrome can be using this profile (container mode — where the
    zombie-kill step just ran and this is the sole launcher). Without it, a
    previously killed Chrome makes the next launch exit with code 21
    ("profile appears to be in use").
    """
    if clean_locks:
        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            try:
                os.remove(os.path.join(profile_dir, name))
            except OSError:
                pass
    if _port_in_use(port):
        raise RuntimeError(f"CDP port {port} is already in use (another Chrome still running?)")

    args = chrome_args(_find_chrome(), profile_dir, port, headless=headless)
    stderr = tempfile.TemporaryFile()
    # Own process group, so stop_chrome() also ends renderer/GPU/zygote children.
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=stderr, start_new_session=True)
    proc._rtjobs_stderr = stderr
    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{port}/json/version"
    launched = time.monotonic()
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"Chrome exited early with code {proc.returncode}: {_stderr_tail(proc)}")
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    logger.info(f"[browser] Chrome up — CDP attachable at http://localhost:{port}")
                    from core import timing
                    timing.record("browser", "chrome_cold_start", time.monotonic() - launched,
                                  {"profile": profile_dir})
                    return proc
        except Exception:
            time.sleep(0.3)
    stop_chrome(proc)
    raise RuntimeError(f"Chrome CDP endpoint never came up on port {port}")


def _stderr_tail(proc, limit: int = 300) -> str:
    """Last non-empty stderr line(s) of a dead Chrome, for the failure alert."""
    stream = getattr(proc, "_rtjobs_stderr", None)
    if stream is None:
        return "no stderr captured"
    try:
        stream.seek(0)
        lines = [line for line in stream.read().decode(errors="replace").splitlines() if line.strip()]
    except OSError:
        return "stderr unreadable"
    return (" | ".join(lines[-2:]) or "no stderr output")[-limit:]


def stop_chrome(proc: subprocess.Popen | None) -> None:
    """Terminate Chrome's whole process group; force-kill if it doesn't exit."""
    if proc is None:
        return
    if proc.poll() is None:
        _signal_group(proc, signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _signal_group(proc, signal.SIGKILL)
            proc.wait(timeout=5)
    # Children can outlive the parent briefly; never leave them behind.
    _signal_group(proc, signal.SIGKILL)
    stream = getattr(proc, "_rtjobs_stderr", None)
    if stream is not None:
        stream.close()


def _signal_group(proc: subprocess.Popen, sig: int) -> None:
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def cdp_url_for(port: int) -> str:
    return f"http://127.0.0.1:{port}"


# ---------------------------------------------------------------------------
# Context-manager helper: bundles launch/stop/lock-cleanup (C3)
# ---------------------------------------------------------------------------


@contextmanager
def chrome_session(
    profile_dir: str,
    port: int,
    headless: bool = False,
    clean_locks: bool = False,
    timeout: float = 30.0,
):
    """Launch Chrome with CDP, yield its cdp_url, then stop it.

    Wrapper around :func:`launch_cdp_chrome` / :func:`stop_chrome` so boards
    don't repeat the same ~5-line block. Example::

        with chrome_session(profile_dir, port, headless, clean_locks) as cdp:
            items = scraper.scrape(selectors, cdp_url=cdp)
    """
    proc = launch_cdp_chrome(
        profile_dir, port, headless=headless, timeout=timeout, clean_locks=clean_locks
    )
    try:
        yield cdp_url_for(port)
    finally:
        stop_chrome(proc)


_PATCH_INSTALLED = False


def install_cdp_default_context_patch() -> None:
    """Make scrapling sessions connected via cdp_url reuse the browser's
    default (persistent profile) context.

    scrapling's own cdp_url path calls browser.new_context(), which is
    isolated from the profile's cookies — that would silently drop the
    LinkedIn login session / Wuzzuf cf_clearance cookie on every run.
    (Verified: cookies added in the default context survive reconnects;
    new_context() ones don't.) We replace the cdp branch of start() and
    grab browser.contexts[0] straight after connect_over_cdp — that's the
    profile's default context. (Careful: AFTER a new_context() call the
    list gets reordered and index 0 is the isolated one.) Idempotent.
    """
    global _PATCH_INSTALLED
    if _PATCH_INSTALLED:
        return

    from playwright.async_api import async_playwright
    from playwright.sync_api import sync_playwright

    from scrapling.fetchers import AsyncStealthySession, StealthySession

    orig_sync_start = StealthySession.start

    def sync_start(self):
        if not getattr(self._config, "cdp_url", None):
            return orig_sync_start(self)
        if self.playwright:
            raise RuntimeError("Session has been already started")
        self.playwright = sync_playwright().start()
        try:
            self.browser = self.playwright.chromium.connect_over_cdp(
                endpoint_url=self._config.cdp_url
            )
            if not self._config.proxy_rotator:
                assert self.browser is not None
                if not self.browser.contexts:
                    raise RuntimeError(
                        "CDP-connected browser has no default context"
                    )
                self.context = self._initialize_context(
                    self._config, self.browser.contexts[0]
                )
            self._is_alive = True
        except Exception:
            self.playwright.stop()
            self.playwright = None
            raise

    StealthySession.start = sync_start

    orig_async_start = AsyncStealthySession.start

    async def async_start(self):
        if not getattr(self._config, "cdp_url", None):
            return await orig_async_start(self)
        if self.playwright:
            raise RuntimeError("Session has been already started")
        self.playwright = await async_playwright().start()
        try:
            self.browser = await self.playwright.chromium.connect_over_cdp(
                endpoint_url=self._config.cdp_url
            )
            if not self._config.proxy_rotator:
                assert self.browser is not None
                if not self.browser.contexts:
                    raise RuntimeError(
                        "CDP-connected browser has no default context"
                    )
                self.context = await self._initialize_context(
                    self._config, self.browser.contexts[0]
                )
            self._is_alive = True
        except Exception:
            await self.playwright.stop()
            self.playwright = None
            raise

    AsyncStealthySession.start = async_start

    _PATCH_INSTALLED = True
