#!/usr/bin/env bash
# Build the Chrome base image and tag it with the Chrome version inside it.
#
#   scripts/build_chrome_base.sh            # reuse Docker's cache (keeps the current Chrome)
#   scripts/build_chrome_base.sh --upgrade  # fetch the newest Chrome on purpose
#
# Then point Dockerfile's `ARG CHROME_BASE` at the printed tag (an upgrade is a
# deliberate change: test it with scripts/deploy.sh --dry-run first).
set -euo pipefail
cd "$(dirname "$0")/.."
args=(-q -f docker/chrome-base.Dockerfile -t rtjobs-chrome-base:building docker)
[ "${1:-}" = "--upgrade" ] && args=(--no-cache "${args[@]}")
docker build "${args[@]}" >/dev/null
version=$(docker run --rm --entrypoint google-chrome-stable rtjobs-chrome-base:building --version | awk '{print $3}')
[ -n "$version" ] || { echo "Could not read the Chrome version" >&2; exit 1; }
docker tag rtjobs-chrome-base:building "rtjobs-chrome-base:$version"
docker rmi -f rtjobs-chrome-base:building >/dev/null
pinned=$(sed -n 's/^ARG CHROME_BASE=//p' Dockerfile)
echo "Built rtjobs-chrome-base:$version (Dockerfile pins ${pinned:-nothing yet})"
