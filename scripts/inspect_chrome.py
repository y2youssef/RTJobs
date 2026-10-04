"""Print current CDP links, including the installed Chrome's own inspector.

Run during a scrape: python scripts/inspect_chrome.py [--port 9222]
Paste the local URL into Chrome's address bar if the remote frontend opens
blank. Tab IDs change when tabs or browsers are replaced.
"""

import argparse
import json
from urllib.parse import urlsplit
from urllib.request import urlopen


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=9222)
    args = parser.parse_args()
    base = f"http://127.0.0.1:{args.port}"
    try:
        with urlopen(base + "/json/version", timeout=3) as response:
            version = json.load(response)
        with urlopen(base + "/json/list", timeout=3) as response:
            targets = json.load(response)
    except (OSError, ValueError) as exc:
        print(f"Chrome is not reachable on port {args.port}: {exc}")
        print("Run this command during an active scrape or checkpoint wait.")
        return 1
    print("Container browser:", version.get("Browser", "unknown"))
    for target in targets:
        if target.get("type") != "page" or not target.get("webSocketDebuggerUrl"):
            continue
        ws = urlsplit(target["webSocketDebuggerUrl"])
        print("\nPage:", target.get("title") or target.get("url"))
        print("Installed Chrome frontend (paste into its address bar):")
        print(f"devtools://devtools/bundled/inspector.html?ws=localhost:{args.port}{ws.path}")
        print("Container-advertised frontend:")
        print(target.get("devtoolsFrontendUrl", "unavailable"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
