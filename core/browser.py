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


# Chrome processes we launched and have not stopped yet (for a forced exit).
_LIVE: set = set()


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def kill_stray_chrome() -> None:
    """Kill every Chrome process left from a crashed run (container only).

    Runs ONCE per cycle in main.py BEFORE any board starts: with boards in
    parallel, a per-board `pkill -f chrome` would kill the other boards'
    browsers. Stale profile locks are cleared per launch (clean_locks).
    """
    from config import KILL_CHROME_ON_START
    if not KILL_CHROME_ON_START:
        return
    logger.info("[browser] Killing stray Chrome processes...")
    try:
        subprocess.run(["pkill", "-f", "chrome"], capture_output=True, timeout=10)
    except Exception:
        pass


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
    proc._rtjobs_port = port  # stop_chrome() asks this endpoint for a clean quit
    _LIVE.add(proc)
    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{port}/json/version"
    launched = time.monotonic()
    while time.monotonic() < deadline:
        if _leader_exited(proc):
            tail = _stderr_tail(proc)
            stop_chrome(proc)
            raise RuntimeError(f"Chrome exited early with code {proc.returncode}: {tail}")
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


def _leader_exited(proc: subprocess.Popen) -> bool:
    """True once Chrome's main process exited, WITHOUT reaping it.

    The unreaped zombie keeps its PID, which is also our process-group id,
    reserved, so group signals sent before proc.wait() can never reach an
    unrelated group that reused the number. (A signal-0 probe could not tell
    the two apart.)
    """
    if proc.returncode is not None:
        return True
    try:
        return os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
    except ChildProcessError:
        return True


def _browser_close(port: int, timeout: float = 2.0) -> bool:
    """Ask Chrome to quit cleanly over CDP (`Browser.close`). True if sent.

    Chrome writes cookies to disk every 30s or on a clean shutdown, and
    SIGTERM exits at once WITHOUT that write (verified Oct 9 in the image's
    Chrome: a cookie set 5s before SIGTERM was gone; after Browser.close it
    was saved, exit in 0.1s). Runs end in 7-40s, so cookies the sites set or
    refreshed during a run (LinkedIn session, Indeed login, Cloudflare and
    Akamai clearance) were lost after most runs. Minimal stdlib WebSocket
    client: handshake, one masked text frame, no Origin header (allowed by
    chrome_args); any failure returns False and stop_chrome uses signals.
    """
    import base64
    import json

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=timeout) as reply:
            ws_url = json.load(reply)["webSocketDebuggerUrl"]
        hostport, path = ws_url.split("://", 1)[1].split("/", 1)
        host, _, ws_port = hostport.partition(":")
        with socket.create_connection((host, int(ws_port or 80)), timeout=timeout) as sock:
            key = base64.b64encode(os.urandom(16)).decode()
            sock.sendall((f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\n"
                          f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                          "Sec-WebSocket-Version: 13\r\n\r\n").encode())
            if b" 101 " not in sock.recv(4096).split(b"\r\n", 1)[0]:
                return False
            payload = json.dumps({"id": 1, "method": "Browser.close"}).encode()
            mask = os.urandom(4)
            sock.sendall(bytes([0x81, 0x80 | len(payload)]) + mask
                         + bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload)))
            try:
                sock.recv(1024)  # the reply, or Chrome closing the socket
            except OSError:
                pass
        return True
    except Exception as exc:
        logger.info(f"[browser] Clean CDP quit unavailable ({type(exc).__name__}); using signals.")
        return False


def stop_chrome(proc: subprocess.Popen | None) -> None:
    """Quit Chrome cleanly (cookies saved), else terminate its whole process
    group; force-kill stragglers. Idempotent."""
    if proc is None or getattr(proc, "_rtjobs_stopped", False):
        return
    proc._rtjobs_stopped = True
    try:
        if proc.returncode is None:  # unreaped, so the group id is still ours
            port = getattr(proc, "_rtjobs_port", None)
            if port and not _leader_exited(proc) and _browser_close(port):
                deadline = time.monotonic() + 5
                while not _leader_exited(proc) and time.monotonic() < deadline:
                    time.sleep(0.1)
            if not _leader_exited(proc):
                _signal_group(proc, signal.SIGTERM)
                deadline = time.monotonic() + 10
                while not _leader_exited(proc) and time.monotonic() < deadline:
                    time.sleep(0.1)
            # Renderer/GPU/zygote children can outlive the main process.
            _signal_group(proc, signal.SIGKILL)
            proc.wait(timeout=5)
    finally:
        _LIVE.discard(proc)
        stream = getattr(proc, "_rtjobs_stderr", None)
        if stream is not None:
            stream.close()


def kill_live_chrome() -> None:
    """Forced-exit cleanup: SIGKILL every Chrome process group we launched.

    Chrome runs in its own session/process group, so a terminal's Ctrl-C no
    longer reaches it; without this it would outlive a forced exit and keep
    the CDP port busy. Only unreaped leaders are signalled (see _leader_exited).
    """
    for proc in list(_LIVE):
        if proc.returncode is None:
            _signal_group(proc, signal.SIGKILL)
        _LIVE.discard(proc)


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


# Second look before skipping the solver: catches a challenge that replaces
# the page right after DOMContentLoaded.
_CF_RECHECK_MS = 1000


def install_cloudflare_fast_path() -> None:
    """Run scrapling's Cloudflare solver only when the page IS a challenge.

    The solver starts with wait_for_load_state("networkidle", timeout=5000)
    before it even looks for a challenge, and Wuzzuf/Indeed pages rarely go
    idle (trackers), so every fetch with solve_cloudflare=True paid ~4-5s
    with no challenge present (Wuzzuf: 6-8s per run for 0.1s of parsing;
    Indeed: every search and detail fetch). Detection is the solver's own
    `_detect_cloudflare` on the page HTML (cType marker or embedded Turnstile
    script), checked at once and again after _CF_RECHECK_MS; a challenge gets
    the original solver untouched (retries pass _attempts > 0 straight
    through). A challenge missed by both looks shows up in the board's own
    page checks (blocked/degraded run) and is solved on the next run.
    Idempotent.
    """
    from scrapling.engines._browsers._stealth import AsyncStealthySession, StealthySession
    from scrapling.engines.toolbelt.convertor import ResponseFactory

    if getattr(StealthySession, "_rtjobs_cf_fast_path", False):
        return
    sync_solver = StealthySession._cloudflare_solver
    async_solver = AsyncStealthySession._cloudflare_solver

    def _challenge(session, content) -> bool:
        return content is None or session._detect_cloudflare(content) is not None

    def solver(self, page, _attempts: int = 0):
        if _attempts == 0:
            for wait_ms in (0, _CF_RECHECK_MS):
                if wait_ms:
                    page.wait_for_timeout(wait_ms)
                try:
                    content = ResponseFactory._get_page_content(page)
                except Exception:
                    content = None  # unknown: let the real solver decide
                if _challenge(self, content):
                    break
            else:
                return None
        return sync_solver(self, page, _attempts)

    async def async_solver_fast(self, page, _attempts: int = 0):
        if _attempts == 0:
            for wait_ms in (0, _CF_RECHECK_MS):
                if wait_ms:
                    await page.wait_for_timeout(wait_ms)
                try:
                    content = await ResponseFactory._get_async_page_content(page)
                except Exception:
                    content = None
                if _challenge(self, content):
                    break
            else:
                return None
        return await async_solver(self, page, _attempts)

    StealthySession._cloudflare_solver = solver
    AsyncStealthySession._cloudflare_solver = async_solver_fast
    StealthySession._rtjobs_cf_fast_path = True


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
