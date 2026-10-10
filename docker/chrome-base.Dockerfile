# Chrome base image: Python + Google Chrome + Xvfb, built once and reused.
#
# Google's download link only ever serves the CURRENT Chrome, so building the
# app image straight from it silently changed Chrome whenever Docker's cache
# was cold. The app image now builds FROM a local, version-tagged base
# (Dockerfile: ARG CHROME_BASE). Build or upgrade it with
# scripts/build_chrome_base.sh, which tags it with the Chrome version it got.
# The Python image is pinned by digest for the same reason.
FROM python:3.13-slim@sha256:9662417aace5ae7b8e2609cce472b72a8958e134ba372808abe9cc1a0c0125e6

# ---------------------------------------------------------------------------
# Google Chrome stable (real_chrome=True requires the real binary) + Xvfb
# (virtual display so headful Chrome can run without a screen server).
# ---------------------------------------------------------------------------
RUN apt-get update \
    && apt-get install -y --no-install-recommends wget xvfb \
    && wget -q -O /tmp/chrome.deb \
        https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
    && apt-get install -y /tmp/chrome.deb \
    && rm /tmp/chrome.deb \
    && rm -rf /var/lib/apt/lists/*
