# Build once with network access to fetch the pinned Playwright base image.
# Every verification run uses --network=none and --pull=never.
FROM mcr.microsoft.com/playwright/python:v1.64.0-noble

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin sandbox \
    && mkdir -p /opt/omnisight-sandbox \
    && chown -R sandbox:sandbox /opt/omnisight-sandbox

COPY --chown=10001:10001 verify.py /opt/omnisight-sandbox/verify.py

USER 10001:10001
WORKDIR /workspace
ENTRYPOINT ["python", "/opt/omnisight-sandbox/verify.py"]
