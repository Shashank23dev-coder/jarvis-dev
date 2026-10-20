# Release browser-check runner: pinned Playwright base (Chromium for 1.48) + the matching pinned client.
FROM mcr.microsoft.com/playwright/python@sha256:6bbd515848db4042068571135979b6ee6d330a794b28e037e3a6ccd7f2abfa26
RUN pip install --no-cache-dir playwright==1.48.0 && python3 -c "import playwright"
