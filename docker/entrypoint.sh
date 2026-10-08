#!/bin/sh
# Container entrypoint: start a virtual display, then exec the command.
#
# xvfb-run kept its own shell between tini and Python, and `docker stop`
# killed that shell first: Python never received SIGTERM, so Chrome was not
# stopped cleanly and run/cycle rows were left "running" (verified Oct 2026,
# exit 143 with no handler output). exec makes the command tini's direct
# child, so main.py's SIGTERM handler runs.
#
# The screen stays xvfb-run's 1280x1024x24 on purpose: the logged-in
# LinkedIn/Indeed sessions were established with this browser fingerprint.
set -eu
display="${XVFB_DISPLAY:-99}"
# `docker start` reuses the container filesystem; the previous run's Xvfb
# died with the container and left its lock and socket behind.
rm -f "/tmp/.X${display}-lock" "/tmp/.X11-unix/X${display}"
Xvfb ":${display}" -screen 0 "${XVFB_SCREEN:-1280x1024x24}" -nolisten tcp >/tmp/xvfb.log 2>&1 &
export DISPLAY=":${display}"
tries=0
until [ -S "/tmp/.X11-unix/X${display}" ]; do
    tries=$((tries + 1))
    if [ "$tries" -gt 100 ]; then
        echo "Xvfb did not start:" >&2
        cat /tmp/xvfb.log >&2
        exit 1
    fi
    sleep 0.1
done
exec "$@"
