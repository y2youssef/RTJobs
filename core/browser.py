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
"""

import inspect


def patch_no_load_wait(page):
    """Make this page's navigations wait for domcontentloaded, not load.

    Returns a coroutine for async pages (scrapling awaits page_setup in
    async sessions) and None for sync pages.
    """
    if inspect.iscoroutinefunction(page.goto):
        return _patch_async(page)
    return _patch_sync(page)


def _patch_sync(page) -> None:
    orig_goto = page.goto

    def goto(url, *args, **kwargs):
        kwargs.setdefault("wait_until", "domcontentloaded")
        return orig_goto(url, *args, **kwargs)

    page.goto = goto

    orig_wait = page.wait_for_load_state

    def wait_for_load_state(state=None, *args, **kwargs):
        if state == "load":
            state = "domcontentloaded"
        return orig_wait(state, *args, **kwargs)

    page.wait_for_load_state = wait_for_load_state


async def _patch_async(page) -> None:
    orig_goto = page.goto

    async def goto(url, *args, **kwargs):
        kwargs.setdefault("wait_until", "domcontentloaded")
        return await orig_goto(url, *args, **kwargs)

    page.goto = goto

    orig_wait = page.wait_for_load_state

    async def wait_for_load_state(state=None, *args, **kwargs):
        if state == "load":
            state = "domcontentloaded"
        return await orig_wait(state, *args, **kwargs)

    page.wait_for_load_state = wait_for_load_state
