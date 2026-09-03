FROM python:3.13-slim

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

# ---------------------------------------------------------------------------
# Non-root user — Chrome still needs --no-sandbox in containers even as
# non-root (see core/browser.py); running as non-root limits the blast radius.
# ---------------------------------------------------------------------------
RUN groupadd -r scraper && useradd -r -g scraper -u 1000 scraper

WORKDIR /app

# ---------------------------------------------------------------------------
# Python deps
# ---------------------------------------------------------------------------
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# xvfb-run needs xauth at runtime. Kept in its own layer after pip so the
# slow dependency layer stays cached when this changes.
RUN apt-get update \
    && apt-get install -y --no-install-recommends xauth \
    && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# App code
# ---------------------------------------------------------------------------
COPY --chown=scraper:scraper . .

# Create volume mount points and hand them to scraper user BEFORE USER switch.
RUN mkdir -p /app/chromeprofile /app/wuzzufprofile /app/indeedprofile /data \
    && chown -R scraper:scraper /app/chromeprofile /app/wuzzufprofile /app/indeedprofile /data

# Chrome (crashpad) needs a real, writable HOME; useradd -r doesn't create
# one, and without it Chrome SIGTRAPs at startup (core dump).
RUN mkdir -p /home/scraper && chown scraper:scraper /home/scraper
ENV HOME=/home/scraper

USER scraper

# xvfb-run gives headful Chrome a virtual display; attach live via CDP on 9222.
CMD ["xvfb-run", "-a", "python", "main.py"]
