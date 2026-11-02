# Headless Chromium for tools/screenshot.sh. Not part of the monitoring stack:
# built on first use, run with --rm, never deployed. shot.py is mounted at run
# time, so editing it needs no rebuild.
FROM mcr.microsoft.com/playwright/python:v1.64.1-noble
# The base image has the browsers but not the Python package; keep the two
# versions equal or Playwright won't find its browser.
RUN pip install --no-cache-dir --break-system-packages playwright==1.64.1
