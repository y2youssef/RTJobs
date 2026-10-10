# Python + Google Chrome + Xvfb come from a local, version-tagged base image
# (docker/chrome-base.Dockerfile, scripts/build_chrome_base.sh): Google only
# serves the current Chrome, so installing it here changed Chrome silently on
# any cold-cache build. Upgrading Chrome = building a new base and changing
# this tag, then scripts/deploy.sh (its gate tests the new image first).
ARG CHROME_BASE=rtjobs-chrome-base:151.0.7922.108
FROM ${CHROME_BASE}

# ---------------------------------------------------------------------------
# Non-root user — Chrome still needs --no-sandbox in containers even as
# non-root (see core/browser.py); running as non-root limits the blast radius.
# ---------------------------------------------------------------------------
RUN groupadd -r scraper && useradd -r -g scraper -u 1000 scraper

WORKDIR /app

# ---------------------------------------------------------------------------
# Python deps
# ---------------------------------------------------------------------------
# requirements.lock pins every transitive version (constraints file).
COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir -r requirements.txt -c requirements.lock

# xauth is only used by xvfb-run (manual debugging); production starts Xvfb
# in docker/entrypoint.sh. Kept in its own layer after pip so the slow
# dependency layer stays cached when this changes.
RUN apt-get update \
    && apt-get install -y --no-install-recommends xauth \
    && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# App code
# ---------------------------------------------------------------------------
COPY --chown=scraper:scraper . .

# Create volume mount points and hand them to scraper user BEFORE USER switch.
RUN mkdir -p /app/chromeprofile /app/wuzzufprofile /app/indeedprofile /app/naukrigulfprofile /data \
    && chown -R scraper:scraper /app/chromeprofile /app/wuzzufprofile /app/indeedprofile /app/naukrigulfprofile /data

# Chrome (crashpad) needs a real, writable HOME; useradd -r doesn't create
# one, and without it Chrome SIGTRAPs at startup (core dump).
RUN mkdir -p /home/scraper && chown scraper:scraper /home/scraper
ENV HOME=/home/scraper

USER scraper

# The entrypoint starts Xvfb (virtual display for headful Chrome) and execs
# the command so it receives SIGTERM; attach live via CDP on 9222.
CMD ["/app/docker/entrypoint.sh", "python", "main.py"]
